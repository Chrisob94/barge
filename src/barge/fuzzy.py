from __future__ import annotations


def match(query: str, text: str) -> tuple[int, list[int]] | None:
    """Subsequence match. Returns (score, matched positions), or None.

    Contiguous runs and matches at word starts score higher, so "web" ranks
    "web-01" above "new-backup".
    """
    if not query:
        return 0, []
    q = query.lower()
    t = text.lower()
    positions: list[int] = []
    score = 0
    i = 0
    prev = -2
    for ch in q:
        i = t.find(ch, i)
        if i == -1:
            return None
        bonus = 1
        if i == prev + 1:
            bonus += 4
        if i == 0 or t[i - 1] in " -_./:@":
            bonus += 3
        score += bonus
        positions.append(i)
        prev = i
        i += 1
    score -= len(t) // 8  # nudge shorter names up
    return score, positions


def rank(query: str, items: list[str]) -> list[tuple[str, list[int]]]:
    scored = []
    for n, item in enumerate(items):
        m = match(query, item)
        if m:
            scored.append((-m[0], n, item, m[1]))
    scored.sort()
    return [(item, pos) for _, _, item, pos in scored]
