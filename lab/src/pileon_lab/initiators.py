"""Pile-on initiators for the labeler: accounts whose quotes keep drawing pile-ons.

The label's claim is structural and nothing more (owner's decision,
2026-10-18): an account is nominated when quotes it posted drew at least
`min_count` likely pile-ons within a window. A likely pile-on is a surge whose
cheap structural score (the frozen retro detector fed batched counts, exactly
the trawl's phase 1) reaches the trawl's frozen high-tier cutoff, outside the
trawl's rally carve-out. The account is the surge's amplifier: the author of
the most engaged quote of the target's post. No text is read; precision is
measured once on a judged sample, never per label.

`score` writes one cheap row per eligible surge; `nominate` counts likely
pile-ons per amplifier and writes the nominees with their incidents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pileon_lab.detector import Reference
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.surges import SurgeRecord, read_surges, to_incident
from pileon_lab.trawl import BATCH, MIN_RESPONSES, cheap_rows

Row = dict[str, Any]


def ineligible(surge: SurgeRecord) -> str | None:
    """Why a surge cannot count toward a label, or `None`."""
    if surge.warmup:
        return "warm-up"
    if not surge.target_posts:
        return "no target post"
    if surge.replies + surge.quotes < MIN_RESPONSES:
        return "fewer than 10 replies and quotes"
    if not surge.quote_posts:
        return "no quote"
    return None


@dataclass(frozen=True, slots=True)
class Cutoffs:
    """The trawl's frozen tier cutoffs (`tiers.json`)."""

    top: float
    high: float
    rally_approval: float

    @classmethod
    def from_tiers(cls, tiers: dict[str, Any]) -> Cutoffs:
        cutoffs = tiers["cutoffs"]
        return cls(
            top=cutoffs["score"]["top"],
            high=cutoffs["score"]["high"],
            rally_approval=cutoffs["rally_approval"],
        )


def likely(row: Row, reference: Reference, cutoffs: Cutoffs) -> str | None:
    """`"top"` or `"high"` for a likely pile-on, `None` otherwise.

    The rally carve-out comes first, as in the trawl: a surge whose approval
    family is at or below the rally cutoff is never a likely pile-on.
    """
    score, parts = reference.score(row)
    if score is None:
        return None
    approval = parts.get("approval")
    if approval is not None and approval <= cutoffs.rally_approval:
        return None
    if score >= cutoffs.top:
        return "top"
    if score >= cutoffs.high:
        return "high"
    return None


@dataclass(frozen=True, slots=True)
class Nominee:
    """An account whose quotes drew enough likely pile-ons in the window."""

    account: str
    incidents: tuple[str, ...]
    tiers: dict[str, int]
    targets: int


def nominate(  # noqa: PLR0913 - every dial is explicit
    rows: list[Row],
    reference: Reference,
    cutoffs: Cutoffs,
    *,
    min_count: int,
    end: datetime,
    days: int,
    counted: frozenset[str] = frozenset({"top", "high"}),
) -> list[Nominee]:
    """Accounts with at least `min_count` likely pile-ons in `(end - days, end]`."""
    start_us = int((end - timedelta(days=days)).timestamp() * 1e6)
    end_us = int(end.timestamp() * 1e6)
    found: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if not start_us < row["onset_us"] <= end_us or not row.get("amplifier"):
            continue
        tier = likely(row, reference, cutoffs)
        if tier in counted:
            found[row["amplifier"]].append(row | {"tier": tier})
    out = [
        Nominee(
            account=account,
            incidents=tuple(r["incident_id"] for r in sorted(hits, key=_onset)),
            tiers=dict(Counter(r["tier"] for r in hits)),
            targets=len({r["incident_id"].rsplit(":", 1)[0] for r in hits}),
        )
        for account, hits in found.items()
        if len(hits) >= min_count
    ]
    return sorted(out, key=lambda n: (-len(n.incidents), n.account))


def _onset(row: Row) -> int:
    return int(row["onset_us"])


def _score(args: argparse.Namespace) -> int:
    skipped: Counter[str] = Counter()
    surges = []
    for surge in read_surges(args.surges):
        reason = ineligible(surge)
        if reason is None:
            surges.append(surge)
        else:
            skipped[reason] += 1
    print(f"eligible {len(surges)}; skipped {dict(skipped)}", file=sys.stderr)

    async def run() -> int:
        fetcher = Fetcher(Cache(args.cache))
        written = 0
        try:
            with args.out.open("w") as out:
                for start in range(0, len(surges), BATCH):
                    batch = surges[start : start + BATCH]
                    meta = {
                        s.surge_id: {
                            "onset_us": s.onset_us,
                            "amplifier": to_incident(s).amplifier_did,
                            "arrivals": s.arrivals,
                        }
                        for s in batch
                    }
                    for row in await cheap_rows(batch, fetcher):
                        out.write(json.dumps(row | meta[row["incident_id"]]) + "\n")
                        written += 1
                    print(f"[initiators] {written}/{len(surges)}", file=sys.stderr)
        finally:
            await fetcher.close()
            print(f"cache: {fetcher.stats}", file=sys.stderr)
        return written

    written = asyncio.run(run())
    print(f"wrote {written} rows to {args.out}")
    return 0


def _nominate(args: argparse.Namespace) -> int:
    reference = Reference.from_json(args.reference.read_text())
    cutoffs = Cutoffs.from_tiers(json.loads(args.tiers.read_text()))
    rows = [json.loads(line) for line in args.scores.read_text().splitlines()]
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    nominees = nominate(
        rows,
        reference,
        cutoffs,
        min_count=args.min_count,
        end=end,
        days=args.days,
        counted=frozenset(args.counted.split("+")),
    )
    args.out.write_text(
        "".join(
            json.dumps(
                {
                    "account": n.account,
                    "incidents": n.incidents,
                    "tiers": n.tiers,
                    "targets": n.targets,
                }
            )
            + "\n"
            for n in nominees
        )
    )
    print(
        f"{len(nominees)} accounts with {args.min_count}+ likely pile-ons "
        f"({args.counted}) in the {args.days} days to {args.end}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sc = sub.add_parser("score", help="cheap structural rows for every eligible surge")
    sc.add_argument("--surges", type=Path, required=True)
    sc.add_argument("--cache", type=Path, required=True)
    sc.add_argument("--out", type=Path, required=True)
    no = sub.add_parser("nominate", help="accounts with enough likely pile-ons")
    no.add_argument("--scores", type=Path, required=True)
    no.add_argument("--reference", type=Path, required=True)
    no.add_argument("--tiers", type=Path, required=True, help="trawl tiers.json")
    no.add_argument("--min-count", type=int, default=3)
    no.add_argument("--days", type=int, default=60)
    no.add_argument("--end", required=True, help="YYYY-MM-DD, window end (UTC)")
    no.add_argument("--counted", default="top+high", choices=["top", "top+high"])
    no.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "score":
        return _score(args)
    return _nominate(args)


if __name__ == "__main__":
    sys.exit(main())
