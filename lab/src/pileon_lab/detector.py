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

`early` uses what is observable 15 minutes after the surge's onset, but
picks the target post and the quoter from the whole surge, and the detector
only knows of most surges later (it triggers a median 39 minutes after
onset). `live` (amendment 2) uses the same signals cut at the trigger, with
the target post and quoter the detector saw then: what a live alert has.
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
from typing import TYPE_CHECKING, Any

from pileon_lab.early import early_signals
from pileon_lab.enrich import BASELINE_POSTS, LIKER_PAGES, arrivals
from pileon_lab.features import baseline
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import (
    Likers,
    Relationship,
    likers,
    post_counts,
    post_uris_before,
    profile_counts,
    relationships,
)
from pileon_lab.surges import (
    SurgeRecord,
    live_incident,
    read_surges,
    sample,
    to_incident,
)

if TYPE_CHECKING:
    from pileon_lab.study import Incident
from pileon_lab.sweep import round_sig, surge_class

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

LIVE_FAMILIES: Families = {
    "volume": {"live_lift": 1},
    "approval": {"live_likes_per_reply": -1, "live_liked_share": -1},
    "reach": {"live_follower_ratio_log10": 1, "target_followers": -1},
    "audience": {"live_follow_amplifier": 1, "live_follow_target": -1},
}

VARIANTS = {"retro": FAMILIES, "early": EARLY_FAMILIES, "live": LIVE_FAMILIES}
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


Graded = list[tuple[float, bool, float]]
"""(score, positive, weight) per graded surge."""


def weighted_auc(graded: Graded) -> float | None:
    """P(a positive outscores a negative), pairs weighted, ties counting half."""
    positives = [(s, w) for s, label, w in graded if label]
    negatives = [(s, w) for s, label, w in graded if not label]
    total = sum(w for _, w in positives) * sum(w for _, w in negatives)
    if total == 0:
        return None
    wins = sum(
        wp * wn * (1.0 if sp > sn else 0.5 if sp == sn else 0.0)
        for sp, wp in positives
        for sn, wn in negatives
    )
    return wins / total


def bootstrap_auc(
    graded: Graded, *, draws: int = 2000, seed: int = 7
) -> tuple[float, float] | None:
    """95% interval of the AUC, resampling positives and negatives separately."""
    rng = random.Random(seed)  # noqa: S311 - reproducible resampling
    positives = [g for g in graded if g[1]]
    negatives = [g for g in graded if not g[1]]
    if not positives or not negatives:
        return None
    values = []
    for _ in range(draws):
        draw = [rng.choice(positives) for _ in positives]
        draw += [rng.choice(negatives) for _ in negatives]
        value = weighted_auc(draw)
        if value is not None:
            values.append(value)
    values.sort()
    return values[int(0.025 * len(values))], values[int(0.975 * len(values)) - 1]


def recall_budget(graded: Graded, recall: float = 0.8) -> float | None:
    """The smallest (weighted) alert rate that reaches `recall`."""
    wanted = recall * sum(w for _, label, w in graded if label)
    if wanted == 0:
        return None
    caught = alerted = 0.0
    for _, label, weight in sorted(graded, key=lambda g: -g[0]):
        alerted += weight
        caught += weight if label else 0.0
        if caught >= wanted:
            return alerted / sum(w for _, _, w in graded)
    return None


Gold = dict[str, tuple[bool, float]]
"""Incident id -> (positive, inverse-probability weight)."""


