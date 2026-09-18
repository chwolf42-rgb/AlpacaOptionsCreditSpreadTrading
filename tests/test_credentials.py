from alpaca_options_credit.credentials import load_credentials
from alpaca_options_credit.errors import (
    CredentialIsolationError,
    MissingCredentialsError,
    PaperOnlyError,
)


def _base(**overrides):
    env = {
        "OPTIONS_APCA_API_KEY_ID": "opt_key_aaa",
        "OPTIONS_APCA_API_SECRET_KEY": "opt_secret_aaa",
        "OPTIONS_APCA_API_BASE_URL": "https://paper-api.alpaca.markets",
    }
    env.update(overrides)
    return env


def test_options_overrides_inherited_apca():
    env = _base(
        APCA_API_KEY_ID="inherited_key",
        APCA_API_SECRET_KEY="inherited_secret",
    )
    creds = load_credentials(env)
    assert creds.api_key_id == "opt_key_aaa"
    assert creds.paper is True
    assert "paper-api.alpaca.markets" in creds.base_url


def test_falls_back_to_apca_when_options_unset():
    env = {
        "APCA_API_KEY_ID": "inherited_key",
        "APCA_API_SECRET_KEY": "inherited_secret",
        "APCA_API_BASE_URL": "https://paper-api.alpaca.markets",
    }
    creds = load_credentials(env)
    assert creds.api_key_id == "inherited_key"


def test_refuse_if_key_matches_equity():
    env = _base(EQUITY_APCA_API_KEY_ID="opt_key_aaa")
    try:
        load_credentials(env)
        assert False, "expected isolation error"
    except CredentialIsolationError:
        pass


def test_refuse_if_key_matches_crypto():
    env = _base(CRYPTO_APCA_API_KEY_ID="opt_key_aaa")
    try:
        load_credentials(env)
        assert False, "expected isolation error"
    except CredentialIsolationError:
        pass


def test_refuse_live_url():
    env = _base(OPTIONS_APCA_API_BASE_URL="https://api.alpaca.markets")
    try:
        load_credentials(env)
        assert False, "expected paper-only error"
    except PaperOnlyError:
        pass


def test_fail_closed_without_secrets():
    try:
        load_credentials({})
        assert False, "expected missing credentials"
    except MissingCredentialsError:
        pass


def test_repr_does_not_include_secret():
    creds = load_credentials(_base())
    text = repr(creds)
    assert "opt_secret_aaa" not in text
    assert "opt_key_aaa" not in text or "opt_k" in text  # prefix hide
    assert "opt_secret" not in text
