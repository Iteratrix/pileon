"""Early warning: can the end state of an incident be read in its first hour?

A live defense has to act while a surge is young: 43% of arrivals land within
15 minutes of the quote. This truncates each incident at +15 min, +1 h and
+6 h after its anchor and recomputes only what would be observable then:
how many have arrived, how many of them already like the target post, likes
per reply on the target post so far (likes dated by `createdAt`), and who the
early arrivals are (dated follows). Each is scored (AUC) against the class the
incident ends in at 48 h.

Reads only the response cache (offline); run `enrich` first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pileon_lab.enrich import LIKER_PAGES, arrivals, incident_posts, usable
from pileon_lab.features import composition, first_arrivals
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import (
    Likers,
    Relationship,
    likers,
    post_counts,
    relationships,
)
from pileon_lab.study import Incident, load_incidents
from pileon_lab.sweep import auc

CUTS = (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6))


def _when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def early_signals(
    incident: Incident,
    cut: timedelta,
    liked: Likers | None,
    composition_inputs: tuple[dict[str, Relationship], dict[str, Relationship]] | None,
) -> dict[str, float | None]:
    """Signals observable `cut` after the anchor."""
    end = incident.anchor + cut
    early = replace(
        incident, responses=tuple(r for r in incident.responses if r.at <= end)
    )
    first = first_arrivals(early.responses)
    signals: dict[str, float | None] = {"arrivals": float(len(first))}
    if liked is not None and liked.complete:
        likes_by_then = sum(
            1
            for at in liked.liked_at.values()
            if (t := _when(at)) is not None and t <= end
        )
        liked_by_then = sum(
            1
            for a in first
            if (t := _when(liked.liked_at.get(a, ""))) is not None and t <= end
        )
        signals["liked_share"] = liked_by_then / len(first) if first else None
        signals["likes_per_reply"] = (likes_by_then + 1) / (len(early.responses) + 1)
    if composition_inputs is not None:
        to_target, to_amplifier = composition_inputs
        comp = composition(early, to_target, to_amplifier)
        signals["follow_target"] = comp.follow_target
        signals["follow_amplifier"] = comp.follow_amplifier
    return signals


def classify(row: dict[str, Any], *, hostile_at: float, support_at: float) -> str:
    labeled = int(row["labeled"])
    hostile = int(row["contempt"]) / labeled >= hostile_at
    support = int(row["supportive"]) / labeled >= support_at
    if hostile:
        return "crossfire" if support else "pileon"
    return "rally" if support else "spotlight"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study-db", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--min-labeled", type=int, default=10)
    parser.add_argument("--hostile-at", type=float, default=0.10)
    parser.add_argument("--support-at", type=float, default=0.25)
    args = parser.parse_args(argv)

    rows = {
        r["incident_id"]: r
        for r in map(json.loads, args.features.read_text().splitlines())
        if (r["labeled"] or 0) >= args.min_labeled
    }
    incidents = [
        i for i in usable(load_incidents(args.study_db)) if i.incident_id in rows
    ]

    async def gather() -> list[tuple[str, dict[str, dict[str, float | None]]]]:
        fetcher = Fetcher(Cache(args.cache), offline=True)
        try:
            targets = await post_counts(fetcher, incident_posts(incidents))
            amplifiers = sorted({i.amplifier_did for i in incidents if i.amplifier_did})
            audiences = {
                amp: await relationships(
                    fetcher,
                    amp,
                    sorted(
                        {
                            a
                            for i in incidents
                            if i.amplifier_did == amp
                            for a in arrivals(i)
                        }
                    ),
                )
                for amp in amplifiers
            }
            out = []
            for incident in incidents:
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
                inputs = (to_target, audiences.get(incident.amplifier_did, {}))
                out.append(
                    (
                        incident.incident_id,
                        {
                            f"{int(cut.total_seconds() // 60)}m": early_signals(
                                incident,
                                cut,
                                liked,
                                inputs,
                            )
                            for cut in CUTS
                        },
                    )
                )
            return out
        finally:
            await fetcher.close()

    signals = asyncio.run(gather())
    lines = []
    for outcome in ("pileon", "rally"):
        positives = sum(
            classify(rows[i], hostile_at=args.hostile_at, support_at=args.support_at)
            == outcome
            for i, _ in signals
        )
        lines.append(f"== {outcome}: {positives} of {len(signals)} judged incidents")
        for cut in ("15m", "60m", "360m"):
            for name in (
                "arrivals",
                "liked_share",
                "likes_per_reply",
                "follow_target",
                "follow_amplifier",
            ):
                values: list[float] = []
                labels: list[bool] = []
                for incident_id, by_cut in signals:
                    value = by_cut[cut].get(name)
                    if value is None:
                        continue
                    values.append(value)
                    final = classify(
                        rows[incident_id],
                        hostile_at=args.hostile_at,
                        support_at=args.support_at,
                    )
                    labels.append(final == outcome)
                score = auc(values, labels)
                if score is not None:
                    lines.append(
                        f"  +{cut:<5} {name:<18} AUC {score:.3f}  n={len(values)}"
                    )
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
