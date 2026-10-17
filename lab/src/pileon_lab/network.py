"""Network surges to relative feature rows, with valence attached.

Draws the same sample the labeler used (same file, size and seed), turns each
surge into an incident, attaches the bulk labels and directions to its
replies, optionally warms the response cache (`--enrich`), and writes one
feature row per surge in the format the sweep page reads.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pileon_lab.enrich import enrich
from pileon_lab.features import assemble
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.label import LabelStore
from pileon_lab.sources import profiles
from pileon_lab.surges import SurgeRecord, read_surges, sample, to_incident

if TYPE_CHECKING:
    from pileon_lab.study import Incident


def labeled_incident(
    surge: SurgeRecord,
    labels: dict[str, str],
    directions: dict[str, str],
    followers: dict[str, int],
) -> Incident:
    """The surge as an incident whose replies carry their labels, if any."""
    incident = to_incident(surge)
    responses = tuple(
        replace(r, valence=labels.get(r.uri), direction=directions.get(r.uri))
        for r in incident.responses
    )
    return replace(
        incident,
        responses=responses,
        target_followers=followers.get(incident.target_did),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--surges", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--labeler", default="llama3.3:70b+v2")
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--include-warmup", action="store_true")
    parser.add_argument("--enrich", action="store_true", help="fetch what is missing")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    surges = sample(
        list(read_surges(args.surges)),
        size=args.sample_size,
        seed=args.seed,
        include_warmup=args.include_warmup,
    )
    store = LabelStore(args.labels)
    labels = store.labeled(args.labeler)
    directions = store.directed(args.labeler)
    store.close()
    print(f"{len(surges)} surges; {len(labels)} labeled items", file=sys.stderr)

    async def run() -> list[dict[str, object]]:
        if args.enrich:
            live = Fetcher(Cache(args.cache))
            try:
                followers = {
                    d: p.followers
                    for d, p in (
                        await profiles(live, [s.target for s in surges])
                    ).items()
                }
                incidents = [
                    labeled_incident(s, labels, directions, followers) for s in surges
                ]
                await enrich(incidents, live)
            finally:
                await live.close()
        offline = Fetcher(Cache(args.cache), offline=True)
        try:
            followers = {
                d: p.followers
                for d, p in (
                    await profiles(offline, [s.target for s in surges])
                ).items()
            }
            incidents = [
                labeled_incident(s, labels, directions, followers) for s in surges
            ]
            rows = await assemble(incidents, offline, pre={})
        finally:
            await offline.close()
            print(f"cache: {offline.stats}", file=sys.stderr)
        return [asdict(row) for row in rows]

    rows = asyncio.run(run())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")
    print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
