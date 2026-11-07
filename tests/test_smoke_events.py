"""pipeline.smoke_events (#146): the dry plan requests nothing and never prints a key value;
--live (with injected scenario data and a fixed clock) prints the verdicts and raw inputs.
Always --no-dotenv, so the repo's real .env is never loaded. No network."""
from __future__ import annotations

import pytest

import pipeline.smoke_events as se
from pipeline.scenarios import ScenarioData, load_scenario
from pipeline.smoke_events import NOT_LIVE, main
from tests.test_orchestrator import clean_env, loopback_only  # noqa: F401  (autouse fixtures)
from tools.halt_feed import HALT_FEED_URL

SENTINELS = {
    "FMP_API_KEY": "SENTINEL-FMP-KEY-111",
    "ALPACA_API_KEY": "SENTINEL-ALPACA-ID-222",
    "ALPACA_SECRET_KEY": "SENTINEL-ALPACA-SECRET-333",
    "SEC_USER_AGENT": "SENTINEL-UA-444 sentinel@example.invalid",
}


class Tripwire:
    """A data object whose every method fails the test if called (the dry run must not)."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError(f"dry run requested data: {name}")
        return boom


@pytest.fixture
def no_live_data(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LiveData was constructed in a dry run")
    monkeypatch.setattr(se, "LiveData", boom)


# ============================ dry run ============================

def test_dry_run_prints_the_plan_requests_nothing_and_exits_3(monkeypatch, capsys, no_live_data):
    for k, v in SENTINELS.items():
        monkeypatch.setenv(k, v)
    code = main(["AAPL", "tsla", "--no-dotenv"], data=Tripwire(), clock=Tripwire())
    out = capsys.readouterr().out
    assert code == NOT_LIVE == 3
    assert "== smoke_events: dry plan only, NOTHING was requested ==" in out
    assert "symbols: AAPL, TSLA" in out
    assert HALT_FEED_URL in out and "FMP earnings calendar" in out and "SEC EDGAR" in out
    assert "Alpaca news" in out and "No Claude call, no order, no journal write." in out
    assert "--live" in out
    for k in SENTINELS:
        assert f"{k} set" in out
    for value in SENTINELS.values():
        assert value not in out
        assert value.split()[0] not in out


def test_dry_run_names_missing_keys_as_missing(monkeypatch, capsys, no_live_data):
    for k in SENTINELS:
        monkeypatch.delenv(k, raising=False)
    assert main(["AAPL", "--no-dotenv"]) == NOT_LIVE
    out = capsys.readouterr().out
    for k in SENTINELS:
        assert f"{k} MISSING" in out


def test_dry_run_treats_a_blank_key_as_missing(monkeypatch, capsys, no_live_data):
    monkeypatch.setenv("FMP_API_KEY", "   ")
    main(["AAPL", "--no-dotenv"])
    assert "FMP_API_KEY MISSING" in capsys.readouterr().out


def test_dry_run_with_an_env_file_never_prints_its_values(tmp_path, monkeypatch, capsys,
                                                          no_live_data):
    for k in SENTINELS:
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / "keys.env"
    env.write_text("FMP_API_KEY=FILE-SENTINEL-999\n", encoding="utf-8")
    try:
        assert main(["AAPL", "--env-file", str(env)]) == NOT_LIVE
        out = capsys.readouterr().out
        assert "FMP_API_KEY set" in out and "FILE-SENTINEL-999" not in out
    finally:
        monkeypatch.delenv("FMP_API_KEY", raising=False)  # load_dotenv writes os.environ


def test_dry_run_plan_counts_one_call_per_symbol(capsys, no_live_data):
    main(["AAPL", "TSLA", "MSFT", "--no-dotenv"])
    out = capsys.readouterr().out
    assert "1 submissions call per symbol (3)" in out and "for the last 24 hours (3)" in out


# ============================ bad arguments ============================

@pytest.mark.parametrize("bad", ["", "TOOLONGSYMBOLXX", "AA PL", "AAPL;", "$$", "12"])
def test_blocked_an_invalid_symbol_exits_2_before_anything_is_requested(bad, capsys,
                                                                       no_live_data):
    code = main([bad, "--no-dotenv", "--live"], data=Tripwire())
    captured = capsys.readouterr()
    assert code == 2 and captured.out == "" and captured.err.startswith("error:")


def test_blocked_one_invalid_symbol_among_good_ones_exits_2(capsys, no_live_data):
    assert main(["AAPL", "bad symbol!", "--no-dotenv"]) == 2


def test_blocked_a_missing_env_file_exits_2(tmp_path, capsys, no_live_data):
    code = main(["AAPL", "--env-file", str(tmp_path / "nope.env"), "--live"], data=Tripwire())
    assert code == 2 and "Traceback" not in capsys.readouterr().err


def test_env_file_and_no_dotenv_cannot_be_combined(tmp_path):
    with pytest.raises(SystemExit) as ei:
        main(["AAPL", "--env-file", str(tmp_path / "x"), "--no-dotenv"])
    assert ei.value.code == 2


def test_at_least_one_symbol_is_required():
    with pytest.raises(SystemExit) as ei:
        main(["--no-dotenv"])
    assert ei.value.code == 2


# ============================ --live over scenario data ============================

def live(name, symbol, capsys, **kw):
    data = ScenarioData(load_scenario(name))
    code = main([symbol, "--live", "--no-dotenv"], data=data, clock=data.as_of, **kw)
    return code, capsys.readouterr().out


def test_live_red_flag_prints_an_event_verdict_with_every_raw_input(capsys):
    code, out = live("red-flag", "VNTR", capsys)
    assert code == 0
    assert "== Event gate check (LIVE) as of 2026-11-03 11:00:00 ET; window end 15:55 ET ==" in out
    assert "[VNTR] verdict EVENT" in out
    assert "earnings EVENT; halt EVENT" in out
    assert "- EVENT (halt/nasdaq): Nasdaq trade halt today: LUDP" in out
    assert "at 10:05:00 ET" in out
    assert "- EVENT (halt/news): trading halt today:" in out
    # raw inputs
    assert "fmp rows for VNTR: [{'symbol': 'VNTR', 'date': '2026-11-03'" in out
    assert "sec: tickers ['VNTR']; 8-K item 2.02 dates ['2026-07-23']" in out
    assert "news: 3 items" in out and "Ventara shares halted for volatility" in out
    assert "nasdaq halt items for VNTR: [{'symbol': 'VNTR', 'halt_date': '2026-11-03'" in out
    assert "'reason': 'LUDP'" in out


def test_live_clean_buy_prints_a_clear_verdict_and_no_nasdaq_items(capsys):
    code, out = live("clean-buy", "BRKO", capsys)
    assert code == 0
    assert "[BRKO] verdict CLEAR" in out and "earnings CLEAR; halt CLEAR" in out
    assert "no halt today" in out
    assert "fmp rows for BRKO: none" in out
    assert "nasdaq halt items for BRKO: none" in out
    assert "(halt/nasdaq)" not in out


def test_live_prints_the_calendar_row_counts_and_the_halt_feed_summary(capsys):
    code, out = live("clean-buy", "BRKO", capsys)
    assert "FMP calendar: ok; fetched 2026-11-03T08:30:00-05:00; range 2026-11-03..2026-11-10;" \
        in out
    assert "5 rows (cap 4000)" in out
    assert "first row 2026-11-03, last row 2026-11-10" in out
    assert "rows per day: 2026-11-03 2, 2026-11-04 1, 2026-11-05 1, 2026-11-10 1" in out
    assert "Nasdaq halt feed: ok; fetched 2026-11-03T10:59:30-05:00;" in out
    assert "3 items; codes H10 1, LUDP 1, T1 1" in out


def test_live_fetches_the_calendar_and_the_feed_once_for_many_symbols(capsys):
    data = ScenarioData(load_scenario("clean-buy"))
    calls = {"cal": 0, "halts": 0}
    orig_cal, orig_halts = data.earnings_calendar, data.halt_feed

    def cal(s, e):
        calls["cal"] += 1
        return orig_cal(s, e)

    def halts():
        calls["halts"] += 1
        return orig_halts()

    data.earnings_calendar, data.halt_feed = cal, halts
    assert main(["BRKO", "VNTR", "AAPL", "--live", "--no-dotenv"], data=data,
                clock=data.as_of) == 0
    out = capsys.readouterr().out
    assert calls == {"cal": 1, "halts": 1}
    assert out.count("verdict ") == 3


def test_live_a_symbol_the_scenario_has_no_filings_for_is_unknown_not_clear(capsys):
    code, out = live("clean-buy", "AAPL", capsys)
    assert code == 0 and "[AAPL] verdict UNKNOWN" in out and "sec: no filer found" in out


def test_blocked_live_with_a_failing_feed_prints_unknown_and_the_reason(capsys):
    data = ScenarioData(load_scenario("clean-buy"))

    def boom():
        raise RuntimeError("HTTP 503 from /rss.aspx")

    data.halt_feed = boom
    assert main(["BRKO", "--live", "--no-dotenv"], data=data, clock=data.as_of) == 0
    out = capsys.readouterr().out
    assert "Nasdaq halt feed: NOT OK: HTTP 503 from /rss.aspx" in out
    assert "[BRKO] verdict UNKNOWN" in out
    assert "UNKNOWN (halt/nasdaq): Nasdaq halt feed unavailable: HTTP 503" in out


def test_live_output_never_contains_a_key_value(monkeypatch, capsys):
    for k, v in SENTINELS.items():
        monkeypatch.setenv(k, v)
    _, out = live("red-flag", "VNTR", capsys)
    for value in SENTINELS.values():
        assert value not in out
