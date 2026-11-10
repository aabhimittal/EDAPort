import re
from collections import Counter

_NUM = re.compile(r"\b\d+(\.\d+)?\b")
_QUOTED = re.compile(r"(['\"`]).*?\1")


def group_key(msg: str) -> str:
    """Collapse a free-text message into a template so repeats group together (identifiers/numbers -> _)."""
    msg = _QUOTED.sub("'_'", msg)
    msg = _NUM.sub("N", msg)
    return " ".join(msg.split())[:120]


def group(keys) -> dict[str, int]:
    return dict(Counter(keys).most_common())
