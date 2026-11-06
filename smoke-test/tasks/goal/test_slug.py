import pytest
from slug import slugify


@pytest.mark.parametrize("title,expected", [
    ("Hello World", "hello-world"),
    ("  Spaces   everywhere  ", "spaces-everywhere"),
    ("Crème brûlée!", "creme-brulee"),
    ("C++ & Rust: 2026", "c-rust-2026"),
    ("already-a-slug", "already-a-slug"),
    ("Multiple---dashes", "multiple-dashes"),
    ("", ""),
    ("Ünïcödé Ñame", "unicode-name"),
])
def test_slugify(title, expected):
    assert slugify(title) == expected


def test_max_length_cuts_at_word():
    assert slugify("a very long title that keeps going", max_length=15) == "a-very-long"
