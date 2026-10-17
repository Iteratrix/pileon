from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from pileon_lab.evalset import direct_variant, read_labels, write_bulk
from pileon_lab.label import Item, LabelStore, Ollama

if TYPE_CHECKING:
    from pathlib import Path


def _item(uri: str) -> Item:
    return Item(
        uri=uri, surge_id="s", target_handle="t", text="x", target_post="", framing=""
    )


def test_write_bulk_folds_hostility_not_aimed_at_the_author(tmp_path: Path) -> None:
    db = tmp_path / "labels.db"
    store = LabelStore(db)
    labeler = "m:1+v2"
    for uri, label in [
        ("at://a", "hostile"),
        ("at://b", "abusive"),
        ("at://c", "hostile"),
        ("at://d", "critical"),
    ]:
        store.save_label(_item(uri), labeler, label, 0.9, "{}")
    store.save_direction(_item("at://a"), labeler, "author", "{}")
    store.save_direction(_item("at://b"), labeler, "third_party", "{}")
    store.close()
    key = {
        "n1": "at://a",
        "n2": "at://b",
        "n3": "at://c",
        "n4": "at://d",
        "n5": "at://e",
    }
    (tmp_path / "key.json").write_text(json.dumps(key))

    stored, folded = write_bulk(tmp_path, db, labeler)

    assert stored.name == "m_1+v2@bulk.jsonl"
    assert read_labels(stored) == {
        "n1": "hostile",
        "n2": "abusive",
        "n3": "hostile",
        "n4": "critical",
    }
    assert read_labels(folded) == {
        "n1": "hostile",
        "n2": "neutral",
        "n3": "neutral",
        "n4": "critical",
    }


def test_direct_variant_asks_only_about_hostility(tmp_path: Path) -> None:
    rows = [
        {"id": f"n{n}", "target": "t", "framing": "", "target_post": "p", "reply": r}
        for n, r in enumerate(["aimed", "elsewhere", "kind"], start=1)
    ]
    packet = tmp_path / "packet.jsonl"
    packet.write_text("".join(json.dumps(row) + "\n" for row in rows))
    source = tmp_path / "m+v2.jsonl"
    source.write_text(
        "".join(
            json.dumps({"id": i, "label": label, "confidence": 0.9}) + "\n"
            for i, label in [("n1", "hostile"), ("n2", "abusive"), ("n3", "neutral")]
        )
    )
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompt = json.loads(request.content)["messages"][0]["content"]
        asked.append(prompt)
        direction = "third_party" if "elsewhere" in prompt else "author"
        content = json.dumps({"direction": direction})
        return httpx.Response(200, json={"message": {"content": content}})

    model = Ollama("http://x", "m", transport=httpx.MockTransport(handler))
    out = tmp_path / "m+v2-directed.jsonl"

    assert direct_variant(packet, source, out, model) == 2
    assert len(asked) == 2
    assert read_labels(out) == {"n1": "hostile", "n2": "neutral", "n3": "neutral"}
    assert direct_variant(packet, source, out, model) == 0
