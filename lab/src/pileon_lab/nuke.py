"""Nuclear block (1a) and its live cousins: what each defense would prevent.

Each yield is a rule armed at time `t` after the anchor, using only what is
observable by then, ordered roughly by blast radius:

- `likers`: block accounts that liked the dunk (the amplifier's quote) by `t`.
- `likers-safe`: the same, minus accounts the target follows or that follow
  the target (allies are spared).
- `audience`: block the amplifier's followers (follows dated before `t`),
  allies spared. The list is the amplifier's whole following; only its
  overlap with the repliers can be scored.
- `audience+likers`: both lists.
- `gate-followers`: a followers-only reply gate from `t` on (Bluesky's
  threadgate, plus a postgate against quotes): only accounts that followed
  the target before `t` get through. No list, nobody is blocked.
- `strike`: block each author at their first harm item from `t` on, with a
  perfect labeler; authors who already harmed before `t` are blocked at `t`.
  The ceiling of "instablock what crosses the abuse threshold".

A block or gate stops replies to and quotes of the target's posts, so every
response after `t` by a stopped author counts as prevented. Scored per surge
class: share of harm (hostile or abusive, aimed at the target) after `t`
prevented, share of supportive responses after `t` also stopped (defenders
silenced), and how many targets still receive >= 3 harm items. Firing on a
rally is the misfire cost.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pileon_lab.enrich import LIKER_PAGES, arrivals, usable
from pileon_lab.features import followed_before
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.network import labeled_incident, labeled_sample
from pileon_lab.sources import Likers, Relationship, likers, relationships
from pileon_lab.study import load_incidents
from pileon_lab.sweep import surge_class

if TYPE_CHECKING:
    from pileon_lab.study import Incident, Response

DELAYS = (timedelta(0), timedelta(minutes=15), timedelta(hours=1))
YIELDS = (
    "likers",
    "likers-safe",
    "audience",
    "audience+likers",
    "gate-followers",
    "strike",
)
HARM_FLOOR = 3


def _when(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Evidence:
    """What is known about one incident's dunk likers and repliers."""

    dunk_likers: Likers
    to_target: dict[str, Relationship]
    to_amplifier: dict[str, Relationship]
    amplifier_followers: int | None = None


@dataclass(frozen=True, slots=True)
class Policy:
    """A defense armed at some moment.

    `blocked` are stopped from the moment on; with `strike`, so is every
    author after their first harm item. `size` is how many accounts the
    target would block (`None` for a gate, which blocks nobody).
    """

    blocked: frozenset[str]
    size: int | None
    strike: bool = False


def ally(rel: Relationship | None) -> bool:
    return rel is not None and (
        rel.actor_follows is not None or rel.follows_actor is not None
    )


def policy(incident: Incident, evidence: Evidence, yield_: str, at: datetime) -> Policy:
    """The defense a yield arms at `at`; it never stops the target itself."""
    liked = {
        did
        for did, when in evidence.dunk_likers.liked_at.items()
        if (t := _when(when)) is not None and t <= at
    } - {incident.target_did}
    audience = {
        did
        for did, rel in evidence.to_amplifier.items()
        if rel.resolved
        and followed_before(rel.follows_actor, at) is True
        and not ally(evidence.to_target.get(did))
    } - {incident.target_did}
    following = evidence.amplifier_followers
    match yield_:
        case "likers":
            return Policy(frozenset(liked), len(liked))
        case "likers-safe":
            safe = {d for d in liked if not ally(evidence.to_target.get(d))}
            return Policy(frozenset(safe), len(safe))
        case "audience":
            return Policy(frozenset(audience), following)
        case "audience+likers":
            safe = {d for d in liked if not ally(evidence.to_target.get(d))}
            size = None if following is None else following + len(safe - audience)
            return Policy(frozenset(audience | safe), size)
        case "gate-followers":
            outside = {
                r.author
                for r in incident.responses
                if r.author != incident.target_did
                and not _follows_by(evidence.to_target.get(r.author), at)
            }
            return Policy(frozenset(outside), None)
        case "strike":
            return Policy(frozenset(), 0, strike=True)
        case _:
            msg = f"unknown yield {yield_!r}"
            raise ValueError(msg)


