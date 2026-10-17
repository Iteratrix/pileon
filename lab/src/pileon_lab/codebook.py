"""Valence codebook v2: stance toward the target of a surge.

Five labels from supportive to abusive, with the rules that keep labels about
the target (not third parties the target posted about) and that count
defending the target against the amplifier as supportive. Labelers and gold
judges must see exactly this text, so it lives in one place.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

V2_LABELS = ("supportive", "neutral", "critical", "hostile", "abusive")
GOLD_LABELS = ("supportive", "neutral", "critical", "hostile")
"""The four-point human gold scale, ordered low to high hostility."""

PROMPT_V2 = (
    "You are labeling the stance of a social-media reply toward a "
    "specific person (the TARGET), who was just quote-posted by a "
    "prominent account. Use exactly one label:\n"
    "- supportive: defends, agrees with, praises, or sympathizes with "
    "the target; includes attacking the target's critics on the "
    "target's behalf\n"
    "- neutral: informational, off-topic, a question, or not "
    "evaluative toward the target; includes negativity aimed at "
    "someone OTHER than the target\n"
    "- critical: substantive disagreement with or negative judgment of "
    "the target's post or views, without personal contempt\n"
    "- hostile: personal contempt aimed at the target - insults, "
    "mockery, ridicule, name-calling - but no slurs, threats, or "
    "dehumanization\n"
    "- abusive: slurs, threats, wishes of harm or death, sexual "
    "degradation, or dehumanization aimed at the target\n"
    "\n"
    "Rules:\n"
    "1. Judge stance toward the TARGET only. If the target's post is "
    "about a third party (a politician, official, or story subject) "
    "and the reply attacks THAT person, the reply is neutral or "
    "supportive toward the target - not hostile.\n"
    "2. Attacking the prominent account who quoted the target, in the "
    "target's defense, is supportive.\n"
    "3. Sarcasm counts by intent, not literal wording.\n"
    "4. If the target's post is unavailable, judge only what the reply "
    "makes evident; do not assume it attacks the target.\n"
    "\n"
    "Examples:\n"
    '- "This misreads the CBO score; the real number is 3%." -> '
    "critical\n"
    '- "You are a clown and everyone is laughing at you. Log off." -> '
    "hostile\n"
    '- "Die in a fire, you subhuman filth." -> abusive\n'
    "- (target is a reporter announcing an official's removal) \"Good "
    'riddance, that fascist pig can rot." -> neutral\n'
    '- "Imagine siccing 100k followers on a grad student. YOU are the '
    'problem." -> supportive\n'
    "\n"
    'Reply with ONLY a JSON object: {"label": ..., "confidence": '
    "<0.0-1.0>}\n"
    "\n"
    "TARGET: @{target}\n"
    "POST: {text}"
)


V2_DEFINITIONS = {
    "supportive": (
        "defends, agrees with, praises, or sympathizes with the target; "
        "includes attacking the target's critics on the target's behalf"
    ),
    "neutral": (
        "informational, off-topic, a question, or not evaluative toward the "
        "target; includes negativity aimed at someone OTHER than the target"
    ),
    "critical": (
        "substantive disagreement with or negative judgment of the target's "
        "post or views, without personal contempt"
    ),
    "hostile": (
        "personal contempt aimed at the target - insults, mockery, ridicule, "
        "name-calling - but no slurs, threats, or dehumanization"
    ),
    "abusive": (
        "slurs, threats, wishes of harm or death, sexual degradation, or "
        "dehumanization aimed at the target"
    ),
}
"""Each v2 label's definition, worded exactly as in `PROMPT_V2`."""

LABEL_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": list(V2_LABELS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["label", "confidence"],
}
"""JSON schema for structured output: the reply `PROMPT_V2` asks for."""


def codebook_text() -> str:
    """The codebook as a labeler sees it, without the prompt's I/O lines."""
    return PROMPT_V2.partition("Reply with ONLY")[0].rstrip() + "\n"


def to_gold(label: str, *, mapping: str = "B") -> str:
    """Map a five-point v2 label onto the four-point gold scale.

    Mapping B (primary) folds hostile and abusive into gold "hostile", which
    matches the human codebook's broad top class. Mapping A treats only
    abusive as hostile and v2 hostile as critical.
    """
    if label == "abusive":
        return "hostile"
    if label == "hostile":
        return "hostile" if mapping == "B" else "critical"
    return label


DIRECTIONS = ("author", "third_party", "unclear")

DIRECTION_PROMPT = (
    "A Bluesky post by @{handle} received the reply below. The reply has "
    "been flagged as abusive. Your ONLY job is to decide who the abuse is "
    "aimed at:\n"
    "- author: @{handle}, the person who wrote the original post\n"
    "- third_party: someone else - a person mentioned, quoted, or depicted "
    "in the original post (e.g. the subject of a news story or clip)\n"
    "- unclear: cannot tell\n\n"
    "Original post by @{handle}:\n{target_post}\n\n"
    "Reply:\n{text}\n\n"
    'Respond with ONLY JSON: {{"direction": "author"|"third_party"|"unclear"}}'
)


