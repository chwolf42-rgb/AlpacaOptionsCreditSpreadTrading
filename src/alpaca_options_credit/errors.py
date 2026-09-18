"""Fail-closed errors. Never leak secrets in messages."""


class BotError(Exception):
    """Base error for this process."""


class MissingCredentialsError(BotError):
    """OPTIONS/APCA keys are absent. Fail closed — do not talk to Alpaca."""


class CredentialIsolationError(BotError):
    """Resolved key matches a sibling (equity/crypto) key. Refuse to start."""


class PaperOnlyError(BotError):
    """Live trading URL or paper=false. This bot is paper-only."""


class AccountMismatchError(BotError):
    """GET /v2/account did not match APCA_EXPECTED_ACCOUNT_*."""


class ConfigError(BotError):
    """Invalid or missing config."""