def _follows_by(rel: Relationship | None, at: datetime) -> bool:
    return rel is not None and followed_before(rel.follows_actor, at) is True


@dataclass(slots=True)
class Tally:
    """Totals for one (class, yield, delay) cell."""

    incidents: int = 0
    harm: int = 0
    harm_prevented: int = 0
    supportive: int = 0
    supportive_blocked: int = 0
    other: int = 0
    other_blocked: int = 0
    over_floor: int = 0
    over_floor_after: int = 0
    sizes: list[int] = field(default_factory=list)

    def add(self, incident: Incident, defense: Policy, at: datetime) -> None:
        self.incidents += 1
        if defense.size is not None:
            self.sizes.append(defense.size)
        first_harm = _first_harm(incident.responses)
        harm = through = 0
        for response in incident.responses:
            if response.at <= at or response.valence is None:
                continue
            struck = first_harm.get(response.author)
            stopped = response.author in defense.blocked or (
                defense.strike and struck is not None and struck < response.at
            )
            if response.contempt:
                harm += 1
                through += not stopped
                self.harm += 1
                self.harm_prevented += stopped
            elif response.valence == "supportive":
                self.supportive += 1
                self.supportive_blocked += stopped
            else:
                self.other += 1
                self.other_blocked += stopped
        self.over_floor += harm >= HARM_FLOOR
        self.over_floor_after += through >= HARM_FLOOR


def _first_harm(responses: tuple[Response, ...]) -> dict[str, datetime]:
    first: dict[str, datetime] = {}
    for r in responses:
        if r.contempt and (r.author not in first or r.at < first[r.author]):
            first[r.author] = r.at
    return first


def _share(part: int, whole: int) -> str:
    return f"{part / whole:5.1%}" if whole else "    -"


def liked_before_harm(
    incidents: list[Incident], evidence: dict[str, Evidence]
) -> tuple[int, int, int]:
    """Harm authors in all; of them, those who liked the dunk; liked first."""
    authors = liked = liked_first = 0
    for incident in incidents:
        found = evidence.get(incident.incident_id)
        if found is None or not found.dunk_likers.complete:
            continue
        for author, at in _first_harm(incident.responses).items():
            authors += 1
            when = found.dunk_likers.liked_at.get(author)
            if when is None:
                continue
            liked += 1
            t = _when(when)
            liked_first += t is not None and t <= at
    return authors, liked, liked_first


CLASS_ORDER = ("pileon", "crossfire", "rally", "spotlight")


