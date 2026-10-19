"""Surge records from `pileon-detect`, as lab incidents.

The detector writes one surge per JSON line (optionally zstd-compressed) with
arrivals compacted to `[seconds from onset, author, kind, post rkey,
target-post rkey, stranger]`. This module reads them back, draws a sample
stratified by size, and turns each surge into an [`Incident`] so the same
enrichment and relative features used on the study apply unchanged.

The amplifier of a surge is the author of the most-engaged quote of the
target's post, when there is one; reply-driven surges without a quote have
no amplifier (screenshots and off-platform links leave no trace). That choice
uses the whole surge. [`live_incident`] instead uses what the detector could
see when the surge triggered: the target post and the quote leading then.
"""

from __future__ import annotations

import io
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pileon_lab.study import Incident, Response

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

KIND_CHANNEL = {"r": "replies", "q": "quotes", "m": "mentions"}


@dataclass(frozen=True, slots=True)
class QuoteCandidate:
    uri: str
    author: str
    at_us: int
    engagement: int


@dataclass(frozen=True, slots=True)
class Trigger:
    """What was visible when the surge crossed the detector's thresholds."""

    at_us: int
    target_post: str | None
    quote: QuoteCandidate | None


@dataclass(frozen=True, slots=True)
class Arrival:
    offset_s: int
    author: str
    kind: str
    post_rkey: str
    target_rkey: str
    stranger: bool

    def uri(self) -> str:
        return f"at://{self.author}/app.bsky.feed.post/{self.post_rkey}"


@dataclass(frozen=True, slots=True)
class SurgeRecord:
    target: str
    onset_us: int
    warmup: bool
    baseline_per_hour: float
    strangers: int
    arrivals: int
    replies: int
    quotes: int
    mentions: int
    peak_surprise: float
    target_posts: tuple[tuple[str, int], ...]
    quote_posts: tuple[QuoteCandidate, ...]
    recorded: tuple[Arrival, ...]
    trigger: Trigger | None = None
    """`None` for detector output written before trigger snapshots existed."""

    @property
    def surge_id(self) -> str:
        return f"{self.target}:{self.onset_us}"

    @property
    def onset(self) -> datetime:
        return datetime.fromtimestamp(self.onset_us / 1e6, tz=UTC)


def _open_text(path: Path) -> io.TextIOBase:
    if path.suffix == ".zst":
        import zstandard  # noqa: PLC0415 - only needed for compressed input

        raw = path.open("rb")
        return io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(raw))
    return path.open()


def _quote(q: dict[str, Any]) -> QuoteCandidate:
    return QuoteCandidate(
        uri=q["uri"],
        author=q["author"],
        at_us=q["at_us"],
        engagement=sum(q["engagement"].values()),
    )


def parse_surge(line: dict[str, Any]) -> SurgeRecord:
    """Build a [`SurgeRecord`] from one decoded JSON line."""
    quotes = tuple(_quote(q) for q in line["quote_posts"])
    trigger = None
    if "trigger_quote" in line:
        quote = line["trigger_quote"]
        trigger = Trigger(
            at_us=line["triggered_us"],
            target_post=line["trigger_target_post"],
            quote=_quote(quote) if quote else None,
        )
    recorded = tuple(
        Arrival(offset, author, kind, rkey, target_rkey, bool(stranger))
        for offset, author, kind, rkey, target_rkey, stranger in line["recorded"]
    )
    return SurgeRecord(
        target=line["target"],
        onset_us=line["onset_us"],
        warmup=line["warmup"],
        baseline_per_hour=line["baseline_per_hour"],
        strangers=line["strangers"],
        arrivals=line["arrivals"],
        replies=line["replies"],
        quotes=line["quotes"],
        mentions=line["mentions"],
        peak_surprise=line["peak_surprise"],
        target_posts=tuple((uri, n) for uri, n in line["target_posts"]),
        quote_posts=quotes,
        recorded=recorded,
        trigger=trigger,
    )


