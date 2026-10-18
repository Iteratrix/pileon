"""Held-out grading for Stage C, exactly as `lab/PREREGISTRATION.md` says.

`plan` splits the Stage C sample. It drops surges in the interim window,
flags surges that share a target or amplifier with the dev data, and picks
the surges the thread judges see, each with its inverse-probability weight:
- screen: a cascade contempt share of at least 4%, or a screen-only
  pile-on or crossfire; weight 1;
- alert: either frozen detector alerts at its 10% cutoff; weight 1;
- random: a seeded 25% of the rest; weight 4.

`gold` turns the judges' thread labels into gold files for `detector grade`.
Both report attrition by size bucket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pileon_lab.detector import Reference
from pileon_lab.evalset import JUDGES
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import post_thread, post_views
from pileon_lab.study import load_incidents
from pileon_lab.surges import SurgeRecord, read_surges, sample, size_bucket
from pileon_lab.sweep import surge_class
from pileon_lab.threads import (
    contempt,
    created,
    descendants,
    label_class,
    read_judgment,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pileon_lab.study import Incident

INTERIM_END = datetime(2026, 8, 27, tzinfo=UTC)
RANDOM_SHARE = 0.25
PLAN_SEED = 20261018
SCREEN_SHARE = 0.04
ALERT_BUDGET = "0.10"
MIN_LABELED = 10
MEASURES = ("primary", "hostile", "cascade")

Row = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Dev:
    """Everyone who is a target or an amplifier in a dev set."""

    people: frozenset[str]


def amplifier_of(surge: SurgeRecord) -> str | None:
    best = max(surge.quote_posts, key=lambda q: q.engagement, default=None)
    return best.author if best else None


def dev_people(interim: Iterable[SurgeRecord], study: Iterable[Incident]) -> Dev:
    people = set()
    for surge in interim:
        people.add(surge.target)
        people.add(amplifier_of(surge) or "")
    for incident in study:
        people |= {incident.target_did, incident.amplifier_did}
    people.discard("")
    return Dev(frozenset(people))


def classify(row: Row) -> str:
    return surge_class(row, hostile_at=0.10, support_at=0.25)


def stratum(
    row: Row, screen: Row | None, references: list[Reference], *, surge_id: str
) -> tuple[str, float]:
    """The judging stratum of one graded surge, and its weight."""
    labeled = row.get("labeled") or 0
    contempt_share = (row.get("contempt") or 0) / labeled
    if contempt_share >= SCREEN_SHARE or (
        screen is not None and classify(screen) in {"pileon", "crossfire"}
    ):
        return "screen", 1.0
    for reference in references:
        score, _ = reference.score(row)
        if score is not None and score >= reference.cutoffs[ALERT_BUDGET]:
            return "alert", 1.0
    draw = random.Random(f"{PLAN_SEED}:{surge_id}").random()  # noqa: S311
    if draw < RANDOM_SHARE:
        return "random", 1 / RANDOM_SHARE
    return "unsampled", 0.0


def plan(
    surges: list[SurgeRecord],
    rows: dict[str, Row],
    screen: dict[str, Row],
    references: list[Reference],
    dev: Dev,
) -> tuple[dict[str, Row], list[str]]:
    """Each graded held-out surge's stratum, weight and overlap; attrition."""
    out: dict[str, Row] = {}
    lost: dict[int, Counter[str]] = {}
    for surge in surges:
        tally = lost.setdefault(size_bucket(surge.strangers), Counter())
        tally["sampled"] += 1
        if surge.onset < INTERIM_END:
            tally["interim window"] += 1
            continue
        row = rows.get(surge.surge_id)
        if row is None or (row.get("labeled") or 0) < MIN_LABELED:
            tally["fewer than 10 labeled"] += 1
            continue
        if not any(r.score(row)[0] is not None for r in references):
            tally["unscored"] += 1
            continue
        kind, weight = stratum(
            row, screen.get(surge.surge_id), references, surge_id=surge.surge_id
        )
        overlap = (
            surge.target in dev.people or (amplifier_of(surge) or "") in dev.people
        )
        tally[kind] += 1
        tally["overlaps dev"] += overlap
        out[surge.surge_id] = {"stratum": kind, "weight": weight, "overlap": overlap}
    return out, attrition(lost)


def attrition(lost: dict[int, Counter[str]]) -> list[str]:
    columns = [
        "sampled",
        "interim window",
        "fewer than 10 labeled",
        "unscored",
        "screen",
        "alert",
        "random",
        "unsampled",
        "overlaps dev",
    ]
    lines = ["bucket  " + "  ".join(f"{c:>8.8}" for c in columns)]
    lines += [
        f"{bucket:>6}  " + "  ".join(f"{lost[bucket][c]:>8}" for c in columns)
        for bucket in sorted(lost)
    ]
    total = sum(lost.values(), Counter())
    lines.append("   all  " + "  ".join(f"{total[c]:>8}" for c in columns))
    return lines


