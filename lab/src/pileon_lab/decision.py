"""Valence from decision models, through Ollama's System One endpoint.

Decision models (Ollama 0.35+, `POST /v1/systemone`) answer with a probability
per option instead of text, so every threshold on their output is a dial with
a measurable tradeoff. One request asks three questions about a reply, from
the same context the chat labelers get (target handle, the amplifier's
framing, the target post):

- `stance` (`choice`): the codebook v2 label, with each label's definition.
- `aimed` (`noul`): whether the reply's negativity is aimed at the target,
  the job of the chat labelers' second pass.
- `severity` (`score`): the probability-weighted zero-based level on the
  ordered v2 scale (0 supportive ... 4 abusive).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from pileon_lab.codebook import V2_DEFINITIONS, V2_LABELS

HOSTILE = ("hostile", "abusive")

STANCE = (
    "What is the stance of `reply` toward the TARGET, the person named in "
    "`target`, who wrote `target_post` and was just quote-posted by a "
    "prominent account whose commentary is `framing`? Judge stance toward the "
    "TARGET only. If `target_post` is about a third party (a politician, "
    "official, or story subject) and `reply` attacks THAT person, the reply "
    "is neutral or supportive toward the target, not hostile. Attacking the "
    "prominent account in the target's defense is supportive. Sarcasm counts "
    "by intent, not literal wording. If `target_post` is unavailable, judge "
    "only what `reply` makes evident."
)
AIMED = (
    "Is any negativity in `reply` aimed at the TARGET, the person named in "
    "`target` who wrote `target_post`?"
)
AIMED_CRITERIA = {
    "false": (
        "There is no negativity, or it is aimed at someone else: a person the "
        "target posted about, the prominent account, or another replier"
    ),
    "true": "Negativity in the reply is aimed at the TARGET",
}
SEVERITY = "How hostile toward the TARGET (`target`) is `reply`?"


@dataclass(frozen=True, slots=True)
class Decision:
    """One reply's answers; `label` is `parse_error` when unusable."""

    label: str
    confidence: float
    probabilities: dict[str, float]
    aimed: float | None
    severity: float | None

    @property
    def p_hostile(self) -> float:
        """Probability of hostile or abusive."""
        return sum(self.probabilities.get(label, 0.0) for label in HOSTILE)


def gate(label: str, aimed: float | None) -> str:
    """Fold hostility that is not aimed at the target into neutral."""
    if label in HOSTILE and aimed is not None and aimed < 0.5:  # noqa: PLR2004 - even odds
        return "neutral"
    return label


def request(
    model: str, handle: str, reply: str, *, target_post: str, framing: str
) -> dict[str, Any]:
    """The System One request body for one reply."""
    return {
        "model": model,
        "state": {
            "target": f"@{handle or 'unknown'}",
            "framing": framing.strip() or "(none)",
            "target_post": target_post.strip() or "(unavailable)",
            "reply": reply,
        },
        "questions": {
            "stance": {
                "type": "choice",
                "instructions": STANCE,
                "criteria": dict(V2_DEFINITIONS),
            },
            "aimed": {
                "type": "noul",
                "instructions": AIMED,
                "criteria": AIMED_CRITERIA,
            },
            "severity": {
                "type": "score",
                "instructions": SEVERITY,
                "criteria": [
                    f"{label}: {V2_DEFINITIONS[label]}" for label in V2_LABELS
                ],
            },
        },
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse(body: dict[str, Any]) -> Decision:
    """Read a System One response; missing pieces become `None` or empty."""
    answers = body.get("answers") or {}
    stance = answers.get("stance") or {}
    choice = stance.get("choice")
    probabilities = {
        str(k): p
        for k, v in (stance.get("probabilities") or {}).items()
        if (p := _number(v)) is not None
    }
    return Decision(
        label=choice if choice in V2_LABELS else "parse_error",
        confidence=_number(stance.get("confidence")) or 0.0,
        probabilities=probabilities,
        aimed=_number((answers.get("aimed") or {}).get("noul")),
        severity=_number((answers.get("severity") or {}).get("score")),
    )


class SystemOne:
    """Decisions from a local Ollama server's System One endpoint."""

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

    def decide(
        self, handle: str, reply: str, *, target_post: str, framing: str
    ) -> Decision:
        response = self._http.post(
            "/v1/systemone",
            json=request(
                self.model, handle, reply, target_post=target_post, framing=framing
            ),
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return parse(body)

    def close(self) -> None:
        self._http.close()
