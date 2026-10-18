"""The trawl: a pile-on-rich frame for grading the frozen detectors.

Pre-registered in `lab/PREREGISTRATION.md` (amendment 1). Stage C's sample
holds too few pile-ons to grade a detector, so the trawl looks where the
detector looks: it scores every eligible held-out surge in the full detection
run on the frozen retro detector's cheap signals, draws a judged sample from
each score tier, and weights each drawn surge by its tier's sampling fraction.

1. `score`: a surge is eligible when its onset is on or after 2026-08-27, it
   is not warm-up, it has a target post and at least 10 recorded replies and
   quotes, and neither its target nor its amplifier is in the dev data. Each
   eligible surge gets the frozen retro score on what batched post and
   profile counts give: reach (quoter and target followers) and approval
   (likes per reply on the target post, dunk likes against target likes).
2. `plan`: tiers by that score (top 0.5%, the next 1.5%, the next 8%, the
   rest) after carving out a rally tier (the 5% with the most approving
   approval family) and a seeded draw from each tier. Writes the drawn
   surges' detector lines for phase 2; `report` weights each judged surge by
   its tier's size over the judged surges in the tier.
3. `union`: one plan and one surge file over several plans, so each surge is
   built and judged once in a single shuffled thread packet.
4. `report`: per tier, the judges' pooled in-context classes with Wilson
   intervals, the trend and rally tests, and gold files for `detector grade`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import NormalDist
from typing import TYPE_CHECKING, Any

from pileon_lab.confirm import wilson
from pileon_lab.detector import Reference
from pileon_lab.enrich import incident_posts
from pileon_lab.evalset import JUDGES
from pileon_lab.features import build_row, like_signals
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.heldout import INTERIM_END, Dev, amplifier_of, dev_people, pooled_class
from pileon_lab.sources import post_counts, profile_counts
from pileon_lab.study import load_incidents
from pileon_lab.surges import (
    SurgeRecord,
    copy_surges,
    read_surges,
    sample,
    to_incident,
)
from pileon_lab.threads import read_judgment

if TYPE_CHECKING:
    from collections.abc import Iterable

Row = dict[str, Any]

TRAWL_SEED = 20261019
MIN_RESPONSES = 10
CHEAP_SIGNALS = (
    "like_reply_ratio",
    "dunk_ratio",
    "follower_ratio_log10",
    "target_followers",
)
SCORE_TIERS = (("top", 0.995), ("high", 0.98), ("mid", 0.90))
"""Each tier holds the surges at or above this quantile of the phase-1 score."""
RALLY_SHARE = 0.05
TIERS = ("top", "high", "mid", "low", "rally")
DRAWS = {"top": 100, "high": 50, "mid": 50, "low": 60, "rally": 40}
TREND = {"top": 3, "high": 2, "mid": 1, "low": 0}
CLASSES = ("pileon", "crossfire", "rally", "spotlight")
BATCH = 2000


def exclusion(surge: SurgeRecord, dev: Dev) -> str | None:
    """Why a surge is outside the trawl's frame, or `None` if it is eligible."""
    if surge.warmup:
        return "warm-up"
    if not surge.target_posts:
        return "no target post"
    if surge.onset < INTERIM_END:
        return "interim window"
    if surge.replies + surge.quotes < MIN_RESPONSES:
        return "fewer than 10 replies and quotes"
    if surge.target in dev.people or (amplifier_of(surge) or "") in dev.people:
        return "overlaps dev"
    return None


