"""Credential isolation for the options paper account.

OPTIONS_APCA_* overrides inherited APCA_*. The resolved key is refused if it
matches EQUITY_APCA_API_KEY_ID or CRYPTO_APCA_API_KEY_ID. Live URLs are refused.
Missing secrets fail closed (no Alpaca client is constructed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

from alpaca_options_credit.errors import (
    AccountMismatchError,
    CredentialIsolationError,
    MissingCredentialsError,
    PaperOnlyError,
)

PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOST = "api.alpaca.markets"
DEFAULT_PAPER_URL = f"https://{PAPER_HOST}"
DEFAULT_DATA_URL = "https://data.alpaca.markets"

# Sibling env names — isolation only; this bot never reads their journals.
EQUITY_KEY_ENV = "EQUITY_APCA_API_KEY_ID"
CRYPTO_KEY_ENV = "CRYPTO_APCA_API_KEY_ID"


def _strip(value: Optional[str]) -> str:
    return (value or "").strip()


@dataclass
class Credentials:
    api_key_id: str
    api_secret_key: str
    base_url: str
    data_url: str
    expected_account_number: Optional[str]
    paper: bool = True

    def __repr__(self) -> str:  # pragma: no cover - safety
        hid = self.api_key_id[:4] + "…" if self.api_key_id else ""
        return f"Credentials(api_key_id={hid!r}, base_url={self.base_url!r}, paper={self.paper})"

    __str__ = __repr__


def _first_env(env: Mapping[str, str], *names: str) -> str:
    for name in names:
        val = _strip(env.get(name))
        if val:
            return val
    return ""


def resolve_base_url(raw: str) -> str:
    url = _strip(raw) or DEFAULT_PAPER_URL
    url = url.rstrip("/")
    # Accept either host or host/v2 — TradingClient wants the host origin.
    if url.endswith("/v2"):
        url = url[: -len("/v2")]
    return url


def assert_paper_only(base_url: str) -> None:
    lowered = base_url.lower()
    if LIVE_HOST in lowered and PAPER_HOST not in lowered:
        raise PaperOnlyError(
            "live Alpaca URL refused — this bot is paper-only "
            f"(got host containing {LIVE_HOST})"
        )
    if PAPER_HOST not in lowered:
        raise PaperOnlyError(
            f"base URL must be the paper host {PAPER_HOST!r}, got {base_url!r}"
        )


def load_credentials(env: Optional[Mapping[str, str]] = None) -> Credentials:
    """Fail closed without secrets. OPTIONS_* wins over inherited APCA_*."""
    env = env if env is not None else os.environ

    key = _first_env(env, "OPTIONS_APCA_API_KEY_ID", "APCA_API_KEY_ID")
    secret = _first_env(env, "OPTIONS_APCA_API_SECRET_KEY", "APCA_API_SECRET_KEY")
    if not key or not secret:
        raise MissingCredentialsError(
            "missing OPTIONS_APCA_API_KEY_ID / OPTIONS_APCA_API_SECRET_KEY "
            "(or inherited APCA_*). Copy .env.example → .env and fill the "
            "options paper keys. Observer --fixture does not need keys."
        )

    equity_key = _first_env(env, EQUITY_KEY_ENV)
    crypto_key = _first_env(env, CRYPTO_KEY_ENV)
    if equity_key and key == equity_key:
        raise CredentialIsolationError(
            "resolved Alpaca key matches EQUITY_APCA_API_KEY_ID — "
            "this options bot must use a separate paper account"
        )
    if crypto_key and key == crypto_key:
        raise CredentialIsolationError(
            "resolved Alpaca key matches CRYPTO_APCA_API_KEY_ID — "
            "this options bot must use a separate paper account"
        )

    base_url = resolve_base_url(
        _first_env(env, "OPTIONS_APCA_API_BASE_URL", "APCA_API_BASE_URL")
        or DEFAULT_PAPER_URL
    )
    assert_paper_only(base_url)

    data_url = _strip(
        _first_env(env, "OPTIONS_APCA_DATA_URL", "APCA_API_DATA_URL")
        or DEFAULT_DATA_URL
    )
    expected = _first_env(
        env,
        "OPTIONS_APCA_EXPECTED_ACCOUNT_NUMBER",
        "APCA_EXPECTED_ACCOUNT_NUMBER",
        "OPTIONS_APCA_EXPECTED_ACCOUNT_ID",
        "APCA_EXPECTED_ACCOUNT_ID",
    ) or None

    return Credentials(
        api_key_id=key,
        api_secret_key=secret,
        base_url=base_url,
        data_url=data_url,
        expected_account_number=expected,
        paper=True,
    )


def assert_expected_account(actual_number: Optional[str], expected: Optional[str]) -> None:
    if not expected:
        return
    if not actual_number or actual_number.strip() != expected.strip():
        raise AccountMismatchError(
            "Alpaca account number did not match OPTIONS/APCA_EXPECTED_ACCOUNT_* "
            "(values not logged)"
        )
