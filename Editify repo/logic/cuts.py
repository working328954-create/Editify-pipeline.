"""Operator + rulebook cut planning (Stage 4, never destructive).

This module turns the operator's refined transcript plus the operator CUT
DECISIONS file and the client Rulebook into the final cut plan:

    refined transcript + operator DROP ranges + Rulebook  ->  cut plan
    (DROP ranges + KEEP ranges with timestamps)

Contract
--------
Operator CUT DECISIONS file format is JSON::

    [
      {"start_seconds": 12.0, "end_seconds": 18.5, "reason": "bad take"},
      {"start_seconds": 40.0, "end_seconds": 41.2, "reason": "bad pause"}
    ]

Rulebook format is Markdown with two sections, rules written as ``- key: value``::

    ## GLOBAL RULES
    - remove_fillers: true
    - filler_words: um, umm, uh, hmm
    - max_pause_seconds: 0.8
    - min_keep_seconds: 0.25

``PROCESS-SPECIFIC RULES`` override ``GLOBAL RULES``.  When no rulebook is
available the engine falls back to safe defaults: remove filler words and cut
pauses longer than 0.8 seconds.  Nothing else is guessed.

Missing input (unreadable rulebook, unusable transcript) is reported as
``status: EMPTY`` with the structure still present — never ``BLOCKED``, and
never a silent success.  Uncertain decisions carry a ``NEEDS_REVIEW`` tag with
a human-readable explanation.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

# Conservative fallback: only the two rules that never invent client style.
DEFAULT_RULES: dict[str, Any] = {
    "remove_fillers": True,
    "filler_words": ("um", "umm", "uh", "uhh", "hmm", "erm"),
    "max_pause_seconds": 0.8,
    "min_keep_seconds": 0.0,
    "cut_style": "unspecified",
}

_GLOBAL_HEADING = re.compile(r"^#+\s*global\s+rules\s*$", re.IGNORECASE)
_PROCESS_HEADING = re.compile(r"^#+\s*process[-\s]*specific\s+rules\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#+\s*\S")
_RULE_LINE = re.compile(r"^[-*+]\s*([A-Za-z0-9_ -]+?)\s*:\s*(.+?)\s*$")

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


class RulebookError(ValueError):
    """Raised only for a structurally unreadable rulebook file."""


_SENTENCE_END = re.compile(r"[.!?][\"')\]]*$")
_SENTENCE_GAP_SECONDS = 1.0


def segment_words_into_sentences(words: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group word timestamps into sentence-level segments (no LLM call).

    Fallback for a missing operator refined transcript: a sentence boundary is
    placed after a word ending in sentence punctuation, or before a word that
    starts more than ``_SENTENCE_GAP_SECONDS`` after the previous word ends.
    The caller marks the result NEEDS_REVIEW so the operator can correct the
    boundaries later.
    """

    segments: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []
    previous_end: float | None = None
    for word in words:
        start = _number(word.get("start"))
        end = _number(word.get("end"))
        if start is None or end is None:
            continue
        if current and previous_end is not None and (start - previous_end) > _SENTENCE_GAP_SECONDS:
            segments.append(_segment_from_words(current))
            current = []
        current.append({"word": str(word.get("word") or ""), "start": start, "end": end})
        previous_end = end
        if _SENTENCE_END.search(str(word.get("word") or "").strip()):
            segments.append(_segment_from_words(current))
            current = []
            previous_end = None
    if current:
        segments.append(_segment_from_words(current))
    return segments


def _segment_from_words(words: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "start": float(words[0]["start"]),
        "end": float(words[-1]["end"]),
        "text": " ".join(str(word.get("word") or "") for word in words).strip(),
    }


def parse_rulebook_text(text: str) -> dict[str, Any]:
    """Parse Rulebook text into a flat rule mapping (process rules win)."""

    global_rules: dict[str, Any] = {}
    process_rules: dict[str, Any] = {}
    target: dict[str, Any] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _GLOBAL_HEADING.match(stripped):
            target = global_rules
            continue
        if _PROCESS_HEADING.match(stripped):
            target = process_rules
            continue
        if _ANY_HEADING.match(stripped):
            target = None
            continue
        if target is None:
            continue
        match = _RULE_LINE.match(stripped)
        if match is None:
            continue
        key = match.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        target[key] = _coerce_value(match.group(2))

    merged = dict(DEFAULT_RULES)
    for section in (global_rules, process_rules):
        for key, value in section.items():
            merged[key] = value
    return merged


