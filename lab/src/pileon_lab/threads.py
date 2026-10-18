"""Whole-thread judge packets: a surge as the target would have seen it.

Labels made one reply at a time miss what only the whole conversation shows:
who "this guy" is, a joke among friends, thirty civil replies that add up to a
crowd. A thread packet shows each sampled surge as one conversation:
- up to three of the target's posts, each with up to two parent posts;
- the quoter's post and the other quote posts;
- the replies under the target's posts and under the quote.

Judges give a verdict on the whole surge and label every post in context.

The packet carries nothing the structural proxies measure. It has no counts
of likes, replies, reposts or followers, and no timestamps (posts are in time
order). It has no handles, names or links: people appear as TARGET, QUOTER,
R1, R2 and so on. So the gold cannot quietly encode the signals it will
grade. One leak remains: a thread with fewer replies than the cap shows how
small it is.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pileon_lab.agreement import cohens_kappa, weighted_kappa
from pileon_lab.codebook import V2_LABELS, codebook_text
from pileon_lab.evalset import HOSTILE, JUDGES, read_labels
from pileon_lab.fetch import Cache, Fetcher
from pileon_lab.label import LabelStore, Ollama, choose
from pileon_lab.sources import post_thread, post_views
from pileon_lab.surges import SurgeRecord, read_surges, sample
from pileon_lab.sweep import surge_class

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

WINDOW = timedelta(hours=48)
VERDICTS = ("pileon", "crossfire", "rally", "spotlight", "unclear")
AIMS = ("target", "quoter", "other", "none")
SEVERITIES = ("0", "1", "2", "3")
STRATA = ("pileon", "demoted", "contested", "near", "other")
QUOTAS = {"pileon": 6, "demoted": 12, "contested": 4, "near": 4, "other": 4}
NEAR_SHARE = 0.04
OWN_POSTS = 10
MIN_CLASS_LABELS = 5

Post = dict[str, Any]
Row = dict[str, Any]

THREAD_INSTRUCTIONS = """\
You are judging whole conversations on Bluesky. In each surge, one person (the
TARGET) posted something that suddenly drew many responses, usually after a
more prominent account (the QUOTER) quote-posted it. For each surge you see:
- the TARGET's post or posts, with the posts they replied to (if any);
- the QUOTER's post (if there is one) and other quote posts of the TARGET's
  post;
- replies under the TARGET's posts and replies under the QUOTER's post.

Each list is in time order. Everyone else appears as R1, R2, ...; the same
name is the same person throughout one surge. Large threads are sampled, so
you may see only part of them. Handles, links and counts were removed on
purpose; do not guess at them.

Do two things for every surge.

1. Label every post listed under "label these": the QUOTER's post, the other
   quote posts, and every reply not written by the TARGET. Use the codebook
   below: the post's stance toward the TARGET, read in the context of the
   whole conversation. Also say who the post is aimed at: target, quoter,
   other (someone else), or none.

2. Give one verdict for the whole surge, as the TARGET experienced it:
   - pileon: many people respond to the TARGET with contempt or abuse
     (hostile or abusive in the codebook), and few defend them
   - crossfire: substantial contempt toward the TARGET and also substantial
     support or defense of them: a fight
   - rally: responses are mostly supportive of or friendly toward the TARGET;
     contempt is rare
   - spotlight: lots of attention, but mostly neutral, questions, or
     good-faith disagreement; little contempt and little support
   - unclear: you cannot tell (most of the conversation is missing or
     unintelligible)

   Also give the severity of what the TARGET faced, 0-3:
   - 0: no meaningful hostility toward the TARGET
   - 1: a few people needling or mocking
   - 2: clear contempt from many
   - 3: abuse (slurs, threats, dehumanization) or an overwhelming crowd

   And give piled_on: true if a reasonable person in the TARGET's position
   would feel piled on, even if each post on its own is civil.

The QUOTER's framing is context, not evidence. Label each reply by what it
says. Replies under the QUOTER's post count as responses to the TARGET when
they are about the TARGET.

"""


def thread_codebook() -> str:
    """The instructions thread judges see, ending with codebook v2."""
    return THREAD_INSTRUCTIONS + "Codebook v2:\n\n" + codebook_text()


def _is_view(node: object) -> bool:
    return isinstance(node, dict) and str(node.get("$type", "")).endswith(
        "#threadViewPost"
    )


def created(post: Post) -> datetime | None:
    """The post's client-reported creation time, as an aware datetime."""
    raw = str((post.get("record") or {}).get("createdAt", ""))
    try:
        when = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def descendants(node: dict[str, Any]) -> list[tuple[Post, str]]:
    """Every reply below a thread view, with the URI of the post it answers."""
    out: list[tuple[Post, str]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        parent = current["post"]["uri"]
        for child in current.get("replies") or []:
            if _is_view(child):
                out.append((child["post"], parent))
                stack.append(child)
    return out


def ancestors(node: dict[str, Any], *, limit: int) -> list[Post]:
    """Up to `limit` posts above a thread view, oldest first."""
    out: list[Post] = []
    parent = node.get("parent")
    while _is_view(parent) and len(out) < limit:
        assert isinstance(parent, dict)  # noqa: S101 - narrowed by _is_view
        out.append(parent["post"])
        parent = parent.get("parent")
    return out[::-1]


class Roles:
    """Pseudonyms for one surge: TARGET, QUOTER, then R1, R2, ... as met."""

    def __init__(self, target: str, quoter: str | None) -> None:
        self._names: dict[str, str] = {target: "TARGET"}
        if quoter and quoter != target:
            self._names[quoter] = "QUOTER"
        self._handles: dict[str, str] = {}
        self._others = 0

    def learn(self, post: Post) -> None:
        author = post.get("author") or {}
        handle, did = author.get("handle"), author.get("did")
        if isinstance(handle, str) and isinstance(did, str):
            self._handles[handle.lower()] = did

    def of(self, did: str) -> str:
        if did not in self._names:
            self._others += 1
            self._names[did] = f"R{self._others}"
        return self._names[did]

    def author(self, post: Post) -> str:
        return self.of(str((post.get("author") or {}).get("did", "")))

    def by_handle(self, handle: str) -> str | None:
        did = self._handles.get(handle.lower())
        return self.of(did) if did else None


HANDLE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)+)")
URL = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)


