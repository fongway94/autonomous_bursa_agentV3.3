# market_calendar.py
"""
Market calendar — accurate trading sessions + public holidays.

v3.6 multi-market change
------------------------
This module used to be 100% Bursa-Malaysia-specific. As of v3.6 it is a
THIN DISPATCHER: when the active market profile is MY it preserves the
exact v3.3 behaviour (no regression). When the active profile is US (or
any future profile), it delegates session/holiday/safe-entry logic to the
profile object itself.

The legacy module-level constants (`MYT`, `BURSA_SESSIONS`,
`MY_PUBLIC_HOLIDAYS`, `is_public_holiday`, etc.) are kept as-is so that:
    * `maintenance_reminders.py` (which scans MY_PUBLIC_HOLIDAYS for the
      "renew the holiday list" banner) keeps working.
    * Old tests pass unchanged.
    * A user staring at the file can still see exactly which Bursa
      holidays are encoded.

Session source of truth
-----------------------
MY (Bursa):
    Mon–Fri (excl. public holidays)
    08:30–09:00  Pre-opening
    09:00–12:30  Morning
    12:30–14:00  Lunch break (closed)
    14:00–14:30  Afternoon pre-open
    14:30–16:45  Afternoon
    16:45–16:50  Pre-closing
    16:50–17:00  Trading at Last

US (NYSE/NASDAQ RTH):
    Mon–Fri (excl. NYSE holidays)
    09:30–16:00  Regular Trading Hours
"""

from __future__ import annotations
from datetime import datetime, time, timezone, timedelta
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Legacy MY constants — kept for backwards compatibility
# ---------------------------------------------------------------------------

MYT = timezone(timedelta(hours=8))


class Session(NamedTuple):
    name: str
    start: time
    end: time
    fills: bool


BURSA_SESSIONS = [
    Session("PRE_OPEN_AM",     time(8, 30),  time(9, 0),  False),
    Session("MORNING",         time(9, 0),   time(12, 30), True),
    Session("LUNCH_BREAK",     time(12, 30), time(14, 0), False),
    Session("PRE_OPEN_PM",     time(14, 0),  time(14, 30), False),
    Session("AFTERNOON",       time(14, 30), time(16, 45), True),
    Session("PRE_CLOSE",       time(16, 45), time(16, 50), True),
    Session("TRADING_AT_LAST", time(16, 50), time(17, 0), True),
]


MY_PUBLIC_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01", "2025-01-29", "2025-01-30", "2025-02-11", "2025-03-18",
    "2025-03-31", "2025-04-01", "2025-05-01", "2025-05-12", "2025-06-02",
    "2025-06-07", "2025-06-27", "2025-08-31", "2025-09-05", "2025-09-16",
    "2025-10-20", "2025-12-25",
    # 2026
    "2026-01-01", "2026-02-17", "2026-02-18", "2026-03-02", "2026-03-21",
    "2026-03-22", "2026-03-23", "2026-05-01", "2026-05-27", "2026-06-02", "2026-06-06",
    "2026-06-16", "2026-08-25", "2026-08-31", "2026-09-16", "2026-11-08",
    "2026-12-25",
    # 2027 (verify before Jan 2027)
    "2027-01-01", "2027-02-06", "2027-02-07", "2027-05-01", "2027-08-31",
    "2027-09-16", "2027-12-25",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _active_market() -> str:
    """Returns the active market code, defaulting to 'MY' on any failure."""
    try:
        from market_profiles import active_market_code
        return active_market_code()
    except Exception:
        return "MY"


def _active_tz():
    code = _active_market()
    if code == "MY":
        return MYT
    try:
        from market_profiles import active_profile
        return active_profile().timezone
    except Exception:
        return MYT


def _now_local(now: datetime | None = None) -> datetime:
    """Return `now` converted to the active market's local timezone."""
    tz = _active_tz()
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        # Naive datetimes are assumed to already be in the local tz.
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


# ---------------------------------------------------------------------------
# Public calendar API (dispatched by active market)
# ---------------------------------------------------------------------------

def current_session(now: datetime | None = None) -> Session | None:
    """Return the trading Session covering `now`, or None if outside.

    For MY this returns one of the BURSA_SESSIONS NamedTuples (with `fills`
    flag). For US/other markets it returns a Session built on the fly so
    callers that inspect `.name` and `.fills` keep working.
    """
    now_local = _now_local(now)
    if not is_trading_day(now_local.date()):
        return None
    t = now_local.time()
    code = _active_market()

    if code == "MY":
        for s in BURSA_SESSIONS:
            if s.start <= t < s.end:
                return s
        return None

    # Generic dispatch via profile.sessions
    try:
        from market_profiles import active_profile
        for s in active_profile().sessions:
            if s.start <= t < s.end:
                return Session(s.name, s.start, s.end, True)
    except Exception:
        pass
    return None


def is_market_open(now: datetime | None = None) -> bool:
    s = current_session(now)
    return s is not None and s.fills


