"""
Banking-day calendar of a bank in Munich (TARGET2 closing days + German / Bavarian holidays + 24/31 Dec) used for
UCP 600 Art. 14(b) / URDG 758 Art. 20 examination periods, value dates and maturity dates.
"""

from datetime import date, timedelta
from functools import lru_cache
from typing import Set


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (Anonymous Gregorian algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def target_holidays(year: int) -> Set[date]:
    """TARGET2 closing days (EUR settlement)."""
    easter = _easter(year)
    return {
        date(year, 1, 1),
        easter - timedelta(days=2),  # Good Friday
        easter + timedelta(days=1),  # Easter Monday
        date(year, 5, 1),
        date(year, 12, 25),
        date(year, 12, 26),
    }


@lru_cache(maxsize=64)
def munich_bank_holidays(year: int) -> Set[date]:
    """Days the bank in Munich is closed: German public holidays, Bavarian holidays observed in Munich, and the
    customary bank closing days 24 and 31 December. UCP 600 Art. 2 / URDG 758 Art. 2 count only days on which
    the bank is regularly open at the place of examination."""
    easter = _easter(year)
    return target_holidays(year) | {
        easter + timedelta(days=39),  # Ascension Day
        easter + timedelta(days=50),  # Whit Monday
        date(year, 10, 3),  # Day of German Unity
        date(year, 1, 6),  # Epiphany (Bavaria)
        easter + timedelta(days=60),  # Corpus Christi (Bavaria)
        date(year, 8, 15),  # Assumption Day (Bavaria, observed in Munich)
        date(year, 11, 1),  # All Saints' Day (Bavaria)
        date(year, 12, 24),
        date(year, 12, 31),
    }


def is_banking_day(d: date) -> bool:
    return d.weekday() < 5 and d not in munich_bank_holidays(d.year)


def next_banking_day(d: date) -> date:
    while not is_banking_day(d):
        d += timedelta(days=1)
    return d


def add_banking_days(d: date, days: int) -> date:
    """Adds banking days, e.g. the 5-banking-day examination period following presentation day."""
    current = d
    added = 0
    while added < days:
        current += timedelta(days=1)
        if is_banking_day(current):
            added += 1
    return current


def banking_days_between(start: date, end: date) -> int:
    """Number of banking days after `start` up to and including `end`."""
    if end <= start:
        return 0
    n = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if is_banking_day(d):
            n += 1
    return n
