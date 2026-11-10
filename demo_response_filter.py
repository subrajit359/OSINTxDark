"""Standalone formatter demo using synthetic sample text only.

This module is intentionally not wired into the live Telegram relay. It is for
classroom demonstrations of basic text cleanup with fictional sample content.
"""

import re


_REQUESTED_BY_LINE = re.compile(r"(?im)^\s*requested\s+by\s*:.*(?:\n|$)")
_USERNAME = re.compile(r"(?<![\w])@[A-Za-z0-9_]{1,32}")


def filter_demo_response(text: str) -> str:
    """Remove `Requested by:` lines and @username mentions from demo text."""
    cleaned = _REQUESTED_BY_LINE.sub("", text or "")
    cleaned = _USERNAME.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return cleaned.strip()


if __name__ == "__main__":
    # Entirely fictional classroom example; no real personal data.
    sample = """Demo lookup\nRequested by: Demo Student\nCredit: @DemoCreditBot\n\n{\n    \"status\": \"SUCCESS\",\n    \"result\": \"Example item 123\"\n}\nAuto-delete in 55s."""
    print(filter_demo_response(sample))
