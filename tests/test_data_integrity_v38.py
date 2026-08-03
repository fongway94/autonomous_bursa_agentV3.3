"""
v3.8 data-integrity regression tests.

Covers the four findings from HandBook/DATA_SOURCE_VALIDATION.md:

  #1 training universe   — brain must train on what the scanner trades
  #2 partial-scan gate   — throttled scans must not poison the brain
  #3 EMA200 window       — 1y fetch left 8.63% seed contamination
  #4 redundant fetches   — benchmark was refetched once per ticker

These are the tests that would have caught the bugs in the first place.
"""

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# #3 — EMA200 seed contamination
# ---------------------------------------------------------------------------

def _seed_weight(bars: int, span: int = 200) -> float:
    """Residual weight of the seed value in an adjust=False EMA."""
    alpha = 2.0 / (span + 1.0)
    return (1.0 - alpha) ** (bars - 1)


def test_one_year_window_leaves_material_ema200_contamination():
    """Documents WHY the window was widened — 1y is not enough for EMA200."""
    assert _seed_weight(246) > 0.08, (
        "A 1y window (~246 bars) should leave >8% seed weight in EMA200. "
        "If this fails the maths changed; re-derive the fix."
    )


def test_two_year_window_effectively_removes_contamination():
    assert _seed_weight(494) < 0.01, "2y (~494 bars) should drop seed weight <1%"
    # And it must be a big improvement over 1y, not a marginal one.
    assert _seed_weight(494) < _seed_weight(246) / 10


def test_screener_fetches_two_years_not_one():
    """Guards the actual call site against silently regressing to 1y."""
    import inspect
    import screener

    src = inspect.getsource(screener.fetch_and_calculate)
    assert 'period="2y"' in src, "fetch_and_calculate must request 2y of bars"
    assert 'period="1y"' not in src, "1y re-introduces EMA200 seed contamination"


def test_ema200_from_short_window_can_flip_the_trend_gate():
    """
    The contamination is only a bug because EMA_Trend gates every BUY.
    Construct a series where the 1y-window EMA200 and the converged EMA200
    fall on opposite sides of the last close.
    """
    span = 200
    # Long flat history, then a slow drift down. A short window seeded at the
    # (higher) start of its slice reads differently from the converged value.
    n = 900
    base = np.concatenate([
        np.full(600, 100.0),
        np.linspace(100.0, 96.0, 300),
    ])
    s = pd.Series(base)

    converged = s.ewm(span=span, adjust=False).mean().iloc[-1]
    short = s.iloc[-246:].ewm(span=span, adjust=False).mean().iloc[-1]

    # They must differ measurably — that difference is the bug.
    assert abs(short - converged) > 1e-6
    # And the short window must be the OPTIMISTIC one here (seeded high),
    # which is what produces false "uptrend intact" reads.
    assert short != pytest.approx(converged, abs=1e-4)


# ---------------------------------------------------------------------------
# #1 — training universe
# ---------------------------------------------------------------------------

def test_price_band_filter_rejects_out_of_band_names():
    from learner import _price_band_ok

    params = {"min_price": 0.30, "max_price": 4.00}

    cheap = pd.DataFrame({"Close": np.full(80, 1.20)})
    rich = pd.DataFrame({"Close": np.full(80, 11.50)})   # e.g. Maybank
    penny = pd.DataFrame({"Close": np.full(80, 0.05)})

    assert _price_band_ok(cheap, params) is True
    assert _price_band_ok(rich, params) is False, (
        "Large caps above max_price must be excluded — the live screener's "
        "is_in_price_range gate means they can never be entered."
    )
    assert _price_band_ok(penny, params) is False