def grade(reference: Reference, rows: list[Row], gold: Gold) -> list[str]:
    """Weighted AUC with a stratified bootstrap range, and alerts at each cutoff."""
    graded: Graded = []
    for row in rows:
        entry = gold.get(row["incident_id"])
        score, _ = reference.score(row)
        if entry is not None and score is not None:
            graded.append((score, entry[0], entry[1]))
    positives = sum(label for _, label, _ in graded)
    total = sum(w for _, _, w in graded)
    unscored = sum(1 for r in rows if r["incident_id"] in gold) - len(graded)
    lines = [
        (
            f"{len(graded)} graded surges ({total:.0f} weighted), "
            f"{positives} positive; {unscored} unscored"
        )
    ]
    value = weighted_auc(graded)
    if value is None:
        return [*lines, "AUC undefined (one class only)"]
    band = bootstrap_auc(graded)
    lines.append(
        f"AUC {value:.3f}" + (f" (95% {band[0]:.2f}-{band[1]:.2f})" if band else "")
    )
    wanted = sum(w for _, label, w in graded if label)
    for budget, cutoff in reference.cutoffs.items():
        alerts = [(label, w) for s, label, w in graded if s >= cutoff]
        alerted = sum(w for _, w in alerts)
        hits = sum(w for label, w in alerts if label)
        lines.append(
            f"cutoff {budget} ({cutoff:.3f}): alerts {alerted / total:.0%} of surges, "
            f"precision {hits / alerted if alerted else 0:.2f}, "
            f"recall {hits / wanted:.2f}"
        )
    needed = recall_budget(graded)
    if needed is not None:
        lines.append(f"alert rate needed for 80% recall: {needed:.0%}")
    return lines


def read_gold(text: str) -> Gold:
    """A gold file: {id: bool} or {id: {"positive": bool, "weight": float}}."""
    out: Gold = {}
    for key, value in json.loads(text).items():
        if isinstance(value, dict):
            out[key] = (bool(value["positive"]), float(value.get("weight", 1.0)))
        else:
            out[key] = (bool(value), 1.0)
    return out


