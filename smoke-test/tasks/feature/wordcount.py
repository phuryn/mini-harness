import argparse
import sys


def count(text: str) -> dict:
    words = text.split()
    return {"lines": len(text.splitlines()), "words": len(words), "chars": len(text)}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("file")
    args = p.parse_args(argv)
    with open(args.file) as f:
        c = count(f.read())
    print(f"{c['lines']} {c['words']} {c['chars']}")


if __name__ == "__main__":
    main()
