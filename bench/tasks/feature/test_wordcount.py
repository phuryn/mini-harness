from wordcount import count


def test_count():
    assert count("a b\nc\n") == {"lines": 2, "words": 3, "chars": 6}
