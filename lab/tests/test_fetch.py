import asyncio
import json
from urllib.parse import parse_qsl, urlsplit

import httpx

from pileon_lab.fetch import Cache, Fetcher, Response
from pileon_lab.sources import post_counts

DELETED = "at://did:plc:a/app.bsky.feed.post/gone"


def post(uri, likes):
    return {
        "uri": uri,
        "author": {"did": "did:plc:a"},
        "record": {"createdAt": "2026-10-17T00:00:00Z", "text": "hi"},
        "likeCount": likes,
        "replyCount": 1,
    }


def transport(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        asked = [
            v for k, v in parse_qsl(urlsplit(str(request.url)).query) if k == "uris"
        ]
        calls.append(asked)
        posts = [post(uri, n) for n, uri in enumerate(asked) if uri != DELETED]
        return httpx.Response(200, json={"posts": posts})

    return httpx.MockTransport(handler)


def test_post_counts_serves_any_subset_from_the_per_post_index(tmp_path):
    calls = []

    async def run():
        client = httpx.AsyncClient(transport=transport(calls))
        fetcher = Fetcher(Cache(tmp_path / "c.db"), client=client)
        uris = [f"at://did:plc:a/app.bsky.feed.post/{i}" for i in range(30)] + [DELETED]
        first = await post_counts(fetcher, uris)
        subset = await post_counts(fetcher, [*uris[5:12], DELETED])
        await fetcher.close()
        offline = Fetcher(Cache(tmp_path / "c.db"), offline=True)
        later = await post_counts(offline, uris[20:25])
        await offline.close()
        return first, subset, later

    first, subset, later = asyncio.run(run())
    assert len(first) == 30
    assert DELETED not in first
    assert len(calls) == 2, "31 URIs fetch in two batches, the subset in none"
    assert set(subset) == {
        f"at://did:plc:a/app.bsky.feed.post/{i}" for i in range(5, 12)
    }
    assert len(later) == 5


def test_existing_batch_responses_are_indexed_once(tmp_path):
    cache = Cache(tmp_path / "c.db")
    uri = "at://did:plc:a/app.bsky.feed.post/1"
    url = (
        "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"
        f"?uris={uri}&uris={DELETED}"
    )
    cache.put(url, Response(200, {"posts": [post(uri, 3)]}))
    cache.commit()
    cache.close()
    reopened = Cache(tmp_path / "c.db")
    known = reopened.get_posts([uri, DELETED, "at://unknown"])
    assert known[uri]["likeCount"] == 3
    assert known[DELETED] is None
    assert "at://unknown" not in known
    assert json.dumps(known[uri])
