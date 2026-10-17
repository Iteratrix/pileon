"""Typed reads from the AppView and PDSes, shared by enrichment and analysis.

Enrichment calls these with a live [`Fetcher`] to fill the cache; feature code
calls the same functions with an offline one, so both always agree on which
requests represent a fact.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pileon_lab.fetch import APPVIEW, Fetcher, resolve_pds
from pileon_lab.tid import for_time

if TYPE_CHECKING:
    from datetime import datetime

_POSTS_BATCH = 25
_PROFILES_BATCH = 25
_RELATIONSHIPS_BATCH = 30
_LIST_PAGE = 100


@dataclass(frozen=True, slots=True)
class PostCounts:
    uri: str
    author: str
    created_at: str
    is_reply: bool
    replies: int
    likes: int
    quotes: int
    reposts: int
    text: str = ""


@dataclass(frozen=True, slots=True)
class Relationship:
    """`actor`'s relationship with one other account, with follow record URIs.

    Each follow URI's rkey is a TID, which dates the follow.
    """

    resolved: bool
    actor_follows: str | None
    follows_actor: str | None


UNRESOLVED = Relationship(resolved=False, actor_follows=None, follows_actor=None)


def _counts(post: dict[str, Any]) -> PostCounts:
    record = post.get("record", {})
    return PostCounts(
        uri=post["uri"],
        author=post.get("author", {}).get("did", ""),
        created_at=record.get("createdAt", ""),
        is_reply="reply" in record,
        replies=post.get("replyCount", 0),
        likes=post.get("likeCount", 0),
        quotes=post.get("quoteCount", 0),
        reposts=post.get("repostCount", 0),
        text=record.get("text", "") if isinstance(record.get("text"), str) else "",
    )


async def post_counts(fetcher: Fetcher, uris: list[str]) -> dict[str, PostCounts]:
    """Return current engagement counts for each URI that still resolves.

    Served from the per-post index when known; only unknown URIs are fetched,
    so the result never depends on how callers batch their requests.
    """
    unique = sorted(set(uris))
    known = fetcher.cache.get_posts(unique)
    missing = [u for u in unique if u not in known]
    if missing and not fetcher.offline:
        batches = [
            missing[i : i + _POSTS_BATCH] for i in range(0, len(missing), _POSTS_BATCH)
        ]
        replies = await asyncio.gather(
            *(
                fetcher.xrpc(
                    APPVIEW, "app.bsky.feed.getPosts", [("uris", uri) for uri in batch]
                )
                for batch in batches
            )
        )
        for batch, response in zip(batches, replies, strict=True):
            if response.ok and response.body is not None:
                fetcher.cache.put_posts(response.body.get("posts", []), batch)
        fetcher.cache.commit()
        known = fetcher.cache.get_posts(unique)
    return {uri: _counts(post) for uri, post in known.items() if post is not None}


async def relationships(
    fetcher: Fetcher, actor: str, others: list[str]
) -> dict[str, Relationship]:
    """Return `actor`'s relationship with each of `others`."""
    unique = sorted(set(others) - {actor})
    batches = [
        unique[i : i + _RELATIONSHIPS_BATCH]
        for i in range(0, len(unique), _RELATIONSHIPS_BATCH)
    ]
    replies = await asyncio.gather(
        *(
            fetcher.xrpc(
                APPVIEW,
                "app.bsky.graph.getRelationships",
                [("actor", actor)] + [("others", d) for d in batch],
            )
            for batch in batches
        )
    )
    out = dict.fromkeys(unique, UNRESOLVED)
    for response in replies:
        for rel in (response.body or {}).get("relationships", []):
            if not rel.get("$type", "").endswith("#relationship"):
                continue
            out[rel["did"]] = Relationship(
                resolved=True,
                actor_follows=rel.get("following"),
                follows_actor=rel.get("followedBy"),
            )
    return out


async def post_uris_before(
    fetcher: Fetcher, did: str, moment: datetime, *, limit: int
) -> list[str] | None:
    """Return up to `limit` of `did`'s post URIs created before `moment`.

    Uses the repo's TID ordering: a TID minted for `moment` is a
    `listRecords` cursor that starts just before it. Returns `None` when the
    account's PDS does not resolve or the repo is gone.
    """
    pds = await resolve_pds(fetcher, did)
    if pds is None:
        return None
    uris: list[str] = []
    cursor = for_time(moment)
    while len(uris) < limit:
        response = await fetcher.xrpc(
            pds.rstrip("/"),
            "com.atproto.repo.listRecords",
            [
                ("repo", did),
                ("collection", "app.bsky.feed.post"),
                ("limit", str(_LIST_PAGE)),
                ("cursor", cursor),
            ],
        )
        if not response.ok or response.body is None:
            return uris or None
        records = response.body.get("records", [])
        uris.extend(r["uri"] for r in records)
        next_cursor = response.body.get("cursor")
        if not records or not next_cursor:
            break
        cursor = next_cursor
    return uris[:limit]


@dataclass(frozen=True, slots=True)
class Profile:
    handle: str
    followers: int


async def profiles(fetcher: Fetcher, dids: list[str]) -> dict[str, Profile]:
    """Return handle and current follower count for each DID that resolves."""
    unique = sorted(set(dids))
    batches = [
        unique[i : i + _PROFILES_BATCH] for i in range(0, len(unique), _PROFILES_BATCH)
    ]
    replies = await asyncio.gather(
        *(
            fetcher.xrpc(
                APPVIEW, "app.bsky.actor.getProfiles", [("actors", d) for d in batch]
            )
            for batch in batches
        )
    )
    return {
        profile["did"]: Profile(
            handle=profile.get("handle", ""), followers=profile.get("followersCount", 0)
        )
        for response in replies
        for profile in (response.body or {}).get("profiles", [])
    }


async def profile_counts(fetcher: Fetcher, dids: list[str]) -> dict[str, int]:
    """Return current follower counts for each DID that still resolves."""
    return {did: p.followers for did, p in (await profiles(fetcher, dids)).items()}


@dataclass(frozen=True, slots=True)
class Likers:
    """Who liked a post, and when (client-reported `createdAt`).

    `complete` is false when the post had more likes than the page cap allowed,
    so absence from `liked_at` means nothing.
    """

    complete: bool
    liked_at: dict[str, str]


async def likers(fetcher: Fetcher, uri: str, *, max_pages: int) -> Likers:
    """Return the accounts that like `uri`, up to `max_pages` pages of 100."""
    liked_at: dict[str, str] = {}
    cursor: str | None = None
    for _ in range(max_pages):
        params = [("uri", uri), ("limit", "100")]
        if cursor:
            params.append(("cursor", cursor))
        response = await fetcher.xrpc(APPVIEW, "app.bsky.feed.getLikes", params)
        if not response.ok or response.body is None:
            return Likers(complete=False, liked_at=liked_at)
        page = response.body.get("likes", [])
        for like in page:
            did = like.get("actor", {}).get("did")
            if did:
                liked_at.setdefault(did, like.get("createdAt", ""))
        cursor = response.body.get("cursor")
        if not page or not cursor:
            return Likers(complete=True, liked_at=liked_at)
    return Likers(complete=False, liked_at=liked_at)