def pooled_class(posts: list[Row]) -> str | None:
    """The class over both judges' in-context labels (at least 10 of them)."""
    if len(posts) < MIN_LABELED:
        return None
    return label_class(posts)


def gold(
    planned: dict[str, Row],
    threads_dir: Path | None,
    rows: dict[str, Row],
    *,
    measure: str,
    include_overlap: bool,
) -> tuple[dict[str, Row], Counter[str]]:
    """Gold entries `{positive, weight}` for `detector grade`, and their sources."""
    sources: Counter[str] = Counter()
    pooled: dict[str, str | None] = {}
    if threads_dir is not None and measure != "cascade":
        key: dict[str, Any] = json.loads((threads_dir / "key.json").read_text())
        judged = [read_judgment(threads_dir / f"{j}.jsonl") for j in JUDGES]
        for packet_id, entry in key.items():
            if all(packet_id in j.verdicts for j in judged):
                labels = [
                    r for j in judged for (s, _), r in j.posts.items() if s == packet_id
                ]
                pooled[entry["surge"]] = pooled_class(labels)
    out: dict[str, Row] = {}
    for surge_id, entry in planned.items():
        if entry["overlap"] and not include_overlap:
            sources["overlap excluded"] += 1
            continue
        row = rows[surge_id]
        if measure == "cascade":
            kind, weight = classify(row), 1.0
            sources["cascade"] += 1
        elif entry["stratum"] == "unsampled":
            continue
        elif pooled.get(surge_id) is not None:
            kind, weight = str(pooled[surge_id]), float(entry["weight"])
            sources["in context"] += 1
        else:
            kind, weight = classify(row), float(entry["weight"])
            sources["cascade (unjudgeable)"] += 1
        wanted = {"pileon"} if measure != "hostile" else {"pileon", "crossfire"}
        out[surge_id] = {"positive": kind in wanted, "weight": weight}
    return out, sources


LEAD = timedelta(minutes=15)


def early_alerts(
    gold_entries: dict[str, Row], rows: dict[str, Row], reference: Reference
) -> dict[str, str]:
    """Each gold surge the early detector alerts on: true or false alert."""
    out = {}
    for surge_id, entry in gold_entries.items():
        score, _ = reference.score(rows[surge_id])
        if score is not None and score >= reference.cutoffs[ALERT_BUDGET]:
            out[surge_id] = "true alert" if entry["positive"] else "false alert"
    return out


def contempt_weights(threads_dir: Path) -> dict[str, dict[str, float]]:
    """Surge id -> post URI -> pooled contempt (each judge's call counts 1/2)."""
    key: dict[str, Any] = json.loads((threads_dir / "key.json").read_text())
    judged = [read_judgment(threads_dir / f"{j}.jsonl") for j in JUDGES]
    out: dict[str, dict[str, float]] = {}
    for packet_id, entry in key.items():
        weights = out.setdefault(entry["surge"], {})
        for judgment in judged:
            for (surge, post), row in judgment.posts.items():
                if surge == packet_id and contempt(row):
                    uri = entry["posts"][post]
                    weights[uri] = weights.get(uri, 0.0) + 1 / len(judged)
    return out


async def post_times(posts: dict[str, str], fetcher: Fetcher) -> dict[str, datetime]:
    """`createdAt` of a thread packet's posts (packet id -> URI), from the cache.

    The packet's own roots (target posts and the quote) give every reply,
    fetched exactly as `threads.build_thread` fetched them so the cache
    serves them; anything else shown, such as other quote posts, comes from
    the post index.
    """
    views: list[dict[str, Any]] = []
    for post_id, uri in posts.items():
        if post_id.startswith("T") or post_id == "Q":
            height = 0 if post_id == "Q" else 2
            node = await post_thread(fetcher, uri, depth=10, parent_height=height)
            if node is not None:
                views += [node["post"], *(p for p, _ in descendants(node))]
    times = {v["uri"]: t for v in views if (t := created(v)) is not None}
    rest = [uri for uri in posts.values() if uri not in times]
    for view in (await post_views(fetcher, rest)).values():
        if (t := created(view)) is not None:
            times[view["uri"]] = t
    return times


def lead_shares(
    weights: dict[str, float], times: dict[str, datetime], onset: datetime
) -> tuple[float, float]:
    """Pooled contempt after onset + 15 min, and all of it (both weighted)."""
    after = total = 0.0
    for uri, weight in weights.items():
        when = times.get(uri)
        if when is None:
            continue
        total += weight
        after += weight if when > onset + LEAD else 0.0
    return after, total