async def cheap_rows(surges: list[SurgeRecord], fetcher: Fetcher) -> list[Row]:
    """Phase-1 rows: the frozen feature code fed only batched counts."""
    incidents = [to_incident(s) for s in surges]
    counts = await post_counts(fetcher, incident_posts(incidents))
    people = {i.target_did for i in incidents}
    people |= {i.amplifier_did for i in incidents if i.amplifier_did}
    followers = await profile_counts(fetcher, sorted(people))
    rows = []
    for bare in incidents:
        target_post = counts.get(bare.target_post_uri)
        incident = replace(bare, target_followers=followers.get(bare.target_did))
        likes = like_signals(
            incident,
            target_post=target_post,
            response_counts={},
            dunk_counts=[counts[u] for u in incident.dunk_uris if u in counts],
            liked=None,
        )
        row = asdict(
            build_row(
                incident,
                target_post=target_post,
                history=[],
                ally=None,
                to_target={},
                to_amplifier={},
                amplifier_followers=(
                    followers.get(incident.amplifier_did)
                    if incident.amplifier_did
                    else None
                ),
                pre_arrivals=None,
                likes=likes,
            )
        )
        keep = ("incident_id", "target_post_found", "replies", "likes", *CHEAP_SIGNALS)
        rows.append({k: row[k] for k in keep})
    return rows


def _at(ordered: list[float], q: float) -> float:
    return ordered[min(len(ordered) - 1, math.floor(q * len(ordered)))]


def tiers(rows: Iterable[Row], reference: Reference) -> tuple[dict[str, str], Row]:
    """Each scored surge's tier, and the cutoffs that made them."""
    scores: dict[str, float] = {}
    approval: dict[str, float] = {}
    for row in rows:
        score, parts = reference.score(row)
        if score is None:
            continue
        scores[row["incident_id"]] = score
        if "approval" in parts:
            approval[row["incident_id"]] = parts["approval"]
    ordered = sorted(scores.values())
    cuts = {tier: _at(ordered, q) for tier, q in SCORE_TIERS}
    rally_cut = _at(sorted(approval.values()), RALLY_SHARE) if approval else -1.0
    out = {}
    for surge_id, score in scores.items():
        if approval.get(surge_id, math.inf) <= rally_cut:
            out[surge_id] = "rally"
            continue
        out[surge_id] = next(
            (tier for tier, _ in SCORE_TIERS if score >= cuts[tier]), "low"
        )
    return out, {"score": cuts, "rally_approval": rally_cut}


def draw(assigned: dict[str, str], *, seed: int = TRAWL_SEED) -> dict[str, Row]:
    """A seeded draw from every tier, each surge weighted tier size / draw."""
    members: dict[str, list[str]] = defaultdict(list)
    for surge_id in sorted(assigned):
        members[assigned[surge_id]].append(surge_id)
    plan: dict[str, Row] = {}
    for tier in TIERS:
        pool = members.get(tier, [])
        size = min(DRAWS[tier], len(pool))
        rng = random.Random(f"{seed}:{tier}")  # noqa: S311 - reproducible draw
        for surge_id in sorted(rng.sample(pool, size)):
            plan[surge_id] = {
                "stratum": tier,
                "weight": len(pool) / size,
                "overlap": False,
            }
    return plan


def union(plans: dict[str, dict[str, Row]]) -> dict[str, Row]:
    """One plan over several: a surge's stratum names every plan that drew it."""
    out: dict[str, Row] = {}
    for name, planned in plans.items():
        for surge_id, entry in planned.items():
            if entry["stratum"] == "unsampled":
                continue
            label = f"{name}:{entry['stratum']}"
            if surge_id in out:
                out[surge_id]["stratum"] += f"+{label}"
            else:
                out[surge_id] = {"stratum": label, "weight": 1.0, "overlap": False}
    return out


@dataclass(frozen=True, slots=True)
class Judged:
    """Pooled classes of fully judged surges, and surges a judge left unfinished.

    A class is `None` when the pooled labels number fewer than 10. An
    incomplete surge goes back to its judge; it is never dropped.
    """

    classes: dict[str, str | None]
    incomplete: frozenset[str]


def pooled_classes(threads_dir: Path) -> Judged:
    """Each packet surge's class over both judges' in-context labels."""
    key: dict[str, Any] = json.loads((threads_dir / "key.json").read_text())
    judged = [read_judgment(threads_dir / f"{j}.jsonl") for j in JUDGES]
    classes: dict[str, str | None] = {}
    incomplete = set()
    for packet_id, entry in key.items():
        if not all(packet_id in j.verdicts for j in judged):
            incomplete.add(entry["surge"])
            continue
        labels = [r for j in judged for (s, _), r in j.posts.items() if s == packet_id]
        classes[entry["surge"]] = pooled_class(labels)
    return Judged(classes, frozenset(incomplete))