def parse_rulebook_file(path: Path | None) -> tuple[dict[str, Any], str]:
    """Return ``(rules, source)``; ``source`` records where the rules came from."""

    if path is None:
        return dict(DEFAULT_RULES), "defaults:no_rulebook_supplied"
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return dict(DEFAULT_RULES), f"defaults:unreadable_rulebook:{Path(path).name}"
    rules = parse_rulebook_text(text)
    if rules == dict(DEFAULT_RULES):
        return rules, f"rulebook:no_usable_rules:{Path(path).name}"
    return rules, f"rulebook:{Path(path).name}"


def build_cut_plan(
    transcript: Mapping[str, Any] | None,
    rulebook_path: Path | None = None,
    *,
    rulebook_rules: Mapping[str, Any] | None = None,
    operator_drops: list[Mapping[str, Any]] | None = None,
    duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Merge operator compulsory cuts with rulebook cuts into one cut plan.

    Contract (Neel's approved Stage 4 redesign):
    - The operator CUT DECISIONS file lists compulsory DROP ranges (bad takes,
      bad pauses) with timestamps.  The pipeline never invents them.
    - Rulebook cuts (filler words, pauses longer than ``max_pause_seconds``)
      are computed from the transcript words and applied ON TOP.
    - The final plan contains DROP ranges and the complementary KEEP ranges
      with timestamps.  Uncertain decisions carry NEEDS_REVIEW tags; missing
      or unusable input is ``status: EMPTY`` (structure present), never a
      silent pass.
    """

    if rulebook_rules is not None:
        rules = {**DEFAULT_RULES, **dict(rulebook_rules)}
        source = "rulebook:injected"
    else:
        rules, source = parse_rulebook_file(rulebook_path)

    plan: dict[str, Any] = {
        "status": "EMPTY",
        "stage_status": "PROVISIONAL",
        "artifact_kind": "cut_plan",
        "rulebook_source": source,
        "operator_source": "operator:none_provided",
        "rules": _serializable_rules(rules),
        "drop_ranges": [],
        "keep_ranges": [],
        "needs_review_tags": [],
        "cut_summary": {
            "input_word_count": 0,
            "operator_drop_count": 0,
            "rulebook_drop_count": 0,
            "operator_drop_seconds": 0.0,
            "rulebook_drop_seconds": 0.0,
            "removed_seconds": 0.0,
        },
        "approval_required": "VISUAL_STORYBOARD_APPROVAL",
        "applied_by_builder": False,
    }
    if not isinstance(transcript, Mapping):
        plan["reason"] = "EMPTY: transcript is unavailable"
        return plan

    words = transcript.get("words")
    if not isinstance(words, list) or not words:
        plan["reason"] = "EMPTY: transcript has no word-level timestamps"
        return plan
    if duration_seconds is None or not _positive_or_none(duration_seconds):
        plan["reason"] = "EMPTY: validated transcript duration is unavailable"
        return plan
    duration = float(duration_seconds)

    review_tags: list[dict[str, Any]] = []
    operator_ranges, operator_invalid = _validated_operator_drops(operator_drops, duration)
    for invalid in operator_invalid:
        review_tags.append(invalid)
    if operator_ranges:
        plan["operator_source"] = f"operator:cut_decisions_file ({len(operator_ranges)} drop(s))"

    filler_words = _filler_set(rules.get("filler_words"))
    remove_fillers = bool(rules.get("remove_fillers", True))
    max_pause = _positive_or_none(rules.get("max_pause_seconds"))

    rulebook_drops: list[dict[str, float]] = []
    filler_count = 0
    kept_word_count = 0
    for index, raw_word in enumerate(words):
        if not isinstance(raw_word, Mapping):
            review_tags.append(
                _needs_review("WORD_NOT_OBJECT", f"transcript word {index} is not an object; skipped")
            )
            continue
        text = raw_word.get("word")
        start = _number(raw_word.get("start"))
        end = _number(raw_word.get("end"))
        if not isinstance(text, str) or not text.strip() or start is None or end is None or end <= start:
            review_tags.append(
                _needs_review(
                    "WORD_TIMESTAMP_INCOMPLETE",
                    f"transcript word {index} has no usable text or timestamps; span kept for manual review",
                    time_seconds=start,
                )
            )
            continue
        if remove_fillers and _normalized(text) in filler_words:
            filler_count += 1
            rulebook_drops.append({"start_seconds": float(start), "end_seconds": float(end)})
            continue
        kept_word_count += 1

    if max_pause is not None:
        previous_end: float | None = None
        for raw_word in words:
            if not isinstance(raw_word, Mapping):
                continue
            text = raw_word.get("word")
            start = _number(raw_word.get("start"))
            end = _number(raw_word.get("end"))
            if not isinstance(text, str) or start is None or end is None or end <= start:
                continue
            if remove_fillers and _normalized(text) in filler_words:
                continue
            if previous_end is not None and (start - previous_end) > max_pause:
                rulebook_drops.append(
                    {"start_seconds": float(previous_end), "end_seconds": float(start)}
                )
            previous_end = end

    merged_drops = _merge_ranges([*operator_ranges, *rulebook_drops])
    keep_ranges = _complement_keeps(merged_drops, duration, words)

    if not keep_ranges:
        plan.update(
            {
                "status": "NEEDS_REVIEW",
                "reason": "every second of the source is covered by a DROP range; no KEEP content exists",
                "drop_ranges": merged_drops,
                "needs_review_tags": review_tags
                + [_needs_review("NO_KEEP_CONTENT", "the merged cut plan removes all content")],
            }
        )
        plan["cut_summary"] = _plan_summary(
            len(words), operator_ranges, rulebook_drops, merged_drops, duration
        )
        return plan

    min_keep = _positive_or_none(rules.get("min_keep_seconds")) or 0.0
    if min_keep > 0:
        short = [item for item in keep_ranges if (item["end_seconds"] - item["start_seconds"]) < min_keep]
        if short and len(short) < len(keep_ranges):
            keep_ranges = [item for item in keep_ranges if item not in short]
            review_tags.append(
                _needs_review(
                    "SHORT_KEEP_DROPPED",
                    f"{len(short)} keep range(s) shorter than min_keep_seconds={min_keep} were dropped",
                )
            )
            merged_drops = _complement_drops(keep_ranges, duration)
        elif short:
            review_tags.append(
                _needs_review(
                    "MIN_KEEP_UNSATISFIED",
                    f"no keep range reaches min_keep_seconds={min_keep}; the longest range was kept",
                )
            )
            longest = max(keep_ranges, key=lambda item: item["end_seconds"] - item["start_seconds"])
            keep_ranges = [longest]
            merged_drops = _complement_drops(keep_ranges, duration)

    removed = sum(item["end_seconds"] - item["start_seconds"] for item in merged_drops)
    plan.update(
        {
            "status": "PROVISIONAL",
            "drop_ranges": merged_drops,
            "keep_ranges": keep_ranges,
            "needs_review_tags": review_tags,
        }
    )
    plan["cut_summary"] = _plan_summary(
        len(words), operator_ranges, rulebook_drops, merged_drops, duration
    )
    plan["cut_summary"]["input_word_count"] = len(words)
    plan["cut_summary"]["kept_word_count"] = kept_word_count
    plan["cut_summary"]["removed_seconds"] = round(removed, 6)
    return plan


def _validated_operator_drops(
    operator_drops: list[Mapping[str, Any]] | None,
    duration: float,
) -> tuple[list[dict[str, float]], list[dict[str, Any]]]:
    """Validate operator compulsory DROP ranges; invalid ones become review tags."""

    valid: list[dict[str, float]] = []
    rejected: list[dict[str, Any]] = []
    if not operator_drops:
        return valid, rejected
    if not isinstance(operator_drops, list):
        rejected.append(_needs_review("OPERATOR_DROPS_INVALID", "cut decisions file must contain a list"))
        return valid, rejected
    for index, item in enumerate(operator_drops, start=1):
        if not isinstance(item, Mapping):
            rejected.append(
                _needs_review("OPERATOR_DROP_INVALID", f"operator drop {index} is not an object")
            )
            continue
        start = _number(item.get("start_seconds", item.get("start")))
        end = _number(item.get("end_seconds", item.get("end")))
        if start is None or end is None or start < 0 or end <= start:
            rejected.append(
                _needs_review(
                    "OPERATOR_DROP_INVALID",
                    f"operator drop {index} needs ordered non-negative start/end seconds",
                    time_seconds=start,
                )
            )
            continue
        if end > duration + 1e-6:
            rejected.append(
                _needs_review(
                    "OPERATOR_DROP_OUT_OF_RANGE",
                    f"operator drop {index} ends after the validated source duration",
                    time_seconds=start,
                )
            )
            continue
        valid.append({"start_seconds": float(start), "end_seconds": float(end)})
    return valid, rejected


def _merge_ranges(ranges: list[Mapping[str, float]]) -> list[dict[str, float]]:
    """Union overlapping/adjacent ranges and return them ordered."""

    ordered = sorted(
        (
            {"start_seconds": float(item["start_seconds"]), "end_seconds": float(item["end_seconds"])}
            for item in ranges
            if _number(item.get("start_seconds")) is not None
            and _number(item.get("end_seconds")) is not None
        ),
        key=lambda item: item["start_seconds"],
    )
    merged: list[dict[str, float]] = []
    for item in ordered:
        if merged and item["start_seconds"] <= merged[-1]["end_seconds"] + 1e-9:
            merged[-1]["end_seconds"] = max(merged[-1]["end_seconds"], item["end_seconds"])
        else:
            merged.append(dict(item))
    return merged


def _complement_drops(keep_ranges: list[Mapping[str, Any]], duration: float) -> list[dict[str, float]]:
    """Inverse of ``_complement_keeps``: DROP ranges covering everything else."""

    return _merge_ranges(
        _invert_ranges(
            sorted(
                (
                    {"start_seconds": float(item["start_seconds"]), "end_seconds": float(item["end_seconds"])}
                    for item in keep_ranges
                ),
                key=lambda item: item["start_seconds"],
            ),
            duration,
        )
    )


def _complement_keeps(
    drops: list[Mapping[str, float]],
    duration: float,
    words: list[Any],
) -> list[dict[str, Any]]:
    """KEEP ranges = the complement of the merged DROP ranges inside [0, duration]."""

    ordered = sorted(
        (
            {"start_seconds": float(item["start_seconds"]), "end_seconds": float(item["end_seconds"])}
            for item in drops
        ),
        key=lambda item: item["start_seconds"],
    )
    inverted = _invert_ranges(ordered, duration)
    keeps: list[dict[str, Any]] = []
    for span in inverted:
        word_count = sum(
            1
            for word in words
            if isinstance(word, Mapping)
            and _number(word.get("start")) is not None
            and float(word["start"]) >= span["start_seconds"] - 1e-9
            and _number(word.get("end")) is not None
            and float(word["end"]) <= span["end_seconds"] + 1e-9
        )
        keeps.append(
            {
                "start_seconds": round(span["start_seconds"], 6),
                "end_seconds": round(span["end_seconds"], 6),
                "reason": "kept: not covered by operator or rulebook DROP ranges",
                "rule_source": "merged:operator+rulebook",
                "word_count": word_count,
            }
        )
    return keeps


def _invert_ranges(
    ordered: list[Mapping[str, float]], duration: float
) -> list[dict[str, float]]:
    """Complement of ordered non-overlapping ranges inside [0, duration]."""

    inverted: list[dict[str, float]] = []
    cursor = 0.0
    for item in ordered:
        start = max(0.0, float(item["start_seconds"]))
        end = min(duration, float(item["end_seconds"]))
        if start > cursor + 1e-9:
            inverted.append({"start_seconds": cursor, "end_seconds": start})
        cursor = max(cursor, end)
    if cursor < duration - 1e-9:
        inverted.append({"start_seconds": cursor, "end_seconds": duration})
    return inverted


def _plan_summary(
    word_count: int,
    operator_ranges: list[Mapping[str, float]],
    rulebook_ranges: list[Mapping[str, float]],
    merged: list[Mapping[str, float]],
    duration: float,
) -> dict[str, Any]:
    return {
        "input_word_count": word_count,
        "operator_drop_count": len(operator_ranges),
        "rulebook_drop_count": len(rulebook_ranges),
        "operator_drop_seconds": round(
            sum(item["end_seconds"] - item["start_seconds"] for item in operator_ranges), 6
        ),
        "rulebook_drop_seconds": round(
            sum(item["end_seconds"] - item["start_seconds"] for item in rulebook_ranges), 6
        ),
        "merged_drop_count": len(merged),
        "source_duration_seconds": round(duration, 6),
        "removed_seconds": round(sum(item["end_seconds"] - item["start_seconds"] for item in merged), 6),
    }


def _coerce_value(raw: str) -> Any:
    value = raw.strip().strip('"').strip("'")
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    # Ticket G4: structured rule values (e.g. mograph_entries as a list of
    # objects) are written as inline JSON; only [/{-prefixed values are
    # parsed, so every legacy scalar/comma rule is unchanged. This must run
    # before the comma split — JSON values contain commas.
    if value[:1] in "[{":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    if "," in value:
        return tuple(part.strip() for part in value.split(",") if part.strip())
    number = _number(value)
    if number is not None:
        # Ticket G4: integer-valued rules (e.g. pre_roll_frames) must stay
        # int, not float, so downstream integer validators accept them.
        return int(number) if number.is_integer() else number
    return value


def _serializable_rules(rules: Mapping[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in rules.items():
        if isinstance(item, (tuple, list, set)):
            value[key] = list(item)
        elif isinstance(item, (bool, int, float, str)) or item is None:
            value[key] = item
        elif isinstance(item, Mapping):
            value[key] = dict(item)
        else:
            value[key] = str(item)
    return value


def _filler_set(value: Any) -> set[str]:
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = list(DEFAULT_RULES["filler_words"])
    return {_normalized(str(part)) for part in parts if str(part).strip()}


def _normalized(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.strip().lower())


def _needs_review(code: str, explanation: str, *, time_seconds: float | None = None) -> dict[str, Any]:
    tag: dict[str, Any] = {
        "tag": "NEEDS_REVIEW",
        "code": code,
        "explanation": explanation,
    }
    if time_seconds is not None:
        tag["time_seconds"] = round(float(time_seconds), 6)
    return tag


def render_approval_summary(
    cut_decisions: Mapping[str, Any] | None,
    *,
    run_id: str | None = None,
) -> str:
    """Render a human-readable operator summary of every uncertain cut decision.

    The result is plain Markdown so it can be reviewed (or archived) before the
    ``VISUAL_STORYBOARD_APPROVAL`` gate.  It never guesses: every listed item is
    a ``NEEDS_REVIEW`` tag produced by ``build_cut_decisions``.  A plan with no
    uncertain decisions still renders, stating that explicitly, so the
    operator can see the check happened rather than an absent section.
    """

    decisions = dict(cut_decisions or {})
    tags = [item for item in decisions.get("needs_review_tags") or [] if isinstance(item, Mapping)]
    keep_ranges = [item for item in decisions.get("keep_ranges") or [] if isinstance(item, Mapping)]
    drop_ranges = [item for item in decisions.get("drop_ranges") or [] if isinstance(item, Mapping)]
    summary = decisions.get("cut_summary") if isinstance(decisions.get("cut_summary"), Mapping) else {}
    status = str(decisions.get("status") or "EMPTY").upper()

    lines: list[str] = ["# Cut Plan Approval Summary", ""]
    if run_id:
        lines += [f"- **Run ID:** {run_id}"]
    lines += [
        f"- **Decision status:** {status}",
        f"- **Rulebook source:** {decisions.get('rulebook_source') or 'defaults:no_rulebook_supplied'}",
        f"- **Operator cut source:** {decisions.get('operator_source') or 'operator:none_provided'}",
        f"- **DROP ranges planned:** {len(drop_ranges)}",
        f"- **KEEP ranges planned:** {len(keep_ranges)}",
        f"- **Uncertain decisions (NEEDS_REVIEW):** {len(tags)}",
        f"- **Approval gate:** {decisions.get('approval_required') or 'VISUAL_STORYBOARD_APPROVAL'}",
        "",
    ]
    if summary:
        lines += [
            "## Cut summary",
            "",
            f"- Input words: {summary.get('input_word_count', 0)}",
            f"- Operator DROP ranges: {summary.get('operator_drop_count', 0)} "
            f"({summary.get('operator_drop_seconds', 0.0)}s)",
            f"- Rulebook DROP ranges: {summary.get('rulebook_drop_count', 0)} "
            f"({summary.get('rulebook_drop_seconds', 0.0)}s)",
            f"- Merged DROP ranges: {summary.get('merged_drop_count', 0)}",
            f"- Seconds removed: {summary.get('removed_seconds', 0.0)}",
            "",
        ]
    if drop_ranges:
        lines += ["## Planned DROP ranges (operator + rulebook merged)", ""]
        for index, item in enumerate(drop_ranges, start=1):
            lines.append(
                f"{index}. DROP `{item.get('start_seconds')}s – {item.get('end_seconds')}s`"
            )
        lines.append("")
    if keep_ranges:
        lines += ["## Planned KEEP ranges", ""]
        for index, item in enumerate(keep_ranges, start=1):
            lines.append(
                f"{index}. `{item.get('start_seconds')}s – {item.get('end_seconds')}s` "
                f"({item.get('word_count', '?')} words) — {item.get('reason', 'no reason recorded')}"
            )
        lines.append("")
    reason = decisions.get("reason")
    if isinstance(reason, str) and reason.strip():
        lines += [f"**Decision note:** {reason}", ""]
    if tags:
        lines += ["## NEEDS_REVIEW — decisions the operator must confirm", ""]
        for index, tag in enumerate(tags, start=1):
            when = (
                f" at `{tag.get('time_seconds')}s`"
                if tag.get("time_seconds") is not None
                else ""
            )
            lines += [
                f"{index}. **{tag.get('code', 'UNTAGGED')}**{when} — {tag.get('explanation', 'no explanation recorded')}",
            ]
        lines += [
            "",
            "Review each item above before approving. Approving the gate confirms",
            "these cut decisions as shown; rejecting sends the run back for review.",
        ]
    else:
        lines += [
            "## NEEDS_REVIEW",
            "",
            "No uncertain cut decisions were recorded. All cuts follow the rulebook",
            "mechanically; approving the gate applies the planned KEEP ranges as listed.",
        ]
    lines.append("")
    return "\n".join(lines)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_or_none(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


_SENTENCE_ENDINGS = (".", "!", "?", "…")


def validate_scope_ending(cut_plan: Mapping[str, Any], words: Any) -> str | None:
    """Loop 19 (assumed_rulebook_v2 2.9): an edit scope must end on a
    complete sentence.

    Returns a named issue string when the last word inside the final keep
    range lacks terminal punctuation; ``None`` when the scope ends cleanly
    (or when no kept word can be found — nothing to judge).
    """

    keep = cut_plan.get("keep_ranges") or []
    if not keep:
        return None
    scope_end = float(keep[-1].get("end_seconds") or 0.0)
    last_word: str | None = None
    for word in words or []:
        if not isinstance(word, Mapping):
            continue
        if float(word.get("start") or 0.0) < scope_end - 1e-6 and str(word.get("word") or "").strip():
            last_word = str(word["word"]).strip()
    if last_word is None:
        return None
    if last_word.endswith(_SENTENCE_ENDINGS):
        return None
    return (
        f"edit scope ends mid-sentence: the last kept word is {last_word!r} "
        "(edit_scope scopes must end on a complete sentence)"
    )


def build_rhythm_report(cut_plan: Mapping[str, Any], rules: Mapping[str, Any]) -> dict[str, Any]:
    """Loop 19 (assumed_rulebook_v2 2.1): advisory rhythm report.

    Reports the longest keep hold against ``max_visual_hold_seconds``.
    Advisory only — jump-cut generation is never automatic; the operator
    shapes rhythm through cut decisions. Consumed as plan metadata, not a
    gate (the mandatory-card and caption gates live in the Loop 20 referee).
    """

    keep = cut_plan.get("keep_ranges") or []
    holds = [
        float(entry.get("end_seconds") or 0.0) - float(entry.get("start_seconds") or 0.0)
        for entry in keep
        if isinstance(entry, Mapping)
    ]
    max_hold = max(holds, default=0.0)
    limit = _number(rules.get("max_visual_hold_seconds")) if isinstance(rules, Mapping) else None
    violations = [
        {
            "hold_seconds": round(hold, 6),
            "start_seconds": round(float(entry.get("start_seconds") or 0.0), 6),
            "end_seconds": round(float(entry.get("end_seconds") or 0.0), 6),
        }
        for entry, hold in zip(keep, holds, strict=False)
        if isinstance(entry, Mapping) and limit is not None and hold > float(limit)
    ]
    return {
        "max_hold_seconds": round(max_hold, 6),
        "holds_ok": (max_hold <= float(limit)) if limit is not None else True,
        "limit_seconds": float(limit) if limit is not None else None,
        "violations": violations,
        "note": "advisory: jump-cut generation is not automatic; the operator shapes rhythm via cut decisions",
    }


__all__ = [
    "DEFAULT_RULES",
    "RulebookError",
    "build_cut_plan",
    "build_rhythm_report",
    "parse_rulebook_file",
    "parse_rulebook_text",
    "render_approval_summary",
    "segment_words_into_sentences",
    "validate_scope_ending",
]
