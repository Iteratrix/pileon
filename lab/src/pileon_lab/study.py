"""Incidents from a quote-post study database in the qp-pileon schema.

One cluster is one incident: an amplifier quoting or screenshotting a target's
post, anchored at the first such event, with every reply and quote that
reached the target's post in the following 48 h. Only the on-target channels
count as responses (replies/quotes to the target's own post); replies under
the amplifier's post are a different population. Study databases are private
and not part of this repository.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

NON_PERSON = ("news_media", "government", "organization", "aggregator", "self")
ON_TARGET = ("qp", "qp_deleted", "screenshot_source")
VALENCE_MODEL = "qwen3.5:27b-mlx+v2"
DIRECTION_MODEL = "qwen3.5:27b-mlx+v2+direction"


@dataclass(frozen=True, slots=True)
class Response:
    uri: str
    author: str
    at: datetime
    channel: str
    valence: str | None
    direction: str | None

    @property
    def contempt(self) -> bool:
        """Hostile or abusive and aimed at the target (codebook v2)."""
        return self.valence in ("hostile", "abusive") and self.direction == "author"

    @property
    def critical_or_worse(self) -> bool:
        return self.contempt or self.valence == "critical"


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: str
    source: str
    target_did: str
    target_type: str
    target_followers: int | None
    anchor: datetime
    amplifier_did: str
    target_post_uri: str
    dunk_uris: tuple[str, ...]
    responses: tuple[Response, ...]


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def repo_of(uri: str) -> str:
    """Return the repository (account) DID of an AT URI."""
    return uri.removeprefix("at://").split("/", 1)[0]


def load_incidents(db: Path, *, persons_only: bool = True) -> list[Incident]:
    """Load every study cluster as an [`Incident`], oldest first."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        profiles = {
            did: (kind, followers)
            for did, kind, followers in conn.execute(
                "SELECT did, target_type, followers_count FROM profiles"
            )
        }
        clusters = [
            (cluster_id, target, anchor, events, source)
            for cluster_id, target, anchor, events, source in conn.execute(
                "SELECT cluster_id, target_did, anchor_time, events_json, source "
                "FROM clusters ORDER BY anchor_time"
            )
        ]
        amplifier_of = {
            cluster_id: repo_of(
                min(json.loads(events), key=lambda e: e["created_at"])["uri"]
            )
            for cluster_id, _, _, events, _ in clusters
        }
        responses: dict[str, list[Response]] = {}
        query = (
            "SELECT t.cluster_id, t.item_uri, t.author_did, t.target_did, "
            "t.indexed_at, t.channel, "
            "v.label, d.label FROM text_items t "
            "LEFT JOIN valence_labels v ON v.item_uri = t.item_uri AND v.model = ? "
            "LEFT JOIN valence_labels d ON d.item_uri = t.item_uri AND d.model = ? "
            "WHERE t.role = 'response' AND t.treatment IN (?, ?, ?)"
        )
        rows = conn.execute(query, (VALENCE_MODEL, DIRECTION_MODEL, *ON_TARGET))
        for cluster_id, uri, author, target, at, channel, valence, direction in rows:
            if author in (amplifier_of.get(cluster_id), target):
                continue
            responses.setdefault(cluster_id, []).append(
                Response(uri, author, _ts(at), channel, valence, direction)
            )
        incidents = []
        for cluster_id, target, anchor, events_json, source in clusters:
            kind, followers = profiles.get(target, ("", None))
            if persons_only and kind in NON_PERSON:
                continue
            events = sorted(json.loads(events_json), key=lambda e: e["created_at"])
            incidents.append(
                Incident(
                    incident_id=cluster_id,
                    source=source,
                    target_did=target,
                    target_type=kind,
                    target_followers=followers,
                    anchor=_ts(anchor),
                    amplifier_did=amplifier_of[cluster_id],
                    target_post_uri=events[0]["quoted_uri"],
                    dunk_uris=tuple(e["uri"] for e in events),
                    responses=tuple(
                        sorted(responses.get(cluster_id, []), key=lambda r: r.at)
                    ),
                )
            )
        return incidents
    finally:
        conn.close()