def trend_p(groups: list[tuple[int, int, float]]) -> float | None:
    """One-sided Cochran-Armitage p that the share rises with the score.

    `groups` holds (positives, judged, score) per tier; `None` when every
    judged surge is positive or none is.
    """
    total = sum(n for _, n, _ in groups)
    hits = sum(x for x, _, _ in groups)
    if total == 0 or hits in {0, total}:
        return None
    p = hits / total
    t = sum(s * (x - n * p) for x, n, s in groups)
    spread = sum(n * s * s for _, n, s in groups)
    spread -= sum(n * s for _, n, s in groups) ** 2 / total
    variance = p * (1 - p) * spread
    if variance <= 0:
        return None
    return 1 - NormalDist().cdf(t / math.sqrt(variance))


def fisher_greater(a: int, n1: int, c: int, n2: int) -> float:
    """One-sided Fisher exact p that group 1's share (a / n1) exceeds group 2's."""
    total, hits = n1 + n2, a + c
    denominator = math.comb(total, n1)
    return (
        sum(
            math.comb(hits, k) * math.comb(total - hits, n1 - k)
            for k in range(a, min(hits, n1) + 1)
        )
        / denominator
    )


def _share(k: int, n: int) -> str:
    if not n:
        return "-"
    low, high = wilson(k, n)
    return f"{k}/{n} {k / n:.0%} ({low:.0%}-{high:.0%})"


def _p(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3g}"


HOSTILE = frozenset({"pileon", "crossfire"})
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 7


def frame_rate(outcomes: dict[str, list[bool]], sizes: dict[str, int]) -> float | None:
    """The frame's positive rate: tier shares weighted by tier size."""
    frame = sum(sizes.get(t, 0) for t in outcomes if outcomes[t])
    if not frame:
        return None
    return (
        sum(sizes.get(t, 0) * sum(o) / len(o) for t, o in outcomes.items() if o) / frame
    )


def enrichment(
    outcomes: dict[str, list[bool]], sizes: dict[str, int]
) -> tuple[float | None, tuple[float, float] | None, float]:
    """Top-tier share over the frame rate, its stratified bootstrap interval,
    and the share of bootstrap draws where the ratio is undefined."""

    def ratio(sample: dict[str, list[bool]]) -> float | None:
        rate = frame_rate(sample, sizes)
        top = sample.get("top") or []
        if not rate or not top:
            return None
        return (sum(top) / len(top)) / rate

    point = ratio(outcomes)
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - reproducible bootstrap
    draws = []
    undefined = 0
    for _ in range(BOOTSTRAP_DRAWS):
        sample = {t: rng.choices(o, k=len(o)) if o else [] for t, o in outcomes.items()}
        value = ratio(sample)
        if value is None:
            undefined += 1
        else:
            draws.append(value)
    draws.sort()
    band = (
        (draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1])
        if draws
        else None
    )
    return point, band, undefined / BOOTSTRAP_DRAWS


def _tally(
    planned: dict[str, Row], judged: Judged
) -> tuple[dict[str, Counter[str]], dict[str, dict[str, str]]]:
    by_tier: dict[str, Counter[str]] = {t: Counter() for t in TIERS}
    kinds: dict[str, dict[str, str]] = {t: {} for t in TIERS}
    for surge_id, entry in planned.items():
        tally = by_tier[entry["stratum"]]
        tally["drawn"] += 1
        if surge_id in judged.incomplete:
            tally["verdict missing"] += 1
        elif surge_id not in judged.classes:
            tally["no packet"] += 1
        elif (kind := judged.classes[surge_id]) is None:
            tally["fewer than 10 labels"] += 1
        else:
            tally["judged"] += 1
            tally[kind] += 1
            kinds[entry["stratum"]][surge_id] = kind
    return by_tier, kinds


LOSSES = ("verdict missing", "no packet", "fewer than 10 labels")