def render_text(record: dict[str, Any], roles: Roles) -> str:
    """Post text with mentions as pseudonyms and links as `[link]`."""
    text = record.get("text", "")
    data = text.encode() if isinstance(text, str) else b""
    spans: list[tuple[int, int, str]] = []
    for facet in record.get("facets") or []:
        index = facet.get("index") or {}
        start, end = index.get("byteStart"), index.get("byteEnd")
        if not (isinstance(start, int) and isinstance(end, int)):
            continue
        if not 0 <= start < end <= len(data):
            continue
        for feature in facet.get("features") or []:
            kind = str(feature.get("$type", ""))
            if kind.endswith("#mention"):
                spans.append((start, end, "@" + roles.of(str(feature.get("did", "")))))
            elif kind.endswith("#link"):
                spans.append((start, end, "[link]"))
    pieces, last = [], 0
    for start, end, replacement in sorted(spans):
        if start < last:
            continue
        pieces += [data[last:start].decode(errors="ignore"), replacement]
        last = end
    pieces.append(data[last:].decode(errors="ignore"))
    return clean("".join(pieces), roles)


def clean(text: str, roles: Roles) -> str:
    """Handles in plain text as pseudonyms (or `@someone`), links as `[link]`."""
    text = HANDLE.sub(lambda m: "@" + (roles.by_handle(m.group(1)) or "someone"), text)
    return URL.sub("[link]", text).strip()


def _quoted(view: dict[str, Any], roles: Roles, ids: dict[str, str]) -> str:
    uri = str(view.get("uri", ""))
    if uri in ids:
        return f"[quotes {ids[uri]}]"
    if not str(view.get("$type", "")).endswith("#viewRecord"):
        return "[quotes a post that is unavailable]"
    who = roles.of(str((view.get("author") or {}).get("did", "")))
    return f"[quotes {who}: {render_text(view.get('value') or {}, roles)[:300]}]"


def render_embed(embed: object, roles: Roles, ids: dict[str, str]) -> str:
    """A short text stand-in for an embed: images, links, quoted posts."""
    if not isinstance(embed, dict):
        return ""
    kind = str(embed.get("$type", ""))
    parts: list[str] = []
    if kind.endswith("images#view"):
        for image in embed.get("images") or []:
            alt = clean(str(image.get("alt") or ""), roles)
            parts.append(f"[image: {alt[:200]}]" if alt else "[image]")
    elif kind.endswith("video#view"):
        alt = clean(str(embed.get("alt") or ""), roles)
        parts.append(f"[video: {alt[:200]}]" if alt else "[video]")
    elif kind.endswith("external#view"):
        title = clean(str((embed.get("external") or {}).get("title") or ""), roles)
        parts.append(f"[link card: {title[:150]}]" if title else "[link card]")
    elif kind.endswith("recordWithMedia#view"):
        parts.append(render_embed(embed.get("media"), roles, ids))
        parts.append(
            _quoted((embed.get("record") or {}).get("record") or {}, roles, ids)
        )
    elif kind.endswith("record#view"):
        parts.append(_quoted(embed.get("record") or {}, roles, ids))
    return " ".join(p for p in parts if p)


def render(post: Post, roles: Roles, ids: dict[str, str]) -> str:
    text = render_text(post.get("record") or {}, roles)
    extra = render_embed(post.get("embed"), roles, ids)
    return f"{text} {extra}".strip()


def quoted_uri(post: Post | None) -> str | None:
    """The URI of the post a quote post embeds, if any."""
    embed = ((post or {}).get("record") or {}).get("embed") or {}
    record = embed.get("record") or {}
    uri = record.get("uri") or (record.get("record") or {}).get("uri")
    return uri if isinstance(uri, str) else None


def pick(
    posts: list[tuple[Post, str]],
    *,
    keep: Callable[[Post], bool],
    cap: int,
    rng: random.Random,
) -> list[tuple[Post, str]]:
    """Kept posts, a random fill up to `cap`, and the parent of each pick.

    The result is in time order. A parent is added only when it is in the
    same thread and was not picked.
    """
    must = [p for p in posts if keep(p[0])]
    rest = [p for p in posts if not keep(p[0])]
    rng.shuffle(rest)
    chosen = must + rest[: max(0, cap - len(must))]
    by_uri = {p[0]["uri"]: p for p in posts}
    have = {p[0]["uri"] for p in chosen}
    for _, parent in list(chosen):
        if parent in by_uri and parent not in have:
            chosen.append(by_uri[parent])
            have.add(parent)
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(chosen, key=lambda p: created(p[0]) or far)


