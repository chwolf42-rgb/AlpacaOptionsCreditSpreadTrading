from datetime import date

from alpaca_options_credit.calendar_stub import skip_new_entry


def test_fomc_and_earnings_skip():
    cfg = {
        "calendar": {
            "skip_fomc": True,
            "fomc_blackout_days": 1,
            "skip_earnings": True,
            "earnings_blackout_days": 3,
        }
    }
    calendar = {"fomc": ["2026-03-18"], "earnings": {"AAPL": ["2026-03-20"]}}
    assert skip_new_entry(calendar, cfg, "SPY", date(2026, 3, 18)) == "fomc_blackout"
    assert skip_new_entry(calendar, cfg, "AAPL", date(2026, 3, 20)) == "earnings_blackout"
    assert skip_new_entry(calendar, cfg, "MSFT", date(2026, 3, 10)) is None
