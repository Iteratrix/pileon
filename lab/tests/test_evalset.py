from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pileon_lab.evalset import read_labels, write_bulk
from pileon_lab.label import Item, LabelStore

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