@dataclass(frozen=True, slots=True)
class Thread:
    """One surge as judges see it, and the key back to post URIs."""

    surge_id: str
    entry: dict[str, Any]
    key: dict[str, str]


def _in_window(post: Post, end: datetime) -> bool:
    when = created(post)
    return when is not None and when <= end


async def build_thread(  # noqa: PLR0913 - one knob per section of the packet
    surge: SurgeRecord,
    fetcher: Fetcher,
    *,
    picks: set[str],
    seed: int,
    cap: int = 30,
    max_targets: int = 3,
    max_quotes: int = 10,
) -> Thread | None:
    """Fetch and assemble one surge; `None` when no target post survives.

    `picks` are the URIs the reply-by-reply labeler sampled; they are always
    shown when they are still up, so the two kinds of label can be compared.
    """
    rng = random.Random(f"{seed}:{surge.surge_id}")  # noqa: S311 - reproducible
    end = surge.onset + WINDOW
    amplifier = max(surge.quote_posts, key=lambda q: q.engagement, default=None)
    quote = None
    if amplifier is not None:
        quote = (await post_views(fetcher, [amplifier.uri])).get(amplifier.uri)
    nodes = await _target_nodes(surge, fetcher, quoted_uri(quote), max_targets)
    if not nodes:
        return None
    quote_node = None
    if amplifier is not None and quote is not None:
        quote_node = await post_thread(
            fetcher, amplifier.uri, depth=10, parent_height=0
        )

    own = _own_posts(surge.target, nodes, quote_node, end)
    roots = {node["post"]["uri"] for node in nodes}
    target_side = pick(
        _replies_under(nodes, end),
        keep=lambda p: p["uri"] in picks or p["uri"] in own,
        cap=cap,
        rng=rng,
    )
    quoter = amplifier.author if amplifier else None
    quote_side: list[tuple[Post, str]] = []
    if quote_node is not None:
        quote_side = pick(
            [p for p in descendants(quote_node) if _in_window(p[0], end)],
            keep=lambda p: p["uri"] in own or _did(p) == quoter,
            cap=cap,
            rng=rng,
        )
    others = await _other_quotes(
        surge, fetcher, amplifier.uri if amplifier else "", end
    )
    others = [
        p
        for p, _ in pick(
            [(p, "") for p in others if p["uri"] not in roots],
            keep=lambda p: p["uri"] in picks,
            cap=max_quotes,
            rng=rng,
        )
    ]
    return _assemble(
        surge,
        nodes,
        quote=quote,
        others=others,
        target_side=target_side,
        quote_side=quote_side,
        quoter=quoter,
    )


async def _target_nodes(
    surge: SurgeRecord, fetcher: Fetcher, quoted: str | None, limit: int
) -> list[dict[str, Any]]:
    """Threads of the target's busiest posts, the quoted one first."""
    order = [uri for uri, _ in sorted(surge.target_posts, key=lambda p: -p[1])]
    if quoted and quoted.startswith(f"at://{surge.target}/"):
        order = [quoted, *(u for u in order if u != quoted)]
    nodes: list[dict[str, Any]] = []
    for uri in order:
        if len(nodes) == limit:
            break
        node = await post_thread(fetcher, uri, depth=10, parent_height=2)
        if node is not None:
            nodes.append(node)
    return nodes


def _replies_under(
    nodes: list[dict[str, Any]], end: datetime
) -> list[tuple[Post, str]]:
    """Replies under any of the target posts in the window, each once."""
    roots = {node["post"]["uri"] for node in nodes}
    seen: dict[str, tuple[Post, str]] = {}
    for node in nodes:
        for post, parent in descendants(node):
            if post["uri"] not in roots and _in_window(post, end):
                seen.setdefault(post["uri"], (post, parent))
    return list(seen.values())


def _did(post: Post) -> str:
    return str((post.get("author") or {}).get("did", ""))


def _own_posts(
    target: str,
    nodes: list[dict[str, Any]],
    quote_node: dict[str, Any] | None,
    end: datetime,
) -> set[str]:
    """The target's first replies in either thread, up to `OWN_POSTS`."""
    replies = [p for node in nodes for p, _ in descendants(node)]
    if quote_node is not None:
        replies += [p for p, _ in descendants(quote_node)]
    far = datetime.max.replace(tzinfo=UTC)
    mine = sorted(
        (p for p in replies if _did(p) == target and _in_window(p, end)),
        key=lambda p: created(p) or far,
    )
    return {p["uri"] for p in mine[:OWN_POSTS]}


async def _other_quotes(
    surge: SurgeRecord, fetcher: Fetcher, amplifier_uri: str, end: datetime
) -> list[Post]:
    uris = {
        a.uri()
        for a in surge.recorded
        if a.kind == "q" and a.offset_s >= 0 and a.author != surge.target
    } | {q.uri for q in surge.quote_posts}
    uris.discard(amplifier_uri)
    views = await post_views(fetcher, sorted(uris))
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(
        (p for p in views.values() if _in_window(p, end)),
        key=lambda p: created(p) or far,
    )