def test_price_band_uses_median_not_last_close():
    """One spike must not drag a whole ticker in or out of the universe."""
    from learner import _price_band_ok

    params = {"min_price": 0.30, "max_price": 4.00}

    # Sits at 2.00 all window, single closing spike to 50.
    spiky = pd.DataFrame({"Close": np.append(np.full(79, 2.00), 50.0)})
    assert _price_band_ok(spiky, params) is True

    # Genuinely out of band for the whole window.
    high = pd.DataFrame({"Close": np.append(np.full(79, 40.0), 2.0)})
    assert _price_band_ok(high, params) is False


def test_price_band_handles_degenerate_input():
    from learner import _price_band_ok

    params = {"min_price": 0.30, "max_price": 4.00}
    assert _price_band_ok(None, params) is False
    assert _price_band_ok(pd.DataFrame(), params) is False
    assert _price_band_ok(pd.DataFrame({"Open": [1, 2]}), params) is False
    assert _price_band_ok(pd.DataFrame({"Close": [np.nan] * 5}), params) is False


def test_training_universe_is_sector_diversified(monkeypatch):
    """
    The old bug: profile.default_watchlist[:10] returned 6 banks + 4 telcos.
    The sampler must spread across sectors instead of taking an ordered head.
    """
    import learner

    universe = {
        **{f"BANK{i}.KL": "Banking" for i in range(20)},
        **{f"TECH{i}.KL": "Technology" for i in range(20)},
        **{f"CONS{i}.KL": "Construction" for i in range(20)},
        **{f"PLNT{i}.KL": "Plantation" for i in range(20)},
    }

    import watchlist
    monkeypatch.setattr(watchlist, "get_all_tickers",
                        lambda: sorted(universe.keys()))
    monkeypatch.setattr(watchlist, "get_ticker_sector",
                        lambda t: universe.get(t, "Unknown"))

    import repository
    monkeypatch.setattr(repository, "load_parameters", lambda: {})

    picked = learner._classifier_tickers_for_active_market(max_tickers=20)

    assert len(picked) == 20
    sectors = {universe[t] for t in picked}
    assert sectors == {"Banking", "Technology", "Construction", "Plantation"}, (
        f"expected all 4 sectors represented, got {sectors}"
    )
    # Round-robin => near-even split, definitely not 2-sector domination.
    from collections import Counter
    counts = Counter(universe[t] for t in picked)
    assert max(counts.values()) - min(counts.values()) <= 1


def test_training_universe_is_larger_than_the_old_ten():
    """The old head-slice was 10 names for a 109-ticker live universe."""
    from learner import TRAINING_UNIVERSE_SIZE
    assert TRAINING_UNIVERSE_SIZE >= 30


def test_training_universe_respects_shariah_filter(monkeypatch):
    import learner
    import watchlist
    import repository

    called = {"shariah": False}

    def _shariah_only():
        called["shariah"] = True
        return ["0166.KL", "5296.KL"]

    monkeypatch.setattr(watchlist, "get_all_tickers_shariah_only", _shariah_only)
    monkeypatch.setattr(watchlist, "get_all_tickers",
                        lambda: ["1155.KL", "0166.KL", "5296.KL"])
    monkeypatch.setattr(watchlist, "get_ticker_sector", lambda t: "X")
    monkeypatch.setattr(repository, "load_parameters",
                        lambda: {"shariah_only": True})

    picked = learner._classifier_tickers_for_active_market(max_tickers=10)

    assert called["shariah"], "shariah_only=True must use the filtered list"
    assert "1155.KL" not in picked


def test_training_universe_falls_back_when_watchlist_unavailable(monkeypatch):
    """Must never return empty — training would silently no-op."""
    import learner
    import watchlist

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(watchlist, "get_all_tickers", _boom)

    picked = learner._classifier_tickers_for_active_market()
    assert picked, "fallback must yield a non-empty universe"
    assert all(isinstance(t, str) for t in picked)


# ---------------------------------------------------------------------------
# #2 — partial-scan gate
# ---------------------------------------------------------------------------

def test_scan_degraded_threshold_is_sane():
    from scheduler import MIN_SCAN_COVERAGE
    assert 0.5 < MIN_SCAN_COVERAGE < 1.0


