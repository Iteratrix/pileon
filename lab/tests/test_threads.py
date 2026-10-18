from __future__ import annotations

import asyncio
import json
import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.surges import Arrival, QuoteCandidate, SurgeRecord
from pileon_lab.threads import (
    Roles,
    build_thread,
    label_class,
    pick,
    read_judgment,
    render_embed,
    render_text,
    stratify,
    write_packet,
)

if TYPE_CHECKING:
    from pathlib import Path

TARGET, QUOTER, OTHER = "did:plc:target", "did:plc:quoter", "did:plc:other"
ONSET = datetime(2026, 8, 20, tzinfo=UTC)


def uri(did: str, rkey: str) -> str:
    return f"at://{did}/app.bsky.feed.post/{rkey}"


def view(
    did: str, rkey: str, text: str, *, hours: float, **record: Any
) -> dict[str, Any]:
    stamp = (ONSET + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    return {
        "uri": uri(did, rkey),
        "author": {
            "did": did,
            "handle": f"{rkey}-handle.bsky.social",
            "displayName": "Real Name",
        },
        "record": {"createdAt": stamp, "text": text, **record},
        "likeCount": 987,
        "replyCount": 654,
    }


def node(post: dict[str, Any], *replies: dict[str, Any]) -> dict[str, Any]:
    return {
        "$type": "app.bsky.feed.defs#threadViewPost",
        "post": post,
        "replies": list(replies),
    }


def mention(text: str, name: str, did: str) -> dict[str, Any]:
    start = text.encode().index(name.encode())
    return {
        "index": {"byteStart": start, "byteEnd": start + len(name.encode())},
        "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}],
    }


def fake_bluesky() -> httpx.MockTransport:
    target = view(TARGET, "t1", "my post", hours=-1)
    text = "@t1-handle.bsky.social is wrong, see https://example.com/x"
    reply = view(
        OTHER,
        "r1",
        text,
        hours=1,
        facets=[mention(text, "@t1-handle.bsky.social", TARGET)],
    )
    late = view(OTHER, "late", "too late", hours=60)
    own = view(TARGET, "own", "please stop", hours=2)
    quote = view(
        QUOTER,
        "q1",
        "look at this",
        hours=0,
        embed={"record": {"uri": uri(TARGET, "t1")}},
    )
    quote["embed"] = {
        "$type": "app.bsky.embed.record#view",
        "record": {
            "$type": "app.bsky.embed.record#viewRecord",
            "uri": uri(TARGET, "t1"),
        },
    }
    under_quote = view(OTHER, "r2", "lol @nobody.example.com", hours=3)
    threads = {
        uri(TARGET, "t1"): node(target, node(reply, node(own)), node(late)),
        uri(QUOTER, "q1"): node(quote, node(under_quote)),
    }
    posts = {p["uri"]: p for p in (target, quote)}

    def handler(request: httpx.Request) -> httpx.Response:
        params = parse_qsl(urlsplit(str(request.url)).query)
        if request.url.path.endswith("getPostThread"):
            return httpx.Response(200, json={"thread": threads[dict(params)["uri"]]})
        asked = [v for k, v in params if k == "uris"]
        return httpx.Response(
            200, json={"posts": [posts[u] for u in asked if u in posts]}
        )

    return httpx.MockTransport(handler)


def surge() -> SurgeRecord:
    return SurgeRecord(
        target=TARGET,
        onset_us=int(ONSET.timestamp() * 1e6),
        warmup=False,
        baseline_per_hour=0.1,
        strangers=20,
        arrivals=20,
        replies=19,
        quotes=1,
        mentions=0,
        peak_surprise=9.0,
        target_posts=((uri(TARGET, "t1"), 19),),
        quote_posts=(
            QuoteCandidate(uri(QUOTER, "q1"), QUOTER, int(ONSET.timestamp() * 1e6), 50),
        ),
        recorded=(Arrival(3600, OTHER, "r", "r1", "t1", stranger=True),),
    )


def test_thread_packet_shows_both_threads_without_counts_handles_or_times(
    tmp_path: Path,
) -> None:
    async def run() -> Any:
        client = httpx.AsyncClient(transport=fake_bluesky())
        fetcher = Fetcher(Cache(tmp_path / "c.db"), client=client)
        try:
            return await build_thread(
                surge(), fetcher, picks={uri(OTHER, "r1")}, seed=1
            )
        finally:
            await fetcher.close()

    thread = asyncio.run(run())
    assert thread is not None
    entry = thread.entry
    shown = json.dumps(entry)
    for leak in (
        "987",
        "654",
        "handle",
        "Real Name",
        "did:plc",
        "2026",
        "http",
        "example.com",
    ):
        assert leak not in shown
    assert entry["quote_post"]["by"] == "QUOTER"
    assert entry["quote_post"]["text"] == "look at this [quotes T1]"
    replies = {r["text"]: r for r in entry["target_thread"]}
    assert set(replies) == {"@TARGET is wrong, see [link]", "please stop"}
    assert replies["please stop"]["by"] == "TARGET"
    assert (
        replies["please stop"]["reply_to"]
        == replies["@TARGET is wrong, see [link]"]["id"]
    )
    assert [r["text"] for r in entry["quote_thread"]] == ["lol @someone"]
    labeled = set(entry["label_these"])
    assert "Q" in labeled
    assert replies["please stop"]["id"] not in labeled
    assert thread.key["T1"] == uri(TARGET, "t1")


def test_render_text_keeps_pseudonyms_consistent() -> None:
    roles = Roles(TARGET, QUOTER)
    text = "hey @friend.bsky.social"
    roles.learn({"author": {"did": OTHER, "handle": "friend.bsky.social"}})
    assert render_text({"text": text}, roles) == "hey @R1"
    embed = {
        "$type": "app.bsky.embed.images#view",
        "images": [{"alt": "screenshot: @friend.bsky.social at https://x.org/y"}],
    }
    assert render_embed(embed, roles, {}) == "[image: screenshot: @R1 at [link]]"
    facet = mention(text, "@friend.bsky.social", QUOTER)
    assert render_text({"text": text, "facets": [facet]}, roles) == "hey @QUOTER"


def test_pick_keeps_must_haves_caps_the_rest_and_adds_parents() -> None:
    posts = [
        (view(OTHER, f"p{i}", "x", hours=i), uri(OTHER, f"p{i - 1}"))
        for i in range(1, 9)
    ]
    chosen = pick(
        posts,
        keep=lambda p: p["uri"] == uri(OTHER, "p8"),
        cap=2,
        rng=random.Random(0),  # noqa: S311
    )
    picked = [p["uri"] for p, _ in chosen]
    assert uri(OTHER, "p8") in picked
    assert uri(OTHER, "p7") in picked
    assert len(picked) <= 4
    assert picked == sorted(picked, key=lambda u: int(u.rsplit("p", 1)[1]))


def test_stratify_separates_confirmed_demoted_and_near() -> None:
    def row(contempt: int, supportive: int = 0) -> dict[str, int]:
        return {"labeled": 20, "contempt": contempt, "supportive": supportive}

    confirmed = {"a": row(3), "b": row(0), "c": row(1, 8), "d": row(1), "e": row(0, 10)}
    screen = {"a": row(3), "b": row(4), "c": row(4, 8), "d": row(1), "e": row(0, 10)}
    assert stratify(confirmed, screen) == {
        "a": "pileon",
        "b": "demoted",
        "c": "contested",
        "d": "near",
        "e": "other",
    }


def test_label_class_counts_only_contempt_aimed_at_the_target() -> None:
    hostile_at_quoter = [{"label": "hostile", "aimed": "quoter"}] * 5
    assert label_class(hostile_at_quoter) == "spotlight"
    mixed = [{"label": "hostile", "aimed": "target"}] + [
        {"label": "neutral", "aimed": "none"}
    ] * 4
    assert label_class(mixed) == "pileon"
    assert label_class(mixed[:3]) is None


def test_packet_is_blind_and_judgments_are_validated(tmp_path: Path) -> None:
    from pileon_lab.threads import Thread  # noqa: PLC0415 - local to this test

    threads = [
        (
            stratum,
            Thread(surge_id=f"s{n}", entry={"label_these": []}, key={"T1": f"u{n}"}),
        )
        for n, stratum in enumerate(["pileon", "other", "near"])
    ]
    ids = write_packet(threads, tmp_path, chunk=2, seed=3)
    assert sorted(ids) == ["s0", "s1", "s2"]
    chunks = sorted(tmp_path.glob("chunk-*.jsonl"))
    assert len(chunks) == 2
    text = "".join(c.read_text() for c in chunks)
    assert "pileon" not in text
    assert "s0" not in text
    (tmp_path / "opus.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in (
                {"surge": "S01", "verdict": "pileon", "severity": 2, "piled_on": True},
                {"surge": "S02", "verdict": "dunk"},
                {"surge": "S01", "post": "P001", "label": "hostile", "aimed": "target"},
                {
                    "surge": "S01",
                    "post": "P002",
                    "label": "hostile",
                    "aimed": "everyone",
                },
            )
        )
    )
    judged = read_judgment(tmp_path / "opus.jsonl")
    assert set(judged.verdicts) == {"S01"}
    assert set(judged.posts) == {("S01", "P001")}