def report(
    planned: dict[str, Row], judged: Judged, sizes: dict[str, int]
) -> tuple[list[str], dict[str, dict[str, Row]]]:
    """Per-tier results, the pre-registered tests, and gold files.

    Gold weights are tier size over judged surges in the tier, so attrition
    within a tier is spread over the surges that remain.
    """
    by_tier, kinds = _tally(planned, judged)
    gold: dict[str, dict[str, Row]] = {"primary": {}, "hostile": {}}
    for tier, members in kinds.items():
        for surge_id, kind in members.items():
            weight = sizes.get(tier, 0) / len(members)
            gold["primary"][surge_id] = {"positive": kind == "pileon", "weight": weight}
            gold["hostile"][surge_id] = {"positive": kind in HOSTILE, "weight": weight}
    lines = [
        "tier    frame  drawn judged  pile-on              hostile              rally"
    ]
    for tier in TIERS:
        t = by_tier[tier]
        n = t["judged"]
        lines.append(
            f"{tier:<6} {sizes.get(tier, 0):>6} {t['drawn']:>6} {n:>6}  "
            f"{_share(t['pileon'], n):<20} "
            f"{_share(t['pileon'] + t['crossfire'], n):<20} {_share(t['rally'], n)}"
        )
        lost = {k: t[k] for k in LOSSES if t[k]}
        if lost:
            lines.append(f"       lost: {lost}")
    missing = sum(t["verdict missing"] for t in by_tier.values())
    if missing:
        lines.append(f"INCOMPLETE: {missing} surges lack a verdict; rerun their judges")
    for name, wanted in (("pile-on", frozenset({"pileon"})), ("hostile", HOSTILE)):
        groups = [
            (sum(by_tier[t][k] for k in wanted), by_tier[t]["judged"], float(s))
            for t, s in TREND.items()
        ]
        outcomes = {t: [k in wanted for k in kinds[t].values()] for t in TIERS}
        rate = frame_rate(outcomes, sizes)
        point, band, undefined = enrichment(outcomes, sizes)
        lines.append(f"{name}: trend test (one-sided) p {_p(trend_p(groups))}")
        lines.append(
            "  weighted rate in the frame "
            + ("-" if rate is None else f"{rate:.2%}")
            + "; enrichment top / frame "
            + ("undefined" if point is None else f"{point:.1f}x")
            + (f" (95% {band[0]:.1f}-{band[1]:.1f}x" if band else " (no interval")
            + f", {undefined:.0%} of draws undefined)"
        )
    rally, low, top = by_tier["rally"], by_tier["low"], by_tier["top"]
    if rally["judged"] and low["judged"]:
        p = fisher_greater(rally["rally"], rally["judged"], low["rally"], low["judged"])
        lines.append(f"rally tag: rally share above the low tier's, p {_p(p)}")
    if rally["judged"] and top["judged"]:
        p = fisher_greater(
            top["pileon"] + top["crossfire"],
            top["judged"],
            rally["pileon"] + rally["crossfire"],
            rally["judged"],
        )
        lines.append(f"rally tag: hostile share below the top tier's, p {_p(p)}")
    return lines, gold


def _score(args: argparse.Namespace) -> int:
    interim = sample(
        list(read_surges(args.interim_surges)),
        size=args.interim_sample_size,
        seed=args.interim_seed,
    )
    dev = dev_people(interim, load_incidents(args.study_db, persons_only=False))
    excluded: Counter[str] = Counter()
    frame = []
    for surge in read_surges(args.surges):
        reason = exclusion(surge, dev)
        if reason is None:
            frame.append(surge)
        else:
            excluded[reason] += 1
    print(f"eligible {len(frame)}; excluded {dict(excluded)}", file=sys.stderr)

    async def run() -> int:
        fetcher = Fetcher(Cache(args.cache))
        written = 0
        try:
            with args.out.open("w") as out:
                for start in range(0, len(frame), BATCH):
                    rows = await cheap_rows(frame[start : start + BATCH], fetcher)
                    for row in rows:
                        out.write(json.dumps(row) + "\n")
                    written += len(rows)
                    print(
                        f"[trawl] {written}/{len(frame)}", file=sys.stderr, flush=True
                    )
        finally:
            await fetcher.close()
            print(f"cache: {fetcher.stats}", file=sys.stderr)
        return written

    written = asyncio.run(run())
    print(f"wrote {written} rows to {args.out}", file=sys.stderr)
    return 0