def _assemble(  # noqa: PLR0913 - the sections of one packet entry
    surge: SurgeRecord,
    nodes: list[dict[str, Any]],
    *,
    quote: Post | None,
    others: list[Post],
    target_side: list[tuple[Post, str]],
    quote_side: list[tuple[Post, str]],
    quoter: str | None,
) -> Thread:
    roles = Roles(surge.target, quoter)
    ids: dict[str, str] = {}
    parents = []
    for t, node in enumerate(nodes, start=1):
        ids[node["post"]["uri"]] = f"T{t}"
        parents.append(ancestors(node, limit=2))
    for a, post in enumerate((p for chain in parents for p in chain), start=1):
        ids.setdefault(post["uri"], f"A{a}")
    if quote is not None:
        ids[quote["uri"]] = "Q"
    for x, post in enumerate(others, start=1):
        ids[post["uri"]] = f"X{x:02d}"
    for n, (post, _) in enumerate([*target_side, *quote_side], start=1):
        ids.setdefault(post["uri"], f"P{n:03d}")
    every = [n["post"] for n in nodes] + [p for c in parents for p in c]
    every += [quote] if quote is not None else []
    every += others + [p for p, _ in target_side] + [p for p, _ in quote_side]
    for post in every:
        roles.learn(post)

    def shown(post: Post) -> dict[str, str]:
        return {
            "id": ids[post["uri"]],
            "by": roles.author(post),
            "text": render(post, roles, ids),
        }

    def reply(item: tuple[Post, str]) -> dict[str, str]:
        post, parent = item
        return shown(post) | {"reply_to": ids.get(parent, "a post not shown")}

    entry: dict[str, Any] = {
        "target_posts": [
            shown(node["post"]) | {"parents": [shown(p) for p in chain]}
            for node, chain in zip(nodes, parents, strict=True)
        ],
        "quote_post": shown(quote) if quote is not None else None,
        "other_quotes": [shown(p) for p in others],
        "target_thread": [reply(p) for p in target_side],
        "quote_thread": [reply(p) for p in quote_side],
    }
    entry["label_these"] = [
        item["id"]
        for item in [
            *([entry["quote_post"]] if entry["quote_post"] else []),
            *entry["other_quotes"],
            *entry["target_thread"],
            *entry["quote_thread"],
        ]
        if item["by"] != "TARGET"
    ]
    return Thread(
        surge_id=surge.surge_id, entry=entry, key={v: k for k, v in ids.items()}
    )


def stratify(
    confirmed: dict[str, Row], unconfirmed: dict[str, Row], *, min_labeled: int = 10
) -> dict[str, str]:
    """Stratum of each judged surge, from confirmed and screen-only classes.

    - pileon: a pile-on on confirmed labels;
    - demoted: a pile-on on the screen's labels only;
    - contested: crossfire on the screen's labels, not a pile-on confirmed;
    - near: confirmed contempt share of at least 4% but no pile-on;
    - other: everything else.
    """

    def kind(row: Row) -> str:
        return surge_class(row, hostile_at=0.10, support_at=0.25)

    out = {}
    for surge_id, row in confirmed.items():
        labeled = row.get("labeled") or 0
        if labeled < min_labeled or surge_id not in unconfirmed:
            continue
        screen = kind(unconfirmed[surge_id])
        if kind(row) == "pileon":
            out[surge_id] = "pileon"
        elif screen == "pileon":
            out[surge_id] = "demoted"
        elif screen == "crossfire":
            out[surge_id] = "contested"
        elif (row.get("contempt") or 0) / labeled >= NEAR_SHARE:
            out[surge_id] = "near"
        else:
            out[surge_id] = "other"
    return out


def candidates(strata: dict[str, str], *, seed: int) -> dict[str, list[str]]:
    """Each stratum's surges in a seeded random order (for quota filling)."""
    rng = random.Random(seed)  # noqa: S311 - reproducible sampling
    out: dict[str, list[str]] = {name: [] for name in STRATA}
    for surge_id in sorted(strata):
        out[strata[surge_id]].append(surge_id)
    for members in out.values():
        rng.shuffle(members)
    return out


def write_packet(
    threads: list[tuple[str, Thread]], outdir: Path, *, chunk: int, seed: int
) -> dict[str, str]:
    """Write a blind thread packet in chunks; return surge id -> packet id.

    `threads` pairs each stratum with its thread; the strata go to
    `strata.json`, which judges must not see.
    """
    rng = random.Random(seed)  # noqa: S311 - reproducible order
    order = list(threads)
    rng.shuffle(order)
    outdir.mkdir(parents=True, exist_ok=True)
    key: dict[str, Any] = {}
    strata = {}
    lines: list[str] = []
    for n, (stratum, thread) in enumerate(order, start=1):
        packet_id = f"S{n:02d}"
        key[packet_id] = {"surge": thread.surge_id, "posts": thread.key}
        strata[packet_id] = stratum
        lines.append(json.dumps({"id": packet_id} | thread.entry, ensure_ascii=False))
    for start in range(0, len(lines), chunk):
        part = start // chunk + 1
        (outdir / f"chunk-{part}.jsonl").write_text(
            "\n".join(lines[start : start + chunk]) + "\n"
        )
    (outdir / "key.json").write_text(json.dumps(key, indent=1))
    (outdir / "strata.json").write_text(json.dumps(strata, indent=1))
    (outdir / "codebook.md").write_text(thread_codebook())
    return {v["surge"]: k for k, v in key.items()}


