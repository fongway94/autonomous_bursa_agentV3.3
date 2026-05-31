from ui_mode_helpers import (
    available_trading_modes_for_profile,
    trading_mode_label,
    effective_scheduler_interval_sec,
    intraday_unavailable_message,
    mode_specific_scanner_columns,
    intraday_settings_rows,
)


def test_available_modes_supports_intraday():
    assert available_trading_modes_for_profile(True) == ["SWING", "INTRADAY"]


def test_available_modes_no_intraday():
    assert available_trading_modes_for_profile(False) == ["SWING"]


def test_trading_mode_label_intraday():
    assert "INTRADAY" in trading_mode_label("INTRADAY")


def test_effective_interval_intraday():
    assert effective_scheduler_interval_sec(
        supports_intraday=True,
        trading_mode="INTRADAY",
        swing_cycle_sec=3600,
        intraday_cycle_sec=300,
    ) == 300


def test_effective_interval_falls_back_to_swing_when_market_unsupported():
    assert effective_scheduler_interval_sec(
        supports_intraday=False,
        trading_mode="INTRADAY",
        swing_cycle_sec=3600,
        intraday_cycle_sec=300,
    ) == 3600


def test_intraday_unavailable_message_for_my_market():
    msg = intraday_unavailable_message(
        market_code="MY",
        supports_intraday=False,
        moomoo_available=False,
    )
    assert msg is not None
    assert "MY" in msg or "Bursa" in msg


def test_intraday_unavailable_message_for_us_without_opend():
    msg = intraday_unavailable_message(
        market_code="US",
        supports_intraday=True,
        moomoo_available=False,
    )
    assert msg is not None
    assert "OpenD" in msg


def test_intraday_unavailable_message_none_when_ready():
    assert intraday_unavailable_message(
        market_code="US",
        supports_intraday=True,
        moomoo_available=True,
    ) is None


def test_mode_specific_scanner_columns_intraday_are_simpler():
    cols = mode_specific_scanner_columns("INTRADAY")
    assert "change_pct" not in cols
    assert "rsi" not in cols
    assert "volume" in cols
    assert "vol_ratio" in cols


def test_intraday_settings_rows_contains_watchlist_and_force_flat():
    rows = intraday_settings_rows(["TNA", "TQQQ"])
    as_dict = dict(rows)
    assert as_dict["Universe"] == "TNA, TQQQ"
    assert as_dict["Force-flat"] == "15:55 ET"