def valence_prompt(handle: str, text: str, *, target_post: str, framing: str) -> str:
    """The v2 prompt with context: the amplifier's framing and the target post.

    Without the target post, labels flip on two in five items, so context is
    always supplied when it exists.
    """
    base = PROMPT_V2.replace("{target}", handle or "unknown").replace("{text}", text)
    context = ""
    if framing.strip():
        context += f"\nThe prominent account's framing: {framing}"
    if target_post.strip():
        context += f"\nWhat @{handle or 'unknown'} (the TARGET) said: {target_post}"
    marker = "\nTARGET: @"
    head, _, tail = base.partition(marker)
    return f"{head}{context}{marker}{tail}"


def direction_prompt(handle: str, text: str, *, target_post: str) -> str:
    """Asks who a hostile or abusive reply is aimed at."""
    return (
        DIRECTION_PROMPT.replace("{handle}", handle or "unknown")
        .replace("{target_post}", target_post or "(unavailable)")
        .replace("{text}", text)
        .replace("{{", "{")
        .replace("}}", "}")
    )


def _json_object(raw: str) -> dict[str, object] | None:
    cleaned = re.sub(r"```(?:json)?|```", "", raw).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_label(raw: str) -> tuple[str, float]:
    """Returns (label, confidence) from a labeler reply; `parse_error` if unusable."""
    obj = _json_object(raw)
    if obj is None or obj.get("label") not in V2_LABELS:
        return "parse_error", 0.0
    try:
        confidence = float(str(obj.get("confidence", 0.0)))
    except ValueError:
        confidence = 0.0
    return str(obj["label"]), min(1.0, max(0.0, confidence))


def parse_direction(raw: str) -> str:
    """Returns the direction from a reply; `parse_error` if unusable."""
    obj = _json_object(raw)
    direction = str((obj or {}).get("direction", "")).strip().lower()
    return direction if direction in DIRECTIONS else "parse_error"


CONTRAST_EXAMPLES = (
    "More examples at the hard boundaries:\n"
    '- "Ok but this ignores that the bill already passed the Senate?" -> critical '
    "(disagreement, no contempt)\n"
    '- "Imagine being this confidently wrong lmao" -> hostile (mockery of the '
    "target, though mild)\n"
    "- (target posted a clip of a politician) "
    '"This ghoul should be in prison." -> neutral (abuse aimed at the politician, '
    "not the target)\n"
    '- "Wow, the reply guys showed up fast. She is right and you all know it." '
    "-> supportive (defends the target against the pile-on)\n"
    '- "Great take. Truly visionary stuff here, genius." -> hostile when it is '
    "sarcastic mockery of the target, critical when it is sarcasm about the "
    "argument only\n"
    '- "Who is this person and why should I care" -> hostile (dismissive '
    "contempt for the person)\n\n"
)


def valence_prompt_contrast(
    handle: str, text: str, *, target_post: str, framing: str
) -> str:
    """The v2 prompt plus worked examples at the critical/hostile and
    third-party boundaries."""
    prompt = valence_prompt(handle, text, target_post=target_post, framing=framing)
    marker = "Reply with ONLY"
    head, _, tail = prompt.partition(marker)
    return f"{head}{CONTRAST_EXAMPLES}{marker}{tail}"


def valence_prompt_reason(
    handle: str, text: str, *, target_post: str, framing: str
) -> str:
    """The v2 prompt, asking for a one-sentence reason before the label."""
    prompt = valence_prompt(handle, text, target_post=target_post, framing=framing)
    return prompt.replace(
        'Reply with ONLY a JSON object: {"label": ..., "confidence": <0.0-1.0>}',
        "Reply with ONLY a JSON object: "
        '{"reason": "<one sentence on who the reply is aimed at and how>", '
        '"label": ..., "confidence": <0.0-1.0>}',
    )


PromptBuilder = Callable[[str, str, str, str], str]
"""(target handle, reply text, target post, framing) -> prompt."""

VARIANTS: dict[str, PromptBuilder] = {
    "v2": lambda handle, text, target_post, framing: valence_prompt(
        handle, text, target_post=target_post, framing=framing
    ),
    "contrast": lambda handle, text, target_post, framing: valence_prompt_contrast(
        handle, text, target_post=target_post, framing=framing
    ),
    "reason": lambda handle, text, target_post, framing: valence_prompt_reason(
        handle, text, target_post=target_post, framing=framing
    ),
}
"""Named prompt variants a labeler can run; the name is part of its tag."""
