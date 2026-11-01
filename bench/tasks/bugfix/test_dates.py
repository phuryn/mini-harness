from datetime import date
from dates import business_days_between, add_business_days


def test_between_excludes_end():
    assert business_days_between(date(2026, 11, 2), date(2026, 11, 9)) == 5


def test_between_same_day():
    assert business_days_between(date(2026, 11, 2), date(2026, 11, 2)) == 0


def test_add_skips_weekend():
    assert add_business_days(date(2026, 11, 6), 1) == date(2026, 11, 9)


def test_add_zero():
    assert add_business_days(date(2026, 11, 7), 0) == date(2026, 11, 7)