@dataclass(frozen=True, slots=True)
class Judgment:
    """One judge's thread run: verdicts by surge, labels by (surge, post)."""

    verdicts: dict[str, Row]
    posts: dict[tuple[str, str], Row]


def read_judgment(path: Path) -> Judgment:
    """Valid verdict and label rows from a judge's JSONL output."""
    verdicts: dict[str, Row] = {}
    posts: dict[tuple[str, str], Row] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        surge = str(row.get("surge", ""))
        if "post" in row:
            if row.get("label") in V2_LABELS and row.get("aimed") in AIMS:
                posts[surge, str(row["post"])] = row
        elif row.get("verdict") in VERDICTS:
            verdicts[surge] = row
    return Judgment(verdicts, posts)


def contempt(row: Row) -> bool:
    """Hostile or abusive, aimed at the target: what counts toward a pile-on."""
    return row["label"] in HOSTILE and row["aimed"] == "target"


def label_class(rows: Iterable[Row]) -> str | None:
    """The surge class implied by in-context labels (`None` if too few)."""
    rows = list(rows)
    if len(rows) < MIN_CLASS_LABELS:
        return None
    counts = {
        "labeled": len(rows),
        "contempt": sum(contempt(r) for r in rows),
        "supportive": sum(r["label"] == "supportive" for r in rows),
    }
    return surge_class(counts, hostile_at=0.10, support_at=0.25)


def isolated_labels(dirs: Iterable[Path]) -> dict[str, dict[str, str]]:
    """Each judge's reply-by-reply labels across item packets, by post URI."""
    out: dict[str, dict[str, str]] = {judge: {} for judge in JUDGES}
    for directory in dirs:
        key: dict[str, str] = json.loads((directory / "key.json").read_text())
        for judge in JUDGES:
            path = directory / f"{judge}.jsonl"
            if path.exists():
                for item_id, label in read_labels(path).items():
                    out[judge][key[item_id]] = label
    return out


def _kappa(
    pairs: list[tuple[str, str]],
    *,
    order: tuple[str, ...] | None = None,
    draws: int = 1000,
) -> str:
    """Agreement, kappa and its 95% bootstrap range over the pairs."""
    if not pairs:
        return "n=0"

    def value(sample: list[tuple[str, str]]) -> float:
        return weighted_kappa(sample, order=order) if order else cohens_kappa(sample)

    rng = random.Random(11)  # noqa: S311 - reproducible resampling
    band = sorted(
        value([pairs[rng.randrange(len(pairs))] for _ in pairs]) for _ in range(draws)
    )
    agree = sum(a == b for a, b in pairs) / len(pairs)
    return (
        f"n={len(pairs)} agree {agree:.0%} kappa {value(pairs):.2f} "
        f"(95% {band[int(0.025 * draws)]:.2f} to {band[int(0.975 * draws) - 1]:.2f})"
    )


def _both(values: list[str | None], wanted: str) -> str:
    hits = [v == wanted for v in values if v is not None]
    if len(hits) < len(values):
        return "?"
    return "both" if all(hits) else ("one" if any(hits) else "none")


def score(  # noqa: PLR0913 - one report, one argument per input
    outdir: Path,
    features: Path,
    labels_db: Path,
    labeler: str,
    *,
    isolated: list[Path],
    models: Sequence[str] = (),
) -> list[str]:
    """Compare holistic, in-context and reply-by-reply measures of each surge."""
    key: dict[str, Any] = json.loads((outdir / "key.json").read_text())
    strata: dict[str, str] = json.loads((outdir / "strata.json").read_text())
    judged = {j: read_judgment(outdir / f"{j}.jsonl") for j in JUDGES}
    store = LabelStore(labels_db)
    try:
        labels, directed = store.labeled(labeler), store.directed(labeler)
    finally:
        store.close()
    cascade = {
        uri: "neutral" if label in HOSTILE and directed.get(uri) != "author" else label
        for uri, label in labels.items()
    }
    uris = {
        (surge, post): uri
        for surge, entry in key.items()
        for post, uri in entry["posts"].items()
    }
    return [
        *_agreement(*(judged[j] for j in JUDGES)),
        *_class_agreement(key, judged, cascade),
        *_context_effect(judged, uris, isolated_labels(isolated), cascade),
        *(
            line
            for name in models
            for line in _model_lines(
                name, read_judgment(outdir / f"{name}.jsonl"), judged
            )
        ),
        "",
        *_surge_table(key, strata, judged, _rows(features), cascade),
    ]


