"""Agreement statistics for comparing labelers."""

from __future__ import annotations

from collections import Counter

from pileon_lab.codebook import GOLD_LABELS


def cohens_kappa(pairs: list[tuple[str, str]]) -> float:
    """Cohen's (nominal) kappa over (rater_a, rater_b) label pairs."""
    n = len(pairs)
    if n == 0:
        return 0.0
    labels = {label for pair in pairs for label in pair}
    observed = sum(1 for a, b in pairs if a == b) / n
    ca = Counter(a for a, _ in pairs)
    cb = Counter(b for _, b in pairs)
    expected = sum((ca[label] / n) * (cb[label] / n) for label in labels)
    if expected >= 1.0:
        return 1.0
    return (observed - expected) / (1.0 - expected)


def weighted_kappa(
    pairs: list[tuple[str, str]],
    *,
    order: tuple[str, ...] = GOLD_LABELS,
    power: int = 2,
) -> float:
    """Ordinal weighted kappa (quadratic by default) over an ordered scale.

    Pairs with a label outside `order` are ignored; neighboring classes are
    penalized less than distant ones.
    """
    index = {label: i for i, label in enumerate(order)}
    usable = [(a, b) for a, b in pairs if a in index and b in index]
    n = len(usable)
    k = len(order)
    if n == 0 or k < 2:  # noqa: PLR2004
        return 0.0

    def distance(a: str, b: str) -> float:
        return (abs(index[a] - index[b]) / (k - 1)) ** power

    ca = Counter(a for a, _ in usable)
    cb = Counter(b for _, b in usable)
    observed = sum(distance(a, b) for a, b in usable) / n
    expected = sum(
        distance(x, y) * (ca[x] / n) * (cb[y] / n) for x in order for y in order
    )
    if expected == 0:
        return 1.0
    return 1.0 - observed / expected
