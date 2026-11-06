from datetime import date, timedelta


def business_days_between(start: date, end: date) -> int:
    """Count weekdays from start (inclusive) to end (exclusive)."""
    days = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def add_business_days(start: date, n: int) -> date:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 6:
            n -= 1
    return d
