"""Valence labels for surge replies, from a local model through Ollama.

For each sampled surge, a seeded random subset of its replies and quotes is
labeled with codebook v2, given the target post they answer and the
amplifier's quote as context. Hostile and abusive items get a second pass
asking who the hostility is aimed at; only author-directed hostility counts
toward a pile-on. Labels are checkpointed in SQLite keyed by (item, labeler),
so runs resume and competing labelers (models or prompt variants) can be
scored side by side.

Post text is fetched through the response cache for labeling only; nothing
here stores text outside that cache.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from pileon_lab.codebook import (
    VARIANTS,
    direction_prompt,
    parse_direction,
    parse_label,
)
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.sources import post_counts, profiles
from pileon_lab.surges import SurgeRecord, read_surges, sample

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(frozen=True, slots=True)
class Item:
    """One reply or quote to label, with its context."""

    uri: str
    surge_id: str
    target_handle: str
    text: str
    target_post: str
    framing: str


class Ollama:
    """Chat completions from a local Ollama server, deterministic settings."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport
        )
        self.model = model

    def ask(self, prompt: str, *, schema: dict[str, Any] | None = None) -> str:
        """The reply to `prompt`; `schema` holds decoding to that JSON schema."""
        body: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "keep_alive": "60m",
            "think": False,
            "options": {"temperature": 0, "num_ctx": 8192},
            "messages": [{"role": "user", "content": prompt}],
        }
        if schema is not None:
            body["format"] = schema
        reply = self._http.post("/api/chat", json=body)
        reply.raise_for_status()
        content: str = reply.json()["message"]["content"]
        return content

    def close(self) -> None:
        self._http.close()