def cascade_gold(
    rows: list[Row], *, min_labeled: int = 10, outcome: str = "pileon"
) -> Gold:
    """Gold from reply labels: the surge class of each judged surge."""
    return {
        row["incident_id"]: (
            surge_class(row, hostile_at=0.10, support_at=0.25) == outcome,
            1.0,
        )
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


def _lift(arrived: float | None, median_replies: float | None) -> float | None:
    if arrived is None or median_replies is None:
        return None
    return arrived / max(median_replies, 1.0)


async def _median_replies(fetcher: Fetcher, incident: Incident) -> float | None:
    """The target's median replies before onset, as `features.assemble` reads it."""
    uris = await post_uris_before(
        fetcher, incident.target_did, incident.anchor, limit=BASELINE_POSTS
    )
    if not uris:
        return None
    counts = await post_counts(fetcher, uris)
    target = (await post_counts(fetcher, [incident.target_post_uri])).get(
        incident.target_post_uri
    )
    history = [
        p
        for p in counts.values()
        if p.uri != incident.target_post_uri and p.author == incident.target_did
    ]
    base = baseline(history, is_reply=target.is_reply if target else None)
    return base.median_replies if base else None


async def live_row(surge: SurgeRecord, row: Row, fetcher: Fetcher) -> Row:
    """`row` with the `live_*` signals: everything cut at the surge's trigger.

    Only arrivals by the trigger are looked up, as a live alert would. Follows
    count when dated before onset, likes when dated by the trigger.
    """
    incident = live_incident(surge)
    trigger = surge.trigger
    assert trigger is not None  # noqa: S101 - live_incident checked it
    cut = timedelta(microseconds=trigger.at_us - surge.onset_us)
    seen = sorted(
        {r.author for r in incident.responses if r.at <= incident.anchor + cut}
    )
    target = None
    if incident.target_post_uri:
        target = (await post_counts(fetcher, [incident.target_post_uri])).get(
            incident.target_post_uri
        )
    liked = None
    if target is not None:
        liked = (
            await likers(fetcher, target.uri, max_pages=LIKER_PAGES)
            if target.likes > 0
            else Likers(complete=True, liked_at={})
        )
    to_target = await relationships(fetcher, incident.target_did, seen)
    to_amplifier: dict[str, Relationship] = {}
    amplifier_followers = None
    if incident.amplifier_did:
        to_amplifier = await relationships(fetcher, incident.amplifier_did, seen)
        amplifier_followers = (
            await profile_counts(fetcher, [incident.amplifier_did])
        ).get(incident.amplifier_did)
    found = early_signals(incident, cut, liked, (to_target, to_amplifier))
    median = await _median_replies(fetcher, incident) if target else None
    target_followers = row.get("target_followers")
    ratio = None
    if amplifier_followers is not None and isinstance(target_followers, int):
        ratio = math.log10((amplifier_followers + 1) / (target_followers + 1))
    end = to_incident(surge)
    return row | {
        "live_cut_s": cut.total_seconds(),
        "live_arrivals": found["arrivals"],
        "live_lift": _lift(found["arrivals"], median),
        "live_likes_per_reply": found.get("likes_per_reply"),
        "live_liked_share": found.get("liked_share"),
        "live_follower_ratio_log10": ratio,
        "live_follow_amplifier": found.get("follow_amplifier"),
        "live_follow_target": found.get("follow_target"),
        "live_same_target_post": incident.target_post_uri == end.target_post_uri,
        "live_same_amplifier": incident.dunk_uris == end.dunk_uris,
    }


async def live_rows(
    surges: list[SurgeRecord], rows: dict[str, Row], fetcher: Fetcher
) -> list[Row]:
    """Live rows for every surge that has a feature row, in surge order."""
    out = []
    for n, surge in enumerate(surges, 1):
        row = rows.get(surge.surge_id)
        if row is not None:
            out.append(await live_row(surge, row, fetcher))
        if n % 50 == 0:
            print(f"[live] {n}/{len(surges)}", file=sys.stderr)
    return out


def _read_rows(paths: list[Path]) -> list[Row]:
    return [
        json.loads(line) for path in paths for line in path.read_text().splitlines()
    ]


def _live_command(args: argparse.Namespace, rows: list[Row]) -> int:
    by_id = {r["incident_id"]: r for r in rows}
    wanted = [s for s in read_surges(args.surges) if s.surge_id in by_id]
    print(f"{len(wanted)} of {len(by_id)} rows have a surge", file=sys.stderr)

    async def run_live() -> list[Row]:
        fetcher = Fetcher(Cache(args.cache), offline=not args.fetch)
        try:
            return await live_rows(wanted, by_id, fetcher)
        finally:
            await fetcher.close()
            print(f"cache: {fetcher.stats}", file=sys.stderr)

    found = asyncio.run(run_live())
    args.out.write_text("".join(json.dumps(r) + "\n" for r in found))
    print(f"wrote {len(found)} rows to {args.out}")
    return 0


def _early_command(args: argparse.Namespace, rows: list[Row]) -> int:
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
    lv = sub.add_parser("live", help="add trigger-time signals (amendment 2)")
    lv.add_argument("--surges", type=Path, required=True, help="with trigger data")
    lv.add_argument("--cache", type=Path, required=True)
    lv.add_argument("--features", type=Path, nargs="+", required=True)
    lv.add_argument("--fetch", action="store_true", help="fetch what is missing")
    lv.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    rows = _read_rows(args.features)
    if args.command == "live":
        return _live_command(args, rows)
    if args.command == "early":
        return _early_command(args, rows)
    if args.command == "freeze":
        reference = freeze(rows, args.variant)
        args.out.write_text(reference.to_json() + "\n")
        print(f"froze {len(reference.signals)} signals; cutoffs {reference.cutoffs}")
        return 0
    reference = Reference.from_json(args.reference.read_text())
    if args.gold:
        gold = read_gold(args.gold.read_text())
    else:
        gold = cascade_gold(rows, min_labeled=args.min_labeled)
    print("\n".join(grade(reference, rows, gold)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