def _model_lines(name: str, model: Judgment, judged: dict[str, Judgment]) -> list[str]:
    """A local model's verdicts and in-context labels against each judge."""
    lines = []
    for judge in JUDGES:
        other = judged[judge]
        surges = sorted(set(model.verdicts) & set(other.verdicts))
        posts = sorted(set(model.posts) & set(other.posts))

        def pileon(j: Judgment, s: str) -> str:
            return str(j.verdicts[s]["verdict"] == "pileon")

        lines += [
            f"{name} vs {judge} pile-on vs not:  "
            + _kappa([(pileon(model, s), pileon(other, s)) for s in surges]),
            f"{name} vs {judge} labels (weighted): "
            + _kappa(
                [(model.posts[p]["label"], other.posts[p]["label"]) for p in posts],
                order=V2_LABELS,
            ),
        ]
    return lines


def _agreement(a: Judgment, b: Judgment) -> list[str]:
    """How well the two judges agree on each measure."""
    shared = sorted(set(a.verdicts) & set(b.verdicts))

    def pair(field: str, fn: Callable[[Any], str] = str) -> list[tuple[str, str]]:
        return [(fn(a.verdicts[s][field]), fn(b.verdicts[s][field])) for s in shared]

    posts = sorted(set(a.posts) & set(b.posts))
    labels = [(a.posts[p]["label"], b.posts[p]["label"]) for p in posts]
    hostile = [(str(contempt(a.posts[p])), str(contempt(b.posts[p]))) for p in posts]
    return [
        f"{len(shared)} surges with both verdicts ({' vs '.join(JUDGES)})",
        "holistic verdict (5 classes): " + _kappa(pair("verdict")),
        "holistic pile-on vs not:      "
        + _kappa(pair("verdict", lambda v: str(v == "pileon"))),
        "severity 0-3 (weighted):      " + _kappa(pair("severity"), order=SEVERITIES),
        "piled_on:                     "
        + _kappa(pair("piled_on", lambda v: str(bool(v)))),
        "in-context labels (weighted): " + _kappa(labels, order=V2_LABELS),
        "in-context contempt:          " + _kappa(hostile),
    ]


def pileon_kappa(pairs: list[tuple[bool, bool]]) -> str:
    """Pile-on vs not kappa; undefined when a rater makes under two calls."""
    calls = [sum(a for a, _ in pairs), sum(b for _, b in pairs)]
    if min(calls) < 2:  # noqa: PLR2004 - the pre-registered floor
        return f"n={len(pairs)} pile-on calls {calls[0]}/{calls[1]}: kappa undefined"
    text = _kappa([(str(a), str(b)) for a, b in pairs])
    return f"{text}; pile-on calls {calls[0]}/{calls[1]}"


def _class_agreement(
    key: dict[str, Any], judged: dict[str, Judgment], cascade: dict[str, str]
) -> list[str]:
    """Judge-vs-judge agreement on pile-on vs not, for H, C and A."""
    a, b = (judged[j] for j in JUDGES)
    holistic, sampled, every = [], [], []
    for surge in sorted(set(a.verdicts) & set(b.verdicts)):
        posts = key[surge]["posts"]
        holistic.append(
            (
                a.verdicts[surge]["verdict"] == "pileon",
                b.verdicts[surge]["verdict"] == "pileon",
            )
        )
        mine = [[(p, r) for (s, p), r in j.posts.items() if s == surge] for j in (a, b)]
        c = [label_class(r for p, r in m if posts.get(p) in cascade) for m in mine]
        e = [label_class(r for _, r in m) for m in mine]
        if None not in c:
            sampled.append((c[0] == "pileon", c[1] == "pileon"))
        if None not in e:
            every.append((e[0] == "pileon", e[1] == "pileon"))
    return [
        "kappa(H) holistic:             " + pileon_kappa(holistic),
        "kappa(C) in-context, sampled:  " + pileon_kappa(sampled),
        "kappa(A) in-context, all:      " + pileon_kappa(every),
    ]


def _context_effect(
    judged: dict[str, Judgment],
    uris: dict[tuple[str, str], str],
    alone: dict[str, dict[str, str]],
    cascade: dict[str, str],
) -> list[str]:
    """Each judge's labels in context against its own labels made one by one."""
    lines = []
    for judge in JUDGES:
        moved: Counter[tuple[bool, bool]] = Counter()
        pairs, screened = [], []
        for ref, row in judged[judge].posts.items():
            uri = uris.get(ref, "")
            if uri in alone[judge]:
                pairs.append((alone[judge][uri], row["label"]))
                moved[alone[judge][uri] in HOSTILE, contempt(row)] += 1
            if uri in cascade:
                screened.append((cascade[uri], row["label"]))
        lines += [
            (
                f"{judge} alone vs in context:   {_kappa(pairs, order=V2_LABELS)}; "
                f"contempt kept {moved[True, True]}, lost {moved[True, False]}, "
                f"gained {moved[False, True]}"
            ),
            f"cascade vs {judge} in context: {_kappa(screened, order=V2_LABELS)}",
        ]
    return lines


