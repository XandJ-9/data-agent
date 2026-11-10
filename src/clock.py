"""Centralised time helpers. All persistent timestamps use the format
``YYYY-MM-DD HH:MM:SS`` (24-hour local time, no timezone suffix) so they are
human-readable in the database and sort correctly as strings.

Internal mechanics (lease fencing, duration measurement, backoff) still use
``time.time()`` for arithmetic; only values that reach the database go through
this module.
"""
from datetime import datetime, timedelta

_FORMAT = '%Y-%m-%d %H:%M:%S.%f'
_SENTINEL = '1970-01-01 00:00:00.000000'
SENTINEL = _SENTINEL  # public alias


def from_timestamp(ts: float) -> str:
    """Convert a Unix epoch timestamp (seconds) to DB format."""
    return datetime.fromtimestamp(ts).strftime(_FORMAT)


def now_iso() -> str:
    """Current local time as a DB-safe string."""
    return datetime.now().strftime(_FORMAT)


def now_plus(seconds: float) -> str:
    """Current local time + *seconds* as a DB-safe string."""
    return (datetime.now() + timedelta(seconds=seconds)).strftime(_FORMAT)


def iso_minus(base: str, seconds: float) -> str:
    """*base* (in _FORMAT) minus *seconds* as a DB-safe string."""
    dt = datetime.strptime(base, _FORMAT)
    return (dt - timedelta(seconds=seconds)).strftime(_FORMAT)