def is_safe_entry_window(now: datetime | None = None) -> bool:
    """True iff the active market is open AND there's enough time left in
    the session to make a new auto-entry sensible.

    MY: 09:00-12:30 OR 14:30-16:00 (16:00 cutoff so trade has ≥1h to develop)
    US: 09:30-15:30  (15:30 cutoff for the same reason)
    """
    now_local = _now_local(now)
    if not is_trading_day(now_local.date()):
        return False
    t = now_local.time()
    code = _active_market()

    if code == "MY":
        morning_ok = time(9, 0) <= t < time(12, 30)
        afternoon_ok = time(14, 30) <= t < time(16, 0)
        return morning_ok or afternoon_ok

    # Generic: in-session AND before the profile's safe_entry_cutoff
    try:
        from market_profiles import active_profile
        from market_profiles.base import is_within_sessions
        prof = active_profile()
        # we need a datetime for the helper; reuse now_local
        if not is_within_sessions(now_local, prof.sessions):
            return False
        return t < prof.safe_entry_cutoff
    except Exception:
        return False


def next_session_start(now: datetime | None = None) -> datetime:
    """Return the next session-start datetime (in active market TZ)."""
    now_local = _now_local(now)
    tz = now_local.tzinfo
    today = now_local.date()
    code = _active_market()

    if code == "MY":
        sessions = BURSA_SESSIONS
        # On a fresh trading day, MY conventionally surfaces 9:00 (MORNING open)
        # to the user, not 8:30 (PRE_OPEN_AM). v3.3 behaviour preserved.
        first_open_session = next(s for s in BURSA_SESSIONS if s.fills)
        first_start = first_open_session.start
    else:
        try:
            from market_profiles import active_profile
            sessions = [Session(s.name, s.start, s.end, True)
                        for s in active_profile().sessions]
        except Exception:
            sessions = BURSA_SESSIONS
        first_start = sessions[0].start

    if is_trading_day(today):
        for s in sessions:
            session_start = datetime.combine(today, s.start, tzinfo=tz)
            if session_start > now_local:
                return session_start

    d = today
    for _ in range(10):
        d = d + timedelta(days=1)
        if is_trading_day(d):
            return datetime.combine(d, first_start, tzinfo=tz)
    return now_local + timedelta(hours=24)


def is_public_holiday(d) -> bool:
    """True if `d` is a public holiday for the ACTIVE market."""
    code = _active_market()
    if code == "MY":
        if hasattr(d, "strftime"):
            d_str = d.strftime("%Y-%m-%d")
        else:
            d_str = str(d)
        return d_str in MY_PUBLIC_HOLIDAYS

    # Other markets delegate to their profile's is_holiday(datetime) callable.
    try:
        from market_profiles import active_profile
        prof = active_profile()
        # Build a midday datetime in profile TZ so we don't trip near-midnight DST cases
        if isinstance(d, str):
            d = datetime.strptime(d, "%Y-%m-%d").date()
        from datetime import datetime as _dt
        local_dt = _dt.combine(d, time(12, 0), tzinfo=prof.timezone)
        return bool(prof.is_holiday(local_dt))
    except Exception:
        return False


def is_trading_day(d) -> bool:
    if isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    if d.weekday() >= 5:
        return False
    return not is_public_holiday(d)


def market_status_text(now: datetime | None = None) -> dict:
    """Human-readable status block; respects active market timezone."""
    now_local = _now_local(now)
    tz = now_local.tzinfo
    code = _active_market()
    tz_label = "MYT" if code == "MY" else now_local.strftime("%Z") or code

    if not is_trading_day(now_local.date()):
        is_hol = is_public_holiday(now_local.date())
        market_name = "Bursa" if code == "MY" else ("NYSE/NASDAQ" if code == "US" else code)
        reason = (f"Public holiday — {market_name} closed"
                  if is_hol else f"Weekend — {market_name} closed")
        nxt = next_session_start(now_local)
        return {
            "open": False,
            "session": "CLOSED_HOLIDAY" if is_hol else "CLOSED_WEEKEND",
            "reason": reason,
            "next_event": nxt.strftime(f"%Y-%m-%d %H:%M {tz_label}"),
        }

    sess = current_session(now_local)
    if sess is None:
        pre_market_label = ("PRE_MARKET"
                            if now_local.time() < time(8, 30 if code == "MY" else 9, 30)
                            else "POST_CLOSE")
        nxt = next_session_start(now_local)
        return {
            "open": False,
            "session": pre_market_label,
            "reason": f"Outside {code} sessions",
            "next_event": nxt.strftime(f"%Y-%m-%d %H:%M {tz_label}"),
        }

    nxt = next_session_start(now_local)
    return {
        "open": sess.fills,
        "session": sess.name,
        "reason": (f"{sess.name} session "
                   f"({sess.start.strftime('%H:%M')}–"
                   f"{sess.end.strftime('%H:%M')})"),
        "next_event": nxt.strftime(f"%Y-%m-%d %H:%M {tz_label}"),
    }