def _rows(path: Path) -> dict[str, Row]:
    return {r["incident_id"]: r for r in map(json.loads, path.read_text().splitlines())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pl = sub.add_parser("plan")
    pl.add_argument(
        "--surges", type=Path, required=True, help="Stage C detector output"
    )
    pl.add_argument("--sample-size", type=int, default=500)
    pl.add_argument("--seed", type=int, default=20261017)
    pl.add_argument(
        "--features", type=Path, required=True, help="confirmed + early rows"
    )
    pl.add_argument("--screen-features", type=Path, required=True)
    pl.add_argument("--references", type=Path, nargs="+", required=True)
    pl.add_argument("--interim-surges", type=Path, required=True)
    pl.add_argument("--interim-sample-size", type=int, default=200)
    pl.add_argument("--study-db", type=Path, required=True)
    pl.add_argument("--out", type=Path, required=True)
    gd = sub.add_parser("gold")
    gd.add_argument("--plan", type=Path, required=True)
    gd.add_argument("--threads", type=Path, help="judged thread packet directory")
    gd.add_argument("--features", type=Path, required=True)
    gd.add_argument("--measure", choices=MEASURES, default="primary")
    gd.add_argument("--include-overlap", action="store_true")
    gd.add_argument("--out", type=Path, required=True)
    al = sub.add_parser("alerts", help="early alerts: lead time and nuke groups")
    al.add_argument("--gold", type=Path, required=True, help="primary gold")
    al.add_argument("--features", type=Path, required=True, help="early rows")
    al.add_argument("--reference", type=Path, required=True, help="early reference")
    al.add_argument("--threads", type=Path, required=True)
    al.add_argument("--surges", type=Path, required=True)
    al.add_argument("--sample-size", type=int, default=500)
    al.add_argument("--seed", type=int, default=20261017)
    al.add_argument("--cache", type=Path, required=True)
    al.add_argument("--groups-out", type=Path, required=True, help="for nuke --groups")
    args = parser.parse_args(argv)
    if args.command == "alerts":
        return alerts(args)

    if args.command == "plan":
        surges = sample(
            list(read_surges(args.surges)), size=args.sample_size, seed=args.seed
        )
        interim = sample(
            list(read_surges(args.interim_surges)),
            size=args.interim_sample_size,
            seed=args.seed,
        )
        dev = dev_people(interim, load_incidents(args.study_db, persons_only=False))
        references = [Reference.from_json(p.read_text()) for p in args.references]
        planned, lines = plan(
            surges, _rows(args.features), _rows(args.screen_features), references, dev
        )
        args.out.write_text(json.dumps(planned, indent=1))
        print("\n".join(lines))
        print(
            f"dev people: {len(dev.people)}; wrote {len(planned)} surges to {args.out}"
        )
        return 0
    planned = json.loads(args.plan.read_text())
    out, sources = gold(
        planned,
        args.threads,
        _rows(args.features),
        measure=args.measure,
        include_overlap=args.include_overlap,
    )
    args.out.write_text(json.dumps(out, indent=1))
    positives = sum(e["positive"] for e in out.values())
    print(f"{len(out)} gold surges, {positives} positive; sources {dict(sources)}")
    return 0


def alerts(args: argparse.Namespace) -> int:
    """Write the early alerts' groups for nuke and print their lead time."""
    entries: dict[str, Row] = json.loads(args.gold.read_text())
    reference = Reference.from_json(args.reference.read_text())
    groups = early_alerts(entries, _rows(args.features), reference)
    args.groups_out.write_text(json.dumps(groups, indent=1))
    true_alerts = sorted(k for k, v in groups.items() if v == "true alert")
    surges = {
        s.surge_id: s
        for s in sample(
            list(read_surges(args.surges)), size=args.sample_size, seed=args.seed
        )
    }
    weights = contempt_weights(args.threads)
    key: dict[str, Any] = json.loads((args.threads / "key.json").read_text())
    shown = {entry["surge"]: entry["posts"] for entry in key.values()}

    async def gather() -> list[tuple[float, float]]:
        fetcher = Fetcher(Cache(args.cache), offline=True)
        try:
            out = []
            for surge_id in true_alerts:
                times = await post_times(shown.get(surge_id, {}), fetcher)
                onset = surges[surge_id].onset
                out.append(lead_shares(weights.get(surge_id, {}), times, onset))
            return out
        finally:
            await fetcher.close()

    shares = asyncio.run(gather())
    after = sum(a for a, _ in shares)
    total = sum(t for _, t in shares)
    print(
        f"early alerts: {len(groups)} ({len(true_alerts)} true); pooled contempt in "
        f"true alerts {total:.1f}, after onset + 15 min {after:.1f}"
        + (f" ({after / total:.0%})" if total else "")
    )
    per = sorted(a / t for a, t in shares if t)
    if per:
        middle = per[len(per) // 2]
        print(f"per alert: median {middle:.0%}, range {per[0]:.0%}-{per[-1]:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