class LabelStore:
    """Labels and directions keyed by (item URI, labeler tag)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.executescript(
            "CREATE TABLE IF NOT EXISTS labels (item TEXT, labeler TEXT, surge TEXT, "
            "label TEXT, confidence REAL, raw TEXT, PRIMARY KEY (item, labeler));"
            "CREATE TABLE IF NOT EXISTS directions (item TEXT, labeler TEXT, "
            "direction TEXT, raw TEXT, PRIMARY KEY (item, labeler));"
        )

    def labeled(self, labeler: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT item, label FROM labels WHERE labeler = ?", (labeler,)
        )
        return dict(rows.fetchall())

    def directed(self, labeler: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT item, direction FROM directions WHERE labeler = ?", (labeler,)
        )
        return dict(rows.fetchall())

    def save_label(
        self, item: Item, labeler: str, label: str, confidence: float, raw: str
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?, ?)",
            (item.uri, labeler, item.surge_id, label, confidence, raw),
        )
        self._conn.commit()

    def save_direction(
        self, item: Item, labeler: str, direction: str, raw: str
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO directions VALUES (?, ?, ?, ?)",
            (item.uri, labeler, direction, raw),
        )
        self._conn.commit()

    def clone(self, source: str, dest: str) -> None:
        """Copy every label and direction of `source` to the labeler `dest`."""
        self._conn.execute("DELETE FROM labels WHERE labeler = ?", (dest,))
        self._conn.execute("DELETE FROM directions WHERE labeler = ?", (dest,))
        self._conn.execute(
            "INSERT INTO labels SELECT item, ?, surge, label, confidence, raw "
            "FROM labels WHERE labeler = ?",
            (dest, source),
        )
        self._conn.execute(
            "INSERT INTO directions SELECT item, ?, direction, raw "
            "FROM directions WHERE labeler = ?",
            (dest, source),
        )
        self._conn.commit()

    def set_label(self, uri: str, labeler: str, label: str) -> None:
        self._conn.execute(
            "UPDATE labels SET label = ? WHERE item = ? AND labeler = ?",
            (label, uri, labeler),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def choose(surge: SurgeRecord, *, per_surge: int, seed: int) -> list[tuple[str, str]]:
    """Pick up to `per_surge` (reply or quote URI, target post URI) pairs.

    Only arrivals from onset on, on one of the target's posts, by someone
    other than the target. Seeded per surge, so reruns pick the same items.
    """
    pool = [
        (a.uri(), f"at://{surge.target}/app.bsky.feed.post/{a.target_rkey}")
        for a in surge.recorded
        if a.offset_s >= 0
        and a.kind in ("r", "q")
        and a.target_rkey
        and a.author != surge.target
    ]
    unique = sorted(set(pool))
    rng = random.Random(f"{seed}:{surge.surge_id}")  # noqa: S311 - reproducible
    rng.shuffle(unique)
    return unique[:per_surge]


async def hydrate(
    surges: Iterable[SurgeRecord], fetcher: Fetcher, *, per_surge: int, seed: int
) -> list[Item]:
    """Fetch text for the chosen replies and their context."""
    surges = list(surges)
    picks = {s.surge_id: choose(s, per_surge=per_surge, seed=seed) for s in surges}
    amplifier_quote = {
        s.surge_id: max(s.quote_posts, key=lambda q: q.engagement).uri
        for s in surges
        if s.quote_posts
    }
    uris = {u for chosen in picks.values() for pair in chosen for u in pair}
    uris |= set(amplifier_quote.values())
    texts = {
        uri: p.text for uri, p in (await post_counts(fetcher, sorted(uris))).items()
    }
    handles = {
        did: p.handle
        for did, p in (await profiles(fetcher, [s.target for s in surges])).items()
    }
    items = []
    for surge in surges:
        framing = texts.get(amplifier_quote.get(surge.surge_id, ""), "")
        for reply_uri, target_post in picks[surge.surge_id]:
            text = texts.get(reply_uri, "")
            if not text.strip():
                continue
            items.append(
                Item(
                    uri=reply_uri,
                    surge_id=surge.surge_id,
                    target_handle=handles.get(surge.target, ""),
                    text=text,
                    target_post=texts.get(target_post, ""),
                    framing=framing,
                )
            )
    return items


def label_items(
    items: list[Item],
    model: Ollama,
    store: LabelStore,
    *,
    labeler: str,
    variant: str = "v2",
) -> None:
    """Label every item not yet labeled by `labeler`, then direct the hostile ones."""
    done = store.labeled(labeler)
    todo = [i for i in items if i.uri not in done]
    started = time.monotonic()
    for n, item in enumerate(todo, start=1):
        raw = model.ask(
            VARIANTS[variant](
                item.target_handle, item.text, item.target_post, item.framing
            )
        )
        label, confidence = parse_label(raw)
        store.save_label(item, labeler, label, confidence, raw)
        if n % 25 == 0 or n == len(todo):
            rate = n / max(time.monotonic() - started, 1e-6)
            print(
                f"[label] {n}/{len(todo)} ({rate:.2f}/s)", file=sys.stderr, flush=True
            )
    labels = store.labeled(labeler)
    directed = store.directed(labeler)
    hostile = [
        i
        for i in items
        if labels.get(i.uri) in ("hostile", "abusive") and i.uri not in directed
    ]
    for n, item in enumerate(hostile, start=1):
        raw = model.ask(
            direction_prompt(
                item.target_handle, item.text, target_post=item.target_post
            )
        )
        store.save_direction(item, labeler, parse_direction(raw), raw)
        if n % 25 == 0 or n == len(hostile):
            print(f"[direction] {n}/{len(hostile)}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--surges", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=500)
    parser.add_argument("--per-surge", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261017)
    parser.add_argument("--include-warmup", action="store_true")
    parser.add_argument("--ollama", default="http://localhost:11435")
    parser.add_argument("--model", default="gemma4:31b")
    parser.add_argument("--variant", default="contrast", choices=sorted(VARIANTS))
    parser.add_argument("--hydrate-only", action="store_true")
    args = parser.parse_args(argv)

    surges = sample(
        list(read_surges(args.surges)),
        size=args.sample_size,
        seed=args.seed,
        include_warmup=args.include_warmup,
    )
    print(f"{len(surges)} surges sampled", file=sys.stderr)

    async def fetch() -> list[Item]:
        fetcher = Fetcher(Cache(args.cache))
        try:
            return await hydrate(
                surges, fetcher, per_surge=args.per_surge, seed=args.seed
            )
        finally:
            await fetcher.close()

    items = asyncio.run(fetch())
    print(f"{len(items)} items with text", file=sys.stderr)
    if args.hydrate_only:
        return 0
    store = LabelStore(args.labels)
    model = Ollama(args.ollama, args.model)
    try:
        label_items(
            items,
            model,
            store,
            labeler=f"{args.model}+{args.variant}",
            variant=args.variant,
        )
    finally:
        model.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