def report(
    incidents: list[Incident],
    classes: dict[str, str],
    evidence: dict[str, Evidence],
    *,
    order: tuple[str, ...] = CLASS_ORDER,
) -> list[str]:
    tallies: dict[tuple[str, str, timedelta], Tally] = defaultdict(Tally)
    skipped = 0
    for incident in incidents:
        found = evidence.get(incident.incident_id)
        if found is None or not found.dunk_likers.complete:
            skipped += 1
            continue
        cls = classes[incident.incident_id]
        for delay in DELAYS:
            at = incident.anchor + delay
            for yield_ in YIELDS:
                defense = policy(incident, found, yield_, at)
                tallies[cls, yield_, delay].add(incident, defense, at)
    header = (
        f"{len(incidents)} incidents with a dunk; {skipped} skipped "
        "(liker list incomplete or missing)"
    )
    lines = [header]
    authors, liked, first = liked_before_harm(incidents, evidence)
    lines.append(
        f"harm authors {authors}: liked the dunk {_share(liked, authors)}, "
        f"liked it before their first harm reply {_share(first, authors)}"
    )
    for cls in order:
        for delay in DELAYS:
            minutes = int(delay.total_seconds() // 60)
            first_cell = tallies.get((cls, YIELDS[0], delay))
            if first_cell is None:
                continue
            lines.append(
                f"== {cls} at +{minutes}m: {first_cell.incidents} incidents, "
                f"{first_cell.harm} harm / {first_cell.supportive} supportive "
                f"after t, targets with >= {HARM_FLOOR} harm: {first_cell.over_floor}"
            )
            lines += [_row(y, tallies[cls, y, delay]) for y in YIELDS]
    return lines


def _row(yield_: str, cell: Tally) -> str:
    stopped = _share(cell.harm_prevented, cell.harm)
    defenders = _share(cell.supportive_blocked, cell.supportive)
    others = _share(cell.other_blocked, cell.other)
    size = f"{statistics.median(cell.sizes):>7.0f}" if cell.sizes else "      -"
    return (
        f"  {yield_:<16} harm stopped {stopped}  supportive blocked {defenders}"
        f"  other blocked {others}  >= {HARM_FLOOR} harm left "
        f"{cell.over_floor_after:>3}  median list {size}"
    )


async def gather(
    incidents: list[Incident], fetcher: Fetcher, followers: dict[str, int | None]
) -> dict[str, Evidence]:
    """Fetch dunk likers and the relationships the yields need (cached)."""
    out: dict[str, Evidence] = {}
    by_amplifier: dict[str, set[str]] = defaultdict(set)
    for incident in incidents:
        by_amplifier[incident.amplifier_did].update(arrivals(incident))
    audiences = {
        amp: await relationships(fetcher, amp, sorted(people))
        for amp, people in by_amplifier.items()
    }
    for n, incident in enumerate(incidents, 1):
        liked_at: dict[str, str] = {}
        complete = True
        for uri in incident.dunk_uris:
            found = await likers(fetcher, uri, max_pages=LIKER_PAGES)
            complete &= found.complete
            for did, when in found.liked_at.items():
                if did not in liked_at or when < liked_at[did]:
                    liked_at[did] = when
        repliers = arrivals(incident)
        to_target = await relationships(fetcher, incident.target_did, repliers)
        to_target |= await relationships(
            fetcher, incident.target_did, sorted(set(liked_at) - set(repliers))
        )
        out[incident.incident_id] = Evidence(
            dunk_likers=Likers(complete=complete, liked_at=liked_at),
            to_target=to_target,
            to_amplifier=audiences[incident.amplifier_did],
            amplifier_followers=followers.get(incident.incident_id),
        )
        if n % 50 == 0:
            fetcher.cache.commit()
            print(f"[nuke] {n}/{len(incidents)}", file=sys.stderr, flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--study-db", type=Path)
    source.add_argument("--surges", type=Path, help="network surges (with --labels)")
    parser.add_argument("--labels", type=Path)
    parser.add_argument("--labeler", default="gemma4:31b+contrast")
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--include-warmup", action="store_true")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--min-labeled", type=int, default=10)
    parser.add_argument("--hostile-at", type=float, default=0.10)
    parser.add_argument("--support-at", type=float, default=0.25)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--groups",
        type=Path,
        help="JSON {incident id: group} to report by instead of surge class",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    rows = [
        r
        for r in map(json.loads, args.features.read_text().splitlines())
        if (r["labeled"] or 0) >= args.min_labeled
    ]
    classes = {
        r["incident_id"]: surge_class(
            r, hostile_at=args.hostile_at, support_at=args.support_at
        )
        for r in rows
    }
    followers = {r["incident_id"]: r.get("amplifier_followers") for r in rows}
    order: tuple[str, ...] = CLASS_ORDER
    if args.groups is not None:
        groups: dict[str, str] = json.loads(args.groups.read_text())
        classes = {k: v for k, v in groups.items() if k in classes}
        order = tuple(sorted(set(classes.values())))
    if args.surges is not None:
        if args.labels is None:
            parser.error("--surges needs --labels")
        surges, labels, directions = labeled_sample(
            args.surges,
            args.labels,
            args.labeler,
            size=args.sample_size,
            seed=args.seed,
            include_warmup=args.include_warmup,
        )
        loaded = [labeled_incident(s, labels, directions, {}) for s in surges]
    else:
        loaded = usable(load_incidents(args.study_db))
    incidents = [
        i
        for i in loaded
        if i.incident_id in classes and i.dunk_uris and i.amplifier_did
    ]

    async def run() -> dict[str, Evidence]:
        fetcher = Fetcher(Cache(args.cache), offline=args.offline)
        try:
            return await gather(incidents, fetcher, followers)
        finally:
            await fetcher.close()

    text = "\n".join(report(incidents, classes, asyncio.run(run()), order=order))
    if args.out is not None:
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
