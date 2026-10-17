import json

import httpx

from pileon_lab.codebook import LABEL_SCHEMA, PROMPT_V2, V2_DEFINITIONS, V2_LABELS
from pileon_lab.decision import SystemOne, gate, parse, request
from pileon_lab.evalset import chat_labeler, run_variant, write_gated
from pileon_lab.label import Ollama

ROW = {
    "id": "n001",
    "target": "someone.bsky.social",
    "framing": "",
    "target_post": "A take.",
    "reply": "You are a clown.",
}
ANSWER = {
    "model": "clef-flash",
    "answers": {
        "stance": {
            "type": "choice",
            "choice": "hostile",
            "probabilities": {
                "supportive": 0.01,
                "neutral": 0.04,
                "critical": 0.15,
                "hostile": 0.7,
                "abusive": 0.1,
            },
            "confidence": 0.6,
        },
        "aimed": {"type": "noul", "noul": 0.2},
        "severity": {"type": "score", "score": 2.9, "confidence": 0.5},
    },
}


def test_definitions_are_worded_exactly_as_in_the_study_prompt():
    assert tuple(V2_DEFINITIONS) == V2_LABELS
    for label, definition in V2_DEFINITIONS.items():
        assert f"- {label}: {definition}\n" in PROMPT_V2
    assert LABEL_SCHEMA["properties"]["label"]["enum"] == list(V2_LABELS)


def test_request_asks_stance_direction_and_severity_with_context():
    body = request("clef", "", "reply", target_post="", framing="  ")
    assert body["state"] == {
        "target": "@unknown",
        "framing": "(none)",
        "target_post": "(unavailable)",
        "reply": "reply",
    }
    questions = body["questions"]
    assert list(questions["stance"]["criteria"]) == list(V2_LABELS)
    assert questions["aimed"]["type"] == "noul"
    assert set(questions["aimed"]["criteria"]) == {"false", "true"}
    assert [c.split(":")[0] for c in questions["severity"]["criteria"]] == list(
        V2_LABELS
    )


def test_parse_reads_answers_and_tolerates_gaps():
    decision = parse(ANSWER)
    assert decision.label == "hostile"
    assert abs(decision.p_hostile - 0.8) < 1e-9
    assert (decision.aimed, decision.severity) == (0.2, 2.9)
    empty = parse({"answers": {"stance": {"choice": "rude"}}})
    assert empty.label == "parse_error"
    assert (empty.aimed, empty.severity, empty.probabilities) == (None, None, {})


def test_gate_folds_only_unaimed_hostility():
    assert gate("hostile", 0.2) == "neutral"
    assert gate("abusive", 0.9) == "abusive"
    assert gate("hostile", None) == "hostile"
    assert gate("critical", 0.0) == "critical"


def test_clients_post_the_right_bodies():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/v1/systemone":
            return httpx.Response(200, json=ANSWER)
        content = json.dumps({"label": "hostile", "confidence": 0.9})
        return httpx.Response(200, json={"message": {"content": content}})

    transport = httpx.MockTransport(handler)
    decider = SystemOne("http://ollama", "clef-flash", transport=transport)
    assert decider.decide("h", "r", target_post="p", framing="f").label == "hostile"
    chat = Ollama("http://ollama", "llama", transport=transport)
    assert chat_labeler(chat, "v2+json")(ROW) == {
        "label": "hostile",
        "confidence": 0.9,
    }
    chat_labeler(chat, "v2")(ROW)
    paths = [path for path, _ in seen]
    assert paths == ["/v1/systemone", "/api/chat", "/api/chat"]
    assert seen[1][1]["format"] == LABEL_SCHEMA
    assert "format" not in seen[2][1]


def test_runs_resume_and_gated_copies_fold_unaimed_hostility(tmp_path):
    packet = tmp_path / "packet.jsonl"
    packet.write_text(
        "\n".join(json.dumps({**ROW, "id": f"n{i}"}) for i in range(3)) + "\n"
    )
    out = tmp_path / "m+systemone.jsonl"
    out.write_text(json.dumps({"id": "n0", "label": "neutral"}) + "\n")
    calls = []

    def labeler(row):
        calls.append(row["id"])
        return {"label": "hostile", "aimed": 0.1, "probabilities": {"hostile": 0.9}}

    assert run_variant(packet, out, labeler) == 2
    assert calls == ["n1", "n2"]
    gated = tmp_path / "m+systemone-gated.jsonl"
    write_gated(out, gated)
    labels = [json.loads(line)["label"] for line in gated.read_text().splitlines()]
    assert labels == ["neutral", "neutral", "neutral"]
