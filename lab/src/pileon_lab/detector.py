"""A pile-on detector from structural signals only: no text, no labels.

The score of a surge is the mean of four family scores, each the mean of its
signals' signed percentiles:
- volume: replies against the target's own baseline;
- approval: likes on the target post against likes on the dunk and the replies;
- reach: the quoter's audience against the target's;
- audience: who arrives, and who they follow.

A signal's percentile is its rank in a frozen reference distribution (dev
surges, no labels used), flipped for signals where low values point to a
pile-on. Nothing is fitted: signals and their directions were fixed in
advance (direction agrees in the private study and the interim network
sample, and each is at least 0.1 from chance in both). Families weigh
equally, so four correlated volume signals count once. A threshold turns the
score into an alert; `freeze` stores cutoffs for 5%, 10% and 20% alert rates
on the reference surges.

`early` uses only what is observable 15 minutes after the quote: follower
counts, the first arrivals and their follows, and likes dated by then.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import random
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from pileon_lab.early import early_signals
from pileon_lab.enrich import LIKER_PAGES, arrivals
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import Likers, Relationship, likers, post_counts, relationships
from pileon_lab.surges import SurgeRecord, read_surges, sample, to_incident
from pileon_lab.sweep import auc, round_sig, surge_class

Families = dict[str, dict[str, int]]

FAMILIES: Families = {
    "volume": {
        "reply_lift_median": 1,
        "reply_lift_p90": 1,
        "reply_vs_record": 1,
        "window_lift_median": 1,
        "mix_shift": 1,
    },
    "approval": {
        "like_reply_ratio": -1,
        "arrivals_liked_target": -1,
        "top_response_ratio": 1,
        "dunk_ratio": 1,
    },
    "reach": {"follower_ratio_log10": 1, "target_followers": -1},
    "audience": {"follow_amplifier": 1, "follow_target": -1, "target_follows": -1},
}

EARLY_FAMILIES: Families = {
    "volume": {"early_lift": 1},
    "approval": {"early_likes_per_reply": -1, "early_liked_share": -1},
    "reach": {"follower_ratio_log10": 1, "target_followers": -1},
    "audience": {"early_follow_amplifier": 1, "early_follow_target": -1},
}

VARIANTS = {"retro": FAMILIES, "early": EARLY_FAMILIES}
BUDGETS = (0.05, 0.10, 0.20)
QUANTILES = 200

Row = dict[str, Any]


def _value(row: Row, signal: str) -> float | None:
    value = row.get(signal)
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return float(value) if math.isfinite(value) else None


def _values(rows: list[Row], signal: str) -> list[float]:
    return [v for row in rows if (v := _value(row, signal)) is not None]


def quantiles(values: list[float], *, points: int = QUANTILES) -> list[float]:
    """`points + 1` evenly spaced order statistics of `values` (min to max)."""
    ordered = sorted(values)
    if not ordered:
        return []
    last = len(ordered) - 1
    return [ordered[round(i * last / points)] for i in range(points + 1)]


def percentile(reference: list[float], value: float) -> float:
    """Mid-rank of `value` among the reference order statistics, in [0, 1]."""
    below = bisect.bisect_left(reference, value)
    upto = bisect.bisect_right(reference, value)
    return (below + upto) / (2 * len(reference))


@dataclass(frozen=True, slots=True)
class Reference:
    """Frozen order statistics per signal and alert cutoffs per budget."""

    variant: str
    signals: dict[str, list[float]]
    cutoffs: dict[str, float]

    def families(self) -> Families:
        return VARIANTS[self.variant]

    def score(self, row: Row) -> tuple[float | None, dict[str, float]]:
        """Detector score and family scores; `None` with fewer than two families."""
        parts: dict[str, float] = {}
        for family, signals in self.families().items():
            ranks = []
            for signal, sign in signals.items():
                value = _value(row, signal)
                reference = self.signals.get(signal)
                if value is None or not reference:
                    continue
                rank = percentile(reference, value)
                ranks.append(rank if sign > 0 else 1 - rank)
            if ranks:
                parts[family] = sum(ranks) / len(ranks)
        if len(parts) < 2:  # noqa: PLR2004 - one family is not a detector
            return None, parts
        return sum(parts.values()) / len(parts), parts

    def to_json(self) -> str:
        return json.dumps(
            {"variant": self.variant, "signals": self.signals, "cutoffs": self.cutoffs}
        )

    @classmethod
    def from_json(cls, text: str) -> Reference:
        data = json.loads(text)
        return cls(data["variant"], data["signals"], data["cutoffs"])


def freeze(rows: list[Row], variant: str) -> Reference:
    """Reference order statistics and alert cutoffs from unlabeled dev rows.

    Order statistics are rounded to three significant figures, so the frozen
    reference can be published without pointing at any one account.
    """
    signals = {
        signal: [round_sig(q, 3) for q in quantiles(_values(rows, signal))]
        for family in VARIANTS[variant].values()
        for signal in family
    }
    unscored = Reference(variant, {k: v for k, v in signals.items() if v}, {})
    scores = sorted(s for row in rows if (s := unscored.score(row)[0]) is not None)
    cutoffs = {
        f"{budget:.2f}": scores[
            min(len(scores) - 1, math.floor((1 - budget) * len(scores)))
        ]
        for budget in BUDGETS
    }
    return Reference(variant, unscored.signals, cutoffs)


def bootstrap_auc(
    scores: list[float], labels: list[bool], *, draws: int = 2000, seed: int = 7
) -> tuple[float, float] | None:
    """95% percentile interval of the AUC, resampling surges."""
    rng = random.Random(seed)  # noqa: S311 - reproducible resampling
    pairs = list(zip(scores, labels, strict=True))
    values = []
    for _ in range(draws):
        draw = [pairs[rng.randrange(len(pairs))] for _ in pairs]
        value = auc([s for s, _ in draw], [lab for _, lab in draw])
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def grade(reference: Reference, rows: list[Row], gold: dict[str, bool]) -> list[str]:
    """AUC (with bootstrap range) and precision/recall at the frozen cutoffs."""
    scored = []
    for row in rows:
        label = gold.get(row["incident_id"])
        score, _ = reference.score(row)
        if label is not None and score is not None:
            scored.append((score, label))
    positives = sum(label for _, label in scored)
    lines = [f"{len(scored)} graded surges, {positives} positive"]
    value = auc([s for s, _ in scored], [lab for _, lab in scored])
    if value is None:
        return [*lines, "AUC undefined (one class only)"]
    band = bootstrap_auc([s for s, _ in scored], [lab for _, lab in scored])
    lines.append(
        f"AUC {value:.3f}" + (f" (95% {band[0]:.2f}-{band[1]:.2f})" if band else "")
    )
    for budget, cutoff in reference.cutoffs.items():
        alerts = [(s, lab) for s, lab in scored if s >= cutoff]
        hits = sum(lab for _, lab in alerts)
        precision = hits / len(alerts) if alerts else 0.0
        recall = hits / positives if positives else 0.0
        lines.append(
            f"budget {budget}: {len(alerts)} alerts ({len(alerts) / len(scored):.0%}), "
            f"precision {precision:.2f}, recall {recall:.2f} ({hits}/{positives})"
        )
    return lines


def cascade_gold(
    rows: list[Row], *, min_labeled: int = 10, outcome: str = "pileon"
) -> dict[str, bool]:
    """Gold from reply labels: the surge class of each judged surge."""
    return {
        row["incident_id"]: surge_class(row, hostile_at=0.10, support_at=0.25)
        == outcome
        for row in rows
        if (row.get("labeled") or 0) >= min_labeled
    }


EARLY_CUT = timedelta(minutes=15)


async def early_rows(
    surges: list[SurgeRecord], rows: dict[str, Row], fetcher: Fetcher
) -> list[Row]:
    """Feature rows with the `early_*` signals observable 15 minutes in.

    Requests repeat the batching that enrichment used, so an offline fetcher
    serves them all from the cache.
    """
    incidents = [to_incident(s) for s in surges]
    targets = await post_counts(fetcher, [i.target_post_uri for i in incidents])
    audiences: dict[str, dict[str, Relationship]] = {}
    for amplifier in sorted({i.amplifier_did for i in incidents if i.amplifier_did}):
        people = {
            a for i in incidents if i.amplifier_did == amplifier for a in arrivals(i)
        }
        audiences[amplifier] = await relationships(fetcher, amplifier, sorted(people))
    out = []
    for incident in incidents:
        row = rows.get(incident.incident_id)
        if row is None:
            continue
        target = targets.get(incident.target_post_uri)
        liked = None
        if target is not None:
            liked = (
                await likers(fetcher, target.uri, max_pages=LIKER_PAGES)
                if target.likes > 0
                else Likers(complete=True, liked_at={})
            )
        to_target = await relationships(
            fetcher, incident.target_did, arrivals(incident)
        )
        found = early_signals(
            incident,
            EARLY_CUT,
            liked,
            (to_target, audiences.get(incident.amplifier_did, {})),
        )
        baseline = row.get("baseline_median_replies")
        lift = None
        if isinstance(baseline, int | float):
            lift = (found["arrivals"] or 0.0) / max(float(baseline), 1.0)
        out.append(
            row | {f"early_{k}": v for k, v in found.items()} | {"early_lift": lift}
        )
    return out


def _read_rows(paths: list[Path]) -> list[Row]:
    return [
        json.loads(line) for path in paths for line in path.read_text().splitlines()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fr = sub.add_parser("freeze", help="reference from unlabeled dev features")
    fr.add_argument("--features", type=Path, nargs="+", required=True)
    fr.add_argument("--variant", choices=sorted(VARIANTS), default="retro")
    fr.add_argument("--out", type=Path, required=True)
    gr = sub.add_parser("grade", help="AUC and alert precision against a gold")
    gr.add_argument("--reference", type=Path, required=True)
    gr.add_argument("--features", type=Path, nargs="+", required=True)
    gr.add_argument(
        "--gold", type=Path, help="JSON {incident id: bool}; default: cascade"
    )
    gr.add_argument("--min-labeled", type=int, default=10)
    ea = sub.add_parser("early", help="add 15-minute signals to network features")
    ea.add_argument("--surges", type=Path, required=True)
    ea.add_argument("--cache", type=Path, required=True)
    ea.add_argument("--features", type=Path, nargs="+", required=True)
    ea.add_argument("--sample-size", type=int, default=500)
    ea.add_argument("--seed", type=int, default=20261017)
    ea.add_argument("--include-warmup", action="store_true")
    ea.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = _read_rows(args.features)
    if args.command == "early":
        surges = sample(
            list(read_surges(args.surges)),
            size=args.sample_size,
            seed=args.seed,
            include_warmup=args.include_warmup,
        )

        async def run() -> list[Row]:
            fetcher = Fetcher(Cache(args.cache), offline=True)
            try:
                return await early_rows(
                    surges, {r["incident_id"]: r for r in rows}, fetcher
                )
            finally:
                await fetcher.close()
                print(f"cache: {fetcher.stats}", file=sys.stderr)

        found = asyncio.run(run())
        args.out.write_text("".join(json.dumps(r) + "\n" for r in found))
        print(f"wrote {len(found)} rows to {args.out}")
        return 0
    if args.command == "freeze":
        reference = freeze(rows, args.variant)
        args.out.write_text(reference.to_json() + "\n")
        print(f"froze {len(reference.signals)} signals; cutoffs {reference.cutoffs}")
        return 0
    reference = Reference.from_json(args.reference.read_text())
    if args.gold:
        gold = {k: bool(v) for k, v in json.loads(args.gold.read_text()).items()}
    else:
        gold = cascade_gold(rows, min_labeled=args.min_labeled)
    print("\n".join(grade(reference, rows, gold)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
