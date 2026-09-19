"""Config lock: no equity OCO/bracket stop path on this options sleeve."""

from __future__ import annotations

import pytest

from alpaca_options_credit.config import (
    OPTIONS_NATIVE_EXIT_PATH,
    load_config,
    validate_exit_policy,
)
from alpaca_options_credit.errors import ExitPolicyError


def test_default_config_locks_options_native_exits():
    cfg = load_config()
    exits = cfg["exits"]
    assert exits["path"] == OPTIONS_NATIVE_EXIT_PATH
    assert exits["forbid_equity_oco_bracket"] is True
    assert exits["never_cancel_working_close"] is True
    assert cfg["rth"]["manage_exits_off_hours"] is False
    assert cfg["broker"]["order_class"] == "mleg"
    assert "oco" not in exits
    assert "bracket" not in exits


def test_refuses_equity_oco_path():
    cfg = load_config()
    cfg["exits"]["path"] = "equity_oco_bracket"
    with pytest.raises(ExitPolicyError, match="forbidden"):
        validate_exit_policy(cfg)


def test_refuses_oco_key():
    cfg = load_config()
    cfg["exits"]["oco"] = True
    with pytest.raises(ExitPolicyError, match="equity stop keys"):
        validate_exit_policy(cfg)


def test_refuses_disabling_cancel_guard():
    cfg = load_config()
    cfg["exits"]["never_cancel_working_close"] = False
    with pytest.raises(ExitPolicyError, match="naked"):
        validate_exit_policy(cfg)


def test_refuses_broker_oco_class():
    cfg = load_config()
    cfg["broker"]["order_class"] = "oco"
    with pytest.raises(ExitPolicyError, match="bracket"):
        validate_exit_policy(cfg)