def _surge_table(
    key: dict[str, Any],
    strata: dict[str, str],
    judged: dict[str, Judgment],
    rows: dict[str, Row],
    cascade: dict[str, str],
) -> list[str]:
    """One line per surge, then pile-on calls by stratum."""
    lines = [
        "surge  stratum    cascade   holistic ctx-sampled ctx-all  piled_on severity"
    ]
    by_stratum: dict[str, Counter[str]] = {}
    for surge in sorted(key):
        posts = key[surge]["posts"]
        mine = {
            j: [(p, r) for (s, p), r in judged[j].posts.items() if s == surge]
            for j in JUDGES
        }
        sampled = [
            label_class(r for p, r in mine[j] if posts.get(p) in cascade)
            for j in JUDGES
        ]
        every = [label_class(r for _, r in mine[j]) for j in JUDGES]
        verdicts = [judged[j].verdicts.get(surge) for j in JUDGES]
        row = rows.get(key[surge]["surge"])
        calls = {
            "cascade": surge_class(row, hostile_at=0.10, support_at=0.25)
            if row
            else "?",
            "holistic": _both(
                [v["verdict"] if v else None for v in verdicts], "pileon"
            ),
            "ctx": _both(sampled, "pileon"),
            "ctx_all": _both(every, "pileon"),
            "piled": _both(
                [str(bool(v["piled_on"])) if v else None for v in verdicts], "True"
            ),
        }
        severity = "/".join(str(v["severity"]) if v else "?" for v in verdicts)
        stratum = strata.get(surge, "?")
        tally = by_stratum.setdefault(stratum, Counter())
        tally.update(["n", f"holistic {calls['holistic']}", f"ctx {calls['ctx']}"])
        lines.append(
            f"{surge:<6} {stratum:<10} {calls['cascade']:<9} {calls['holistic']:<8} "
            f"{calls['ctx']:<11} {calls['ctx_all']:<8} {calls['piled']:<8} {severity}"
        )
    lines.append("")
    for stratum in STRATA:
        if stratum in by_stratum:
            tally = by_stratum[stratum]
            lines.append(
                f"{stratum:<10} n={tally['n']:>2}  holistic pile-on both "
                f"{tally['holistic both']}, one {tally['holistic one']}; in-context "
                f"(sampled replies) both {tally['ctx both']}, one {tally['ctx one']}"
            )
    return lines


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "severity": {"type": "integer", "minimum": 0, "maximum": 3},
        "piled_on": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["verdict", "severity", "piled_on", "confidence"],
}
POST_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(V2_LABELS)},
        "aimed": {"type": "string", "enum": list(AIMS)},
    },
    "required": ["label", "aimed"],
}


def surge_text(entry: dict[str, Any]) -> str:
    """One packet entry as plain text, for a local model."""
    lines = []
    for post in entry["target_posts"]:
        lines += [
            f"{parent['id']} by {parent['by']} (earlier): {parent['text']}"
            for parent in post["parents"]
        ]
        lines.append(f"{post['id']} by TARGET: {post['text']}")
    if entry["quote_post"]:
        lines.append(f"Q by QUOTER (quote post): {entry['quote_post']['text']}")
    for section, title in (
        ("other_quotes", "Other quote posts of the TARGET"),
        ("target_thread", "Replies under the TARGET's posts"),
        ("quote_thread", "Replies under the QUOTER's post"),
    ):
        if entry[section]:
            lines.append(f"\n{title}:")
        for post in entry[section]:
            to = f" (reply to {post['reply_to']})" if "reply_to" in post else ""
            lines.append(f"{post['id']} by {post['by']}{to}: {post['text']}")
    return "\n".join(lines)


def run_model(outdir: Path, model: Ollama, out: Path) -> int:
    """Verdicts for every surge, then in-context labels; resumes from `out`.

    Every request for one surge starts with the same text (codebook, then the
    surge), so the server can reuse its prompt cache across that surge's
    posts.
    """
    entries = [
        json.loads(line)
        for chunk in sorted(outdir.glob("chunk-*.jsonl"))
        for line in chunk.read_text().splitlines()
        if line.strip()
    ]
    done = read_judgment(out) if out.exists() else Judgment({}, {})
    asked = 0
    with out.open("a") as sink:

        def write(row: Row) -> None:
            sink.write(json.dumps(row) + "\n")
            sink.flush()

        for entry in entries:
            if entry["id"] in done.verdicts:
                continue
            context = f"{thread_codebook()}\n\nSURGE\n{surge_text(entry)}\n\n"
            raw = model.ask(
                context + "Give your verdict for this whole surge. Reply with ONLY "
                'JSON: {"verdict": ..., "severity": 0-3, "piled_on": true|false, '
                '"confidence": 0.0-1.0}',
                schema=VERDICT_SCHEMA,
            )
            write({"surge": entry["id"], **_json_or_empty(raw)})
            asked += 1
        for entry in entries:
            context = f"{thread_codebook()}\n\nSURGE\n{surge_text(entry)}\n\n"
            for post in entry["label_these"]:
                if (entry["id"], post) in done.posts:
                    continue
                raw = model.ask(
                    context + f"Label post {post} in context. Reply with ONLY JSON: "
                    '{"label": ..., "aimed": "target"|"quoter"|"other"|"none"}',
                    schema=POST_SCHEMA,
                )
                write({"surge": entry["id"], "post": post, **_json_or_empty(raw)})
                asked += 1
                if asked % 50 == 0:
                    print(f"[threads run] {asked}", file=sys.stderr, flush=True)
    return asked


def _json_or_empty(raw: str) -> Row:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _rows(path: Path) -> dict[str, Row]:
    return {r["incident_id"]: r for r in map(json.loads, path.read_text().splitlines())}