def _rows(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _plan(args: argparse.Namespace) -> int:
    reference = Reference.from_json(args.reference.read_text())
    rows = _rows(args.phase1)
    assigned, cuts = tiers(rows, reference)
    sizes = Counter(assigned.values())
    planned = draw(assigned)
    args.dir.mkdir(parents=True, exist_ok=True)
    (args.dir / "plan.json").write_text(json.dumps(planned, indent=1))
    found = sum(bool(r.get("target_post_found")) for r in rows)
    meta = {
        "phase1_sha256": hashlib.sha256(args.phase1.read_bytes()).hexdigest(),
        "reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        "phase1_rows": len(rows),
        "target_post_found": found,
        "scored": len(assigned),
        "tiers": {t: sizes.get(t, 0) for t in TIERS},
        "cutoffs": cuts,
    }
    (args.dir / "tiers.json").write_text(json.dumps(meta, indent=1))
    copied = copy_surges(args.surges, set(planned), args.dir / "surges.jsonl")
    print(json.dumps(meta, indent=1))
    print(f"drew {len(planned)} surges; copied {copied} detector lines")
    return 0


def _union(args: argparse.Namespace) -> int:
    plans = {p.parent.name: json.loads(p.read_text()) for p in args.plan}
    merged = union(plans)
    args.dir.mkdir(parents=True, exist_ok=True)
    (args.dir / "plan.json").write_text(json.dumps(merged, indent=1))
    copied = copy_surges(args.surges, set(merged), args.dir / "surges.jsonl")
    shared = sum("+" in e["stratum"] for e in merged.values())
    print(f"{len(merged)} surges ({shared} in more than one plan); copied {copied}")
    return 0


def _report(args: argparse.Namespace) -> int:
    planned: dict[str, Row] = json.loads((args.dir / "plan.json").read_text())
    sizes = json.loads((args.dir / "tiers.json").read_text())["tiers"]
    lines, gold = report(planned, pooled_classes(args.threads), sizes)
    for measure, entries in gold.items():
        (args.dir / f"gold-{measure}.json").write_text(json.dumps(entries, indent=1))
    print("\n".join(lines))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sc = sub.add_parser("score", help="phase 1: cheap frozen scores for the frame")
    sc.add_argument("--surges", type=Path, required=True, help="full detector output")
    sc.add_argument("--interim-surges", type=Path, required=True)
    sc.add_argument("--interim-sample-size", type=int, default=200)
    sc.add_argument("--interim-seed", type=int, default=20261017)
    sc.add_argument("--study-db", type=Path, required=True)
    sc.add_argument("--cache", type=Path, required=True)
    sc.add_argument("--out", type=Path, required=True)
    pl = sub.add_parser("plan", help="tiers, the seeded draw, phase-2 surge file")
    pl.add_argument("--phase1", type=Path, required=True)
    pl.add_argument("--reference", type=Path, required=True, help="retro reference")
    pl.add_argument("--surges", type=Path, required=True, help="full detector output")
    pl.add_argument("--dir", type=Path, required=True)
    un = sub.add_parser("union", help="one thread packet plan over several plans")
    un.add_argument("--plan", type=Path, nargs="+", required=True)
    un.add_argument("--surges", type=Path, required=True, help="full detector output")
    un.add_argument("--dir", type=Path, required=True)
    rp = sub.add_parser("report", help="per-tier results and gold files")
    rp.add_argument("--dir", type=Path, required=True, help="the trawl plan's dir")
    rp.add_argument("--threads", type=Path, required=True, help="judged thread dir")
    args = parser.parse_args(argv)
    commands = {"score": _score, "plan": _plan, "union": _union, "report": _report}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