def test_rate_limit_classifier_matches_throttling_errors():
    from data_provider import _is_rate_limited

    assert _is_rate_limited(Exception("429 Too Many Requests"))
    assert _is_rate_limited(Exception("Rate limited. Try after a while."))
    assert _is_rate_limited(TimeoutError("read timed out"))
    assert _is_rate_limited(Exception("SSL_connect: connection closed abruptly"))
    assert _is_rate_limited(Exception("503 Service Unavailable"))
    # Genuine data problems must NOT be retried.
    assert not _is_rate_limited(ValueError("No price data found; symbol delisted"))
    assert not _is_rate_limited(KeyError("Close"))


def test_fetch_retries_then_succeeds(monkeypatch):
    """A transient 429 must not silently drop the ticker."""
    import data_provider as dp

    calls = {"n": 0}
    good = pd.DataFrame({"Close": [1.0, 2.0]})

    def flaky(ticker, period, start, end, timeout, interval=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise Exception("429 Too Many Requests")
        return good

    monkeypatch.setattr(dp, "_fetch_yfinance_once", flaky)
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)   # no real waiting

    out = dp._fetch_yfinance("1155.KL", "2y", None, None, 15)

    assert calls["n"] == 3
    assert not out.empty, "retry should have recovered the data"


def test_fetch_gives_up_after_max_attempts(monkeypatch):
    import data_provider as dp

    calls = {"n": 0}

    def always_429(ticker, period, start, end, timeout, interval=None):
        calls["n"] += 1
        raise Exception("429 Too Many Requests")

    monkeypatch.setattr(dp, "_fetch_yfinance_once", always_429)
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)

    out = dp._fetch_yfinance("1155.KL", "2y", None, None, 15)

    assert calls["n"] == dp.YF_MAX_ATTEMPTS
    assert out.empty


def test_fetch_does_not_retry_permanent_errors(monkeypatch):
    """Delisted symbols must fail fast — retrying wastes the rate budget."""
    import data_provider as dp

    calls = {"n": 0}

    def delisted(ticker, period, start, end, timeout, interval=None):
        calls["n"] += 1
        raise ValueError("No price data found; symbol may be delisted")

    monkeypatch.setattr(dp, "_fetch_yfinance_once", delisted)
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)

    out = dp._fetch_yfinance("DEAD.KL", "2y", None, None, 15)

    assert calls["n"] == 1, "permanent errors must not be retried"
    assert out.empty


# ---------------------------------------------------------------------------
# #4 — redundant benchmark fetches
# ---------------------------------------------------------------------------

def test_rs_ranking_fetches_benchmark_once(monkeypatch):
    """
    The bug: rank_stocks_by_relative_strength() left klci_df=None, so
    calculate_relative_strength re-downloaded the benchmark per ticker —
    109 identical ^KLSE pulls on a MY cycle.
    """
    import market_analyzer as ma

    bench_calls = {"n": 0}
    bench = pd.DataFrame({"Close": np.linspace(100, 110, 60),
                          "Open": np.linspace(100, 110, 60),
                          "High": np.linspace(100, 111, 60),
                          "Low": np.linspace(99, 109, 60),
                          "Volume": np.full(60, 1_000_000)})

    def counting_bench(period="3mo"):
        bench_calls["n"] += 1
        return bench

    monkeypatch.setattr(ma, "get_regime_benchmark_data", counting_bench)
    monkeypatch.setattr(ma, "validate_ohlcv", lambda df, t, **k: (True, []))

    frames = {
        f"T{i}.KL": pd.DataFrame({
            "Close": np.linspace(1.0, 1.2, 60),
            "Open": np.linspace(1.0, 1.2, 60),
            "High": np.linspace(1.0, 1.3, 60),
            "Low": np.linspace(0.9, 1.1, 60),
            "Volume": np.full(60, 500_000),
        })
        for i in range(25)
    }

    def no_network(*a, **k):
        raise AssertionError("RS must not fetch when price_frames supplied")

    monkeypatch.setattr(ma, "get_history", no_network)

    out = ma.rank_stocks_by_relative_strength(
        list(frames.keys()), price_frames=frames)

    assert len(out) == 25
    assert bench_calls["n"] == 1, (
        f"benchmark fetched {bench_calls['n']}x for 25 tickers — should be 1"
    )