def _quotas(text: str) -> dict[str, int]:
    out = dict(QUOTAS)
    for part in filter(None, text.split(",")):
        name, _, count = part.partition("=")
        if name not in STRATA:
            msg = f"unknown stratum {name!r}; known: {', '.join(STRATA)}"
            raise argparse.ArgumentTypeError(msg)
        out[name] = int(count)
    return out


def _pools(args: argparse.Namespace) -> tuple[dict[str, list[str]], dict[str, int]]:
    """Surges to build per stratum, and how many of each to take."""
    if args.plan:
        planned: dict[str, Any] = json.loads(args.plan.read_text())
        pools: dict[str, list[str]] = {}
        for surge_id in sorted(planned):
            kind = planned[surge_id]["stratum"]
            if kind != "unsampled":
                pools.setdefault(kind, []).append(surge_id)
        return pools, {kind: len(members) for kind, members in pools.items()}
    if not (args.features and args.screen_features):
        msg = "give --plan, or --features and --screen-features"
        raise SystemExit(msg)
    strata = stratify(_rows(args.features), _rows(args.screen_features))
    return candidates(strata, seed=args.seed), args.quotas


def packet(args: argparse.Namespace) -> None:
    """Build every planned surge, or fill each interim stratum's quota."""
    pools, quotas = _pools(args)
    chosen = sample(
        list(read_surges(args.surges)),
        size=args.sample_size,
        seed=args.seed,
        include_warmup=args.include_warmup,
    )
    surges = {s.surge_id: s for s in chosen}

    async def build() -> tuple[list[tuple[str, Thread]], Counter[str]]:
        fetcher = Fetcher(Cache(args.cache))
        built: list[tuple[str, Thread]] = []
        skipped: Counter[str] = Counter()
        try:
            for stratum, members in pools.items():
                taken = 0
                for surge_id in members:
                    if taken >= quotas[stratum]:
                        break
                    surge = surges.get(surge_id)
                    if surge is None:
                        skipped["not in sample"] += 1
                        continue
                    picks = {
                        uri
                        for uri, _ in choose(
                            surge, per_surge=args.per_surge, seed=args.seed
                        )
                    }
                    thread = await build_thread(
                        surge, fetcher, picks=picks, seed=args.seed, cap=args.cap
                    )
                    if thread is None:
                        skipped[f"{stratum}: target post gone"] += 1
                        continue
                    built.append((stratum, thread))
                    taken += 1
                    print(f"[threads] {stratum} {taken}", file=sys.stderr, flush=True)
        finally:
            await fetcher.close()
        return built, skipped

    built, skipped = asyncio.run(build())
    write_packet(built, args.dir, chunk=args.chunk, seed=args.seed)
    shown = [len(t.entry["label_these"]) for _, t in built]
    strata_built = dict(Counter(s for s, _ in built))
    print(
        f"wrote {len(built)} surges to {args.dir} ({sum(shown)} posts to label, "
        f"{min(shown, default=0)}-{max(shown, default=0)} per surge); "
        f"strata {strata_built}; skipped {dict(skipped)}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pk = sub.add_parser("packet")
    pk.add_argument("--surges", type=Path, required=True)
    pk.add_argument("--cache", type=Path, required=True)
    pk.add_argument("--features", type=Path, help="confirmed labels")
    pk.add_argument("--screen-features", type=Path, help="screen only")
    pk.add_argument("--plan", type=Path, help="a held-out plan (heldout plan)")
    pk.add_argument("--sample-size", type=int, default=500)
    pk.add_argument("--per-surge", type=int, default=30)
    pk.add_argument("--seed", type=int, default=20261017)
    pk.add_argument("--include-warmup", action="store_true")
    pk.add_argument("--quotas", type=_quotas, default=dict(QUOTAS), help="e.g. near=6")
    pk.add_argument("--cap", type=int, default=30, help="posts per thread side")
    pk.add_argument("--chunk", type=int, default=10, help="surges per judge file")
    pk.add_argument("--dir", type=Path, required=True)
    sc = sub.add_parser("score")
    sc.add_argument("--dir", type=Path, required=True)
    sc.add_argument("--features", type=Path, required=True)
    sc.add_argument("--labels", type=Path, required=True)
    sc.add_argument("--labeler", default="gemma4:31b+contrast@confirmed")
    sc.add_argument("--isolated", type=Path, nargs="*", default=[])
    sc.add_argument("--models", nargs="*", default=[], help="local model outputs")
    rn = sub.add_parser("run", help="verdicts and in-context labels from a local model")
    rn.add_argument("--dir", type=Path, required=True)
    rn.add_argument("--ollama", default="http://localhost:11436")
    rn.add_argument("--model", default="gemma4:31b")
    rn.add_argument("--num-ctx", type=int, default=16384)
    args = parser.parse_args(argv)
    if args.command == "packet":
        packet(args)
    elif args.command == "run":
        model = Ollama(args.ollama, args.model, num_ctx=args.num_ctx)
        try:
            out = args.dir / f"{args.model.replace(':', '_')}.jsonl"
            print(f"asked {run_model(args.dir, model, out)}", file=sys.stderr)
        finally:
            model.close()
    else:
        lines = score(
            args.dir,
            args.features,
            args.labels,
            args.labeler,
            isolated=args.isolated,
            models=args.models,
        )
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
