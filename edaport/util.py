import hashlib
import json


def canonical(obj) -> bytes:
    """Stable JSON bytes: the basis of every digest in EDAPort."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def est_tokens(obj) -> int:
    # shortcut: ~4 bytes/token heuristic, swap for a real tokenizer if budgets need to be exact
    return len(canonical(obj)) // 4 + 1