def read_surges(path: Path) -> Iterator[SurgeRecord]:
    """Yield every surge in a detector output file."""
    with _open_text(path) as lines:
        for line in lines:
            if line.strip():
                yield parse_surge(json.loads(line))


def copy_surges(source: Path, wanted: set[str], out: Path) -> int:
    """Copy the detector lines of the `wanted` surge ids to `out`, verbatim."""
    written = 0
    with _open_text(source) as lines, out.open("w") as sink:
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            if f"{row['target']}:{row['onset_us']}" in wanted:
                sink.write(line if line.endswith("\n") else line + "\n")
                written += 1
    return written


def size_bucket(strangers: int) -> int:
    """Power-of-two bucket: 8-15 -> 3, 16-31 -> 4, ... (capped at 9 = 512+)."""
    return min(9, max(3, math.floor(math.log2(max(strangers, 1)))))


def sample(
    surges: list[SurgeRecord],
    *,
    size: int,
    seed: int,
    include_warmup: bool = False,
) -> list[SurgeRecord]:
    """Draw up to `size` surges, equal numbers per size bucket.

    Equal allocation oversamples large surges relative to their frequency, so
    the rare big events are represented; analyses weight by bucket when they
    need population rates. Surges without a target post (mention-only) are
    skipped: there is no post to measure approval on.
    """
    pool = [s for s in surges if s.target_posts and (include_warmup or not s.warmup)]
    buckets: dict[int, list[SurgeRecord]] = defaultdict(list)
    for surge in pool:
        buckets[size_bucket(surge.strangers)].append(surge)
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling, not secrecy
    for members in buckets.values():
        rng.shuffle(members)
    chosen: list[SurgeRecord] = []
    keys = sorted(buckets)
    while len(chosen) < size and any(buckets[k] for k in keys):
        for key in keys:
            if buckets[key] and len(chosen) < size:
                chosen.append(buckets[key].pop())
    return sorted(chosen, key=lambda s: s.onset_us)


def _incident(
    surge: SurgeRecord, target_post: str, amplifier: QuoteCandidate | None
) -> Incident:
    """Responses are every recorded arrival from onset on, except the target's
    own posts and the amplifier's quote; their valence starts unknown."""
    onset = surge.onset
    responses = [
        Response(
            uri=a.uri(),
            author=a.author,
            at=onset + timedelta(seconds=a.offset_s),
            channel=KIND_CHANNEL.get(a.kind, a.kind),
            valence=None,
            direction=None,
        )
        for a in surge.recorded
        if a.offset_s >= 0
        and a.author != surge.target
        and not (amplifier is not None and a.uri() == amplifier.uri)
    ]
    return Incident(
        incident_id=surge.surge_id,
        source="surge",
        target_did=surge.target,
        target_type="",
        target_followers=None,
        anchor=onset,
        amplifier_did=amplifier.author if amplifier else "",
        target_post_uri=target_post,
        dunk_uris=(amplifier.uri,) if amplifier else (),
        responses=tuple(sorted(responses, key=lambda r: r.at)),
    )


def to_incident(surge: SurgeRecord) -> Incident:
    """Express a surge as an [`Incident`] anchored at its onset.

    The target post and the amplifier are the ones leading over the whole
    surge, which a live system cannot know yet.
    """
    amplifier = max(surge.quote_posts, key=lambda q: q.engagement, default=None)
    return _incident(surge, surge.target_posts[0][0], amplifier)


def live_incident(surge: SurgeRecord) -> Incident:
    """Express a surge as an [`Incident`] with the trigger's target post and quote.

    Still anchored at onset and still holding every arrival; signals cut at
    the trigger see only what a live system had then. A surge whose arrivals
    by the trigger were all mentions has no target post (`""`).
    """
    if surge.trigger is None:
        msg = f"{surge.surge_id}: no trigger snapshot; rerun pileon-detect"
        raise ValueError(msg)
    return _incident(surge, surge.trigger.target_post or "", surge.trigger.quote)
