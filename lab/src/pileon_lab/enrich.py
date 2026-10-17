"""Warm the response cache with everything the relative features need.

Stages, each idempotent (a rerun is served from the cache):

1. Current counts for every incident's target post and dunk posts.
2. Amplifier follower counts, and amplifier <-> target relationships (is the
   quoter an ally?).
3. Each target's posts from before the incident, with counts: the baseline.
4. Target <-> arrival relationships (do the people showing up follow the
   target, or does the target follow them?).
5. Amplifier <-> arrival relationships (did they follow the quoter, and since
   when?).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import (
    post_counts,
    post_uris_before,
    profile_counts,
    relationships,
)
from pileon_lab.study import Incident, load_incidents

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

BASELINE_POSTS = 200


def usable(incidents: Iterable[Incident]) -> list[Incident]:
    """Incidents whose target and target post are real AT identifiers."""
    return [
        i
        for i in incidents
        if i.target_did.startswith("did:") and i.target_post_uri.startswith("at://")
    ]


def arrivals(incident: Incident) -> list[str]:
    return sorted({r.author for r in incident.responses})


def incident_posts(incidents: Iterable[Incident]) -> list[str]:
    """Every target and dunk post, in one fixed order.

    The cache is keyed by URL and posts are fetched in batches, so enrichment
    and analysis must request exactly this list to hit the same batches.
    """
    posts = set()
    for incident in incidents:
        posts.add(incident.target_post_uri)
        posts.update(incident.dunk_uris)
    return sorted(posts)


class Progress:
    def __init__(self, fetcher: Fetcher, stage: str, total: int) -> None:
        self._fetcher = fetcher
        self._stage = stage
        self._total = total
        self._done = 0
        self._started = time.monotonic()
        self._last = 0.0

    def tick(self) -> None:
        self._done += 1
        now = time.monotonic()
        if now - self._last >= 30 or self._done == self._total:  # noqa: PLR2004
            self._last = now
            stats = self._fetcher.stats
            print(
                f"[{self._stage}] {self._done}/{self._total} "
                f"({now - self._started:.0f}s; fetched {stats.fetched}, "
                f"cached {stats.cached}, retried {stats.retried})",
                file=sys.stderr,
                flush=True,
            )


async def _each(
    items: list[Incident],
    work: Callable[[Incident], Awaitable[object]],
    progress: Progress,
    concurrency: int,
) -> None:
    gate = asyncio.Semaphore(concurrency)

    async def one(item: Incident) -> None:
        async with gate:
            await work(item)
        progress.tick()

    await asyncio.gather(*(one(item) for item in items))


async def enrich(
    incidents: list[Incident], fetcher: Fetcher, *, concurrency: int = 12
) -> None:
    """Run every stage over `incidents`."""
    posts = incident_posts(incidents)
    print(f"[posts] {len(posts)} target and dunk posts", file=sys.stderr)
    await post_counts(fetcher, posts)

    targets_by_amplifier: dict[str, set[str]] = defaultdict(set)
    arrivals_by_amplifier: dict[str, set[str]] = defaultdict(set)
    for incident in incidents:
        targets_by_amplifier[incident.amplifier_did].add(incident.target_did)
        arrivals_by_amplifier[incident.amplifier_did].update(arrivals(incident))
    await profile_counts(fetcher, sorted(targets_by_amplifier))
    for amplifier, targets in targets_by_amplifier.items():
        print(f"[ally] {amplifier}: {len(targets)} targets", file=sys.stderr)
        await relationships(fetcher, amplifier, sorted(targets))

    async def baseline(incident: Incident) -> None:
        uris = await post_uris_before(
            fetcher, incident.target_did, incident.anchor, limit=BASELINE_POSTS
        )
        if uris:
            await post_counts(fetcher, uris)

    await _each(
        incidents, baseline, Progress(fetcher, "baseline", len(incidents)), concurrency
    )

    async def target_relations(incident: Incident) -> None:
        await relationships(fetcher, incident.target_did, arrivals(incident))

    with_arrivals = [i for i in incidents if i.responses]
    await _each(
        with_arrivals,
        target_relations,
        Progress(fetcher, "target-arrivals", len(with_arrivals)),
        concurrency,
    )

    for amplifier, people in arrivals_by_amplifier.items():
        print(f"[amplifier-arrivals] {len(people)} accounts", file=sys.stderr)
        await relationships(fetcher, amplifier, sorted(people))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study-db", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0, help="first N incidents only")
    parser.add_argument("--concurrency", type=int, default=12)
    args = parser.parse_args(argv)

    incidents = usable(load_incidents(args.study_db))
    if args.limit:
        incidents = incidents[: args.limit]
    print(f"{len(incidents)} incidents", file=sys.stderr)

    async def run() -> None:
        fetcher = Fetcher(Cache(args.cache))
        try:
            await enrich(incidents, fetcher, concurrency=args.concurrency)
        finally:
            await fetcher.close()
        print(f"done: {fetcher.stats}", file=sys.stderr)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