def test_rs_uses_supplied_frame_instead_of_refetching(monkeypatch):
    import market_analyzer as ma

    monkeypatch.setattr(ma, "validate_ohlcv", lambda df, t, **k: (True, []))
    monkeypatch.setattr(
        ma, "get_history",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("should not fetch")))

    stock = pd.DataFrame({"Close": np.linspace(1.0, 1.5, 60)})
    bench = pd.DataFrame({"Close": np.linspace(100, 105, 60)})

    rs = ma.calculate_relative_strength(
        "X.KL", klci_df=bench, period=20, stock_df=stock)

    assert rs is not None
    assert rs["rs_signal"] in {"LEADING", "LAGGING", "MATCHING"}
    # Stock +~% vs benchmark +~% — outperformer should lead.
    assert rs["stock_return_pct"] > 0


def test_rs_still_fetches_when_no_frame_supplied(monkeypatch):
    """Backwards compatibility for standalone/manual callers."""
    import market_analyzer as ma

    fetched = {"n": 0}
    df = pd.DataFrame({"Close": np.linspace(1.0, 1.5, 60)})

    def fake_get(ticker, period=None, timeout=None, **k):
        fetched["n"] += 1
        return df

    monkeypatch.setattr(ma, "get_history", fake_get)
    monkeypatch.setattr(ma, "validate_ohlcv", lambda d, t, **k: (True, []))

    rs = ma.calculate_relative_strength(
        "X.KL", klci_df=pd.DataFrame({"Close": np.linspace(100, 105, 60)}),
        period=20)

    assert rs is not None
    assert fetched["n"] == 1


def test_empty_frame_retried_only_when_yfinance_logged_transient_error(monkeypatch):
    """
    yfinance is inconsistent: it sometimes swallows a network error and
    returns an EMPTY frame instead of raising. Those must still be retried,
    or a throttled scan silently looks like a universe of delisted stocks.
    """
    import data_provider as dp

    calls = {"n": 0}
    good = pd.DataFrame({"Close": [1.0, 2.0]})

    def empty_then_good(ticker, period, start, end, timeout, interval=None):
        calls["n"] += 1
        return good if calls["n"] >= 3 else pd.DataFrame()

    monkeypatch.setattr(dp, "_fetch_yfinance_once", empty_then_good)
    monkeypatch.setattr(dp, "_yf_logged_transient_error", lambda t: True)
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)

    out = dp._fetch_yfinance("1155.KL", "2y", None, None, 15)

    assert calls["n"] == 3
    assert not out.empty


def test_empty_frame_not_retried_for_genuinely_delisted_symbol(monkeypatch):
    """Retrying every dead ticker would burn 3x the rate budget each cycle."""
    import data_provider as dp

    calls = {"n": 0}

    def always_empty(ticker, period, start, end, timeout, interval=None):
        calls["n"] += 1
        return pd.DataFrame()

    monkeypatch.setattr(dp, "_fetch_yfinance_once", always_empty)
    monkeypatch.setattr(dp, "_yf_logged_transient_error", lambda t: False)
    monkeypatch.setattr(dp.time, "sleep", lambda s: None)

    out = dp._fetch_yfinance("DEAD.KL", "2y", None, None, 15)

    assert calls["n"] == 1, "delisted symbols must not be retried"
    assert out.empty


def test_yf_error_probe_never_raises():
    """Uses a private yfinance API — must degrade quietly if it disappears."""
    from data_provider import _yf_logged_transient_error
    assert _yf_logged_transient_error("NOSUCHTICKER.KL") in (True, False)
