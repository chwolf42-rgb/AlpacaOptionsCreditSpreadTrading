"""client_order_id is unique per attempt. Alpaca never allows a reuse.

Trading's PR #15 repro: a deterministic id made an identical close stick to
an expired or filled order. These are those three cases, plus a timeout that
is still in flight across a restart.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import requests

from alpaca_options_credit.broker.alpaca import AlpacaBroker
from alpaca_options_credit.broker.payloads import (
    close_credit_spread_payload,
    mint_client_order_id,
    order_attempt_key,
)
from alpaca_options_credit.errors import SubmitUnconfirmed


class APIError(Exception):
    def __init__(self, status: int, msg: str) -> None:
        super().__init__(msg)
        self.response = SimpleNamespace(status_code=status)


class FakeTrading:
    """Alpaca-like: client_order_id unique across all orders, ever."""

    def __init__(self) -> None:
        self.by_cid: dict[str, SimpleNamespace] = {}
        self.posts = 0
        self.n = 0
        self.requests: list[object] = []

    def submit_order(self, req):
        self.posts += 1
        self.requests.append(req)
        cid = req.client_order_id
        if cid in self.by_cid:
            raise APIError(422, '{"code":40010001,"message":"client_order_id must be unique"}')
        self.n += 1
        order = SimpleNamespace(id=f"oid{self.n}", status="new", client_order_id=cid)
        self.by_cid[cid] = order
        return order

    def get_order_by_client_id(self, cid):
        if cid not in self.by_cid:
            raise APIError(404, "order not found")
        return self.by_cid[cid]


def _broker(trading, tmp_path) -> AlpacaBroker:
    broker = object.__new__(AlpacaBroker)
    broker._trading = trading
    broker._submit_state_path = tmp_path / "mleg_submit_attempts.json"
    return broker


def _payload(**overrides):
    body = dict(
        short_occ="MSFT261016P00400000",
        long_occ="MSFT261016P00395000",
        qty=1,
        debit=0.45,
    )
    body.update(overrides)
    return close_credit_spread_payload(**body)


def _only_cid(trading: FakeTrading) -> str:
    assert len(trading.by_cid) == 1
    return next(iter(trading.by_cid))


def test_minted_ids_differ_and_fit_alpaca_limit():
    first = mint_client_order_id(_payload())
    second = mint_client_order_id(_payload())
    assert first != second
    assert first.startswith("oc")
    assert len(first) <= 128
    assert len(second) <= 128


def test_same_process_resend_after_expiry_posts_a_new_id(tmp_path):
    trading = FakeTrading()
    broker = _broker(trading, tmp_path)
    first = broker._submit_mleg(_payload())
    expired = _only_cid(trading)
    trading.by_cid[expired].status = "expired"
    second = broker._submit_mleg(_payload())
    assert first == "oid1"
    assert second == "oid2"
    assert second != first
    assert trading.posts == 2
    assert len(trading.requests[-1].legs) == 2
    assert trading.requests[-1].client_order_id != expired
    assert len(trading.requests[-1].client_order_id) <= 128


def test_restart_after_expired_order_places_a_new_one(tmp_path):
    trading = FakeTrading()
    _broker(trading, tmp_path)._submit_mleg(_payload())
    trading.by_cid[_only_cid(trading)].status = "expired"
    restarted = _broker(trading, tmp_path)
    placed = restarted._submit_mleg(_payload())
    assert placed == "oid2"
    assert trading.posts == 2
    assert len(trading.by_cid) == 2


def test_saved_expired_id_is_not_posted_again(tmp_path):
    """A crash can leave the expired attempt on disk. The next poll mints a new id."""
    trading = FakeTrading()
    broker = _broker(trading, tmp_path)
    payload = _payload()
    first = broker._submit_mleg(payload)
    expired = _only_cid(trading)
    trading.by_cid[expired].status = "expired"
    state = tmp_path / "mleg_submit_attempts.json"
    state.write_text(
        json.dumps({"attempts": {order_attempt_key(payload): expired}}),
        encoding="utf-8",
    )
    restarted = _broker(trading, tmp_path)
    second = restarted._submit_mleg(_payload())
    assert first == "oid1"
    assert second == "oid2"
    assert trading.posts == 2
    assert expired not in {req.client_order_id for req in trading.requests[1:]}


def test_earlier_identical_fill_is_not_the_new_attempt(tmp_path):
    trading = FakeTrading()
    _broker(trading, tmp_path)._submit_mleg(_payload())
    trading.by_cid[_only_cid(trading)].status = "filled"
    restarted = _broker(trading, tmp_path)
    placed = restarted._submit_mleg(_payload())
    assert placed == "oid2"
    assert placed != "oid1"
    assert trading.posts == 2


def test_unresolved_fill_is_returned_once_then_a_new_attempt_posts(tmp_path):
    class _FilledOnLookup(FakeTrading):
        def submit_order(self, req):
            if self.posts == 0:
                self.posts += 1
                self.requests.append(req)
                self.n += 1
                order = SimpleNamespace(id="oid1", status="filled", client_order_id=req.client_order_id)
                self.by_cid[req.client_order_id] = order
                raise requests.Timeout("submit hung")
            return super().submit_order(req)

    trading = _FilledOnLookup()
    broker = _broker(trading, tmp_path)
    assert broker._submit_mleg(_payload()) == "oid1"
    assert trading.posts == 1
    again = _broker(trading, tmp_path)._submit_mleg(_payload())
    assert again == "oid2"
    assert again != "oid1"
    assert trading.posts == 2


def test_timeout_then_restart_finds_the_saved_live_order(tmp_path):
    class _LandsThenTimesOut(FakeTrading):
        def __init__(self) -> None:
            super().__init__()
            self.lookup_timeouts_left = 1

        def submit_order(self, req):
            self.posts += 1
            self.requests.append(req)
            self.n += 1
            order = SimpleNamespace(id="oid-live", status="accepted", client_order_id=req.client_order_id)
            self.by_cid[req.client_order_id] = order
            raise requests.Timeout("submit hung")

        def get_order_by_client_id(self, cid):
            if self.lookup_timeouts_left:
                self.lookup_timeouts_left -= 1
                raise requests.Timeout("lookup hung")
            return super().get_order_by_client_id(cid)

    trading = _LandsThenTimesOut()
    broker = _broker(trading, tmp_path)
    with pytest.raises(SubmitUnconfirmed):
        broker._submit_mleg(_payload())
    assert trading.posts == 1
    state = json.loads((tmp_path / "mleg_submit_attempts.json").read_text(encoding="utf-8"))
    assert list(state["attempts"].values()) == [trading.requests[0].client_order_id]

    restarted = _broker(trading, tmp_path)
    assert restarted._submit_mleg(_payload()) == "oid-live"
    assert trading.posts == 1
    assert len(trading.requests[0].legs) == 2


def test_open_attempt_does_not_swallow_a_different_order(tmp_path):
    class _Hang(FakeTrading):
        def submit_order(self, req):
            self.posts += 1
            self.requests.append(req)
            raise requests.Timeout("submit hung")

        def get_order_by_client_id(self, cid):
            raise requests.Timeout("lookup hung")

    trading = _Hang()
    broker = _broker(trading, tmp_path)
    with pytest.raises(SubmitUnconfirmed):
        broker._submit_mleg(_payload())
    other = FakeTrading()
    other_broker = _broker(other, tmp_path)
    # Same state file, different legs: a new logical order, not the hung one.
    placed = other_broker._submit_mleg(
        _payload(short_occ="AAPL261016P00200000", long_occ="AAPL261016P00195000", debit=0.30)
    )
    assert placed == "oid1"
    assert other.posts == 1
    assert trading.posts == 1
