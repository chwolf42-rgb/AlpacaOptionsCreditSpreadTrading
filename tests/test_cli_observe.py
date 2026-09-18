from alpaca_options_credit.cli import main


def test_observe_fixture_once_exits_zero():
    code = main(["observe", "--fixture", "--once", "--log-level", "WARNING"])
    assert code == 0
