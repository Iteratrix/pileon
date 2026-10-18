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
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pileon_lab.detector import Reference
from pileon_lab.evalset import JUDGES
from pileon_lab.study import load_incidents
from pileon_lab.surges import SurgeRecord, read_surges, sample, size_bucket
from pileon_lab.sweep import surge_class
from pileon_lab.threads import label_class, read_judgment

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
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
