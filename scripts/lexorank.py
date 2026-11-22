"""Lexicographic midpoint ranking (base-36) for sparse manual card ordering."""
from __future__ import annotations

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
BASE = len(ALPHABET)
INDEX = {ch: idx for idx, ch in enumerate(ALPHABET)}


def is_rank(value: object) -> bool:
    """True when value is a non-empty base-36 rank string not ending in '0'."""
    return isinstance(value, str) and bool(value) and not value.endswith("0") and all(ch in INDEX for ch in value)


def rank_between(before: str | None, after: str | None) -> str:
    """Return a base-36 rank string strictly between `before` and `after`.

    `None` stands for an open bound (start or end). Never emits a trailing '0',
    so every generated rank has room strictly below it.
    """
    if before is not None and not is_rank(before):
        raise ValueError(f"invalid rank bound: {before!r}")
    if after is not None and not is_rank(after):
        raise ValueError(f"invalid rank bound: {after!r}")
    if before is not None and after is not None and before >= after:
        raise ValueError(f"before ({before!r}) must be strictly less than after ({after!r})")

    out: list[str] = []
    pos = 0
    lo_done = before is None
    hi_done = after is None
    while True:
        lo = 0 if lo_done or pos >= len(before) else INDEX[before[pos]]
        hi = BASE if hi_done else (0 if pos >= len(after) else INDEX[after[pos]])
        if lo == hi:
            out.append(ALPHABET[lo])
            pos += 1
            continue
        if hi - lo > 1:
            out.append(ALPHABET[(lo + hi) // 2])
            return "".join(out)
        # Adjacent digits (hi - lo == 1): keep the lower prefix and open the upper bound
        # for subsequent positions while still respecting any remaining digits of `before`.
        out.append(ALPHABET[lo])
        hi_done = True
        pos += 1


def initial_ranks(count: int, *, before: str | None = None, after: str | None = None) -> list[str]:
    """Return `count` strictly increasing ranks within `(before, after)`."""
    if count <= 0:
        return []
    result: list[str] = []

    def fill(lo: str | None, hi: str | None, n: int) -> None:
        if n <= 0:
            return
        mid_idx = n // 2
        mid = rank_between(lo, hi)
        fill(lo, mid, mid_idx)
        result.append(mid)
        fill(mid, hi, n - mid_idx - 1)

    fill(before, after, count)
    return result
