"""SRT caption generation from an approved cut plan + word-level transcript.

Ticket B1: after VISUAL_STORYBOARD_APPROVAL passes, the approved KEEP ranges
are applied and captions are generated for the POST-CUT timeline.  Only words
inside KEEP ranges appear; their timestamps are remapped to the edited
timeline (removed segments shift earlier content left).  Cues are merged into
readable sentence/punctuation-based blocks with a max duration and max 2
lines.  Serialization is hand-rolled with the stdlib — SRT is a tiny format
(MIT-licensed `srt` package unnecessary), and this avoids a new dependency.

The builder calls :func:`build_subtitle_plan`; the SRT file itself is written
by the CORE_TIMELINE stage, not here.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

MAX_CUE_DURATION = 5.0
MAX_LINE_CHARS = 42
MAX_CUE_LINES = 2

_SENTENCE_ENDS = (".", "!", "?", "…")
_TIMESTAMP_PATTERN = re.compile(
    r"^(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})$"
)


def remap_words_to_cut_timeline(
    words: list[Mapping[str, Any]],
    keep_ranges: list[Mapping[str, float]],
) -> tuple[list[dict[str, float]], dict[str, int]]:
    """Map transcript words inside KEEP ranges onto the post-cut timeline.

    A word is kept only when it lies fully inside one KEEP range (words
    straddling a cut boundary are dropped — they would cross the edit).  The
    new timestamp is the original time minus the total duration removed before
    that keep range.
    """

    mapped: list[dict[str, float]] = []
    dropped_count = 0
    keep_ranges = sorted(
        (dict(item) for item in keep_ranges if isinstance(item, Mapping)),
        key=lambda item: item.get("start_seconds", 0.0),
    )
    # prefix[i] = total kept duration before keep range i; removed before range
    # i is therefore (range[i].start - prefix[i]).
    prefix = [0.0]
    for item in keep_ranges:
        prefix.append(prefix[-1] + (float(item["end_seconds"]) - float(item["start_seconds"])))

    index = 0
    for raw_word in words:
        if not isinstance(raw_word, Mapping):
            dropped_count += 1
            continue
        start = _number(raw_word.get("start"))
        end = _number(raw_word.get("end"))
        if start is None or end is None or end <= start:
            dropped_count += 1
            continue
        while (
            index < len(keep_ranges)
            and end > float(keep_ranges[index]["end_seconds"]) + 1e-9
        ):
            index += 1
        if index >= len(keep_ranges):
            dropped_count += 1
            continue
        item = keep_ranges[index]
        if start < float(item["start_seconds"]) - 1e-9 or end > float(item["end_seconds"]) + 1e-9:
            dropped_count += 1
            continue
        removed_before = float(item["start_seconds"]) - prefix[index]
        mapped.append(
            {
                "text": str(raw_word.get("word", "")).strip(),
                "start": round(start - removed_before, 6),
                "end": round(end - removed_before, 6),
            }
        )
    return mapped, {"kept_word_count": len(mapped), "dropped_word_count": dropped_count}


def build_caption_cues(
    mapped_words: list[Mapping[str, Any]],
    *,
    max_cue_duration: float = MAX_CUE_DURATION,
    max_line_chars: int = MAX_LINE_CHARS,
    max_cue_lines: int = MAX_CUE_LINES,
) -> list[dict[str, Any]]:
    """Group remapped words into readable SRT cues.

    Words are first grouped at sentence-ending punctuation; very short groups
    (single words) are merged into the previous group when limits allow.
    Groups are then chunked so every cue stays within the max duration and the
    line budget.  Cues are contiguous and therefore never overlap; timestamps
    are monotonic.
    """

    cues: list[dict[str, Any]] = []
    if not mapped_words:
        return cues

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for raw_word in mapped_words:
        word = dict(raw_word)
        word["text"] = str(word.get("text") or word.get("word") or "").strip()
        current.append(word)
        if _is_sentence_end(word["text"]):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    # Merge single-word groups into the previous group when the combination
    # still fits the cue limits (avoids one-word cues wherever possible).
    merged: list[list[dict[str, Any]]] = []
    for group in groups:
        if merged and len(group) == 1:
            candidate = merged[-1] + group
            if (
                _group_duration(candidate) <= max_cue_duration + 1e-9
                and _group_text_length(candidate) <= max_line_chars * max_cue_lines
            ):
                merged[-1] = candidate
                continue
        merged.append(list(group))

    previous_end = 0.0
    overlong_word_cue_count = 0
    for group in merged:
        for chunk in _chunk_group(group, max_cue_duration, max_line_chars, max_cue_lines):
            start = max(float(chunk[0]["start"]), previous_end)
            end = float(chunk[-1]["end"])
            if end <= start:
                end = start + 0.001
            if end - start > max_cue_duration + 1e-9 and len(chunk) == 1:
                overlong_word_cue_count += 1
            text = " ".join(str(word["text"]) for word in chunk if str(word["text"]))
            cues.append(
                {
                    "start": round(start, 6),
                    "end": round(end, 6),
                    "lines": _cue_lines(text, max_line_chars, max_cue_lines),
                }
            )
            previous_end = float(cues[-1]["end"])
    return cues


def serialize_srt(cues: list[Mapping[str, Any]]) -> str:
    """Serialize cues into SRT text (sequential numbering, UTF-8-safe)."""

    blocks: list[str] = []
    for number, cue in enumerate(cues, start=1):
        lines = list(cue.get("lines") or [])
        if not lines:
            continue
        blocks.append(
            "\n".join(
                [
                    str(number),
                    f"{format_srt_timestamp(cue['start'])} --> {format_srt_timestamp(cue['end'])}",
                    "\n".join(lines),
                ]
            )
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def format_srt_timestamp(seconds: float) -> str:
    """Format a seconds value as HH:MM:SS,mmm with safe rounding."""

    total = max(0.0, float(seconds))
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    whole_seconds = int(total % 60)
    millis = int(round((total % 60 - whole_seconds) * 1000))
    if millis >= 1000:
        millis -= 1000
        whole_seconds += 1
    if whole_seconds >= 60:
        minutes += 1
        whole_seconds -= 60
    if minutes >= 60:
        hours += 1
        minutes -= 60
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def parse_srt_timestamp(value: str) -> float:
    """Parse HH:MM:SS,mmm back to seconds."""

    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"invalid SRT timestamp: {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def validate_srt(
    text: str,
    *,
    max_cue_duration: float = MAX_CUE_DURATION,
    max_cue_lines: int = MAX_CUE_LINES,
    enforce_duration: bool = True,
) -> dict[str, Any]:
    """Validate SRT structure: numbering, timestamps, monotonicity, limits.

    Raises ValueError on the first violation; otherwise returns a summary.
    ``enforce_duration=False`` skips the max-duration rule (the builder uses
    this at commit time so a rare overlong single word cannot fail the whole
    timeline build; it is still surfaced in the summary).
    """

    blocks = [block for block in text.split("\n\n") if block.strip()]
    if not blocks:
        raise ValueError("SRT is empty")
    previous_end: float | None = None
    for expected, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        if len(lines) < 3:
            raise ValueError(f"cue {expected} is missing its number, timing, or text")
        if lines[0].strip() != str(expected):
            raise ValueError(f"cue {expected} numbering is not sequential ({lines[0]!r})")
        timing = _TIMESTAMP_PATTERN.match(lines[1].strip())
        if not timing:
            raise ValueError(f"cue {expected} has malformed timing line: {lines[1]!r}")
        start = parse_srt_timestamp(timing.group(1))
        end = parse_srt_timestamp(timing.group(2))
        if end <= start:
            raise ValueError(f"cue {expected} ends at or before it starts")
        if enforce_duration and (end - start) > max_cue_duration + 1e-6:
            raise ValueError(f"cue {expected} exceeds the max cue duration ({max_cue_duration}s)")
        if previous_end is not None and start < previous_end - 1e-6:
            raise ValueError(f"cue {expected} overlaps the previous cue")
        text_lines = [line for line in lines[2:] if line.strip()]
        if not text_lines:
            raise ValueError(f"cue {expected} has no caption text")
        if len(text_lines) > max_cue_lines:
            raise ValueError(f"cue {expected} has more than {max_cue_lines} lines")
        previous_end = end
    return {"cue_count": len(blocks), "valid": True}


def build_subtitle_plan(
    transcript_data: Mapping[str, Any] | None,
    keep_ranges: list[Mapping[str, float]] | None,
) -> dict[str, Any]:
    """Build the caption plan for the approved cut.

    Returns ``status: GENERATED`` with cues, or ``status: SKIPPED`` with a
    machine-readable reason (empty transcript / no keep ranges / no kept
    words).  Skipping is a normal outcome — never a crash.
    """

    plan: dict[str, Any] = {
        "status": "SKIPPED",
        "reason": None,
        "cues": [],
        "summary": {
            "input_word_count": 0,
            "kept_word_count": 0,
            "dropped_word_count": 0,
            "cue_count": 0,
            "post_cut_duration_seconds": 0.0,
            "remapped_seconds": 0.0,
            "overlong_word_cue_count": 0,
        },
    }
    if not isinstance(transcript_data, Mapping):
        plan["reason"] = "EMPTY_TRANSCRIPT: transcript data is unavailable"
        return plan
    words = transcript_data.get("words")
    if not isinstance(words, list) or not words:
        plan["reason"] = "EMPTY_TRANSCRIPT: transcript has no word-level timestamps"
        return plan
    if not keep_ranges:
        plan["reason"] = "NO_KEEP_RANGES: approved cut plan has no KEEP content"
        return plan

    mapped, stats = remap_words_to_cut_timeline(words, keep_ranges)
    cues = build_caption_cues(mapped)
    if not cues:
        plan["reason"] = "NO_WORDS_INSIDE_KEEP_RANGES: no transcript word lies fully in a KEEP range"
        plan["summary"].update(stats)
        return plan

    plan["status"] = "GENERATED"
    plan["cues"] = cues
    plan["summary"].update(
        {
            **stats,
            "input_word_count": len(words),
            "cue_count": len(cues),
            "post_cut_duration_seconds": round(cues[-1]["end"], 6),
            "remapped_seconds": round(cues[0]["start"], 6),
            "overlong_word_cue_count": sum(
                1 for cue in cues if cue["end"] - cue["start"] > MAX_CUE_DURATION + 1e-9
            ),
        }
    )
    return plan


def write_srt(destination: Any, cues: list[Mapping[str, Any]]) -> Any:
    """Write cues as UTF-8 SRT with LF line endings (no BOM)."""

    from pathlib import Path

    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_srt(cues), encoding="utf-8", newline="\n")
    return path


def _is_sentence_end(text: str) -> bool:
    return text.rstrip().endswith(_SENTENCE_ENDS)


def _group_duration(group: list[Mapping[str, Any]]) -> float:
    return float(group[-1]["end"]) - float(group[0]["start"])


def _group_text_length(group: list[Mapping[str, Any]]) -> int:
    return len(" ".join(str(word.get("text", "")).strip() for word in group))


def _chunk_group(
    group: list[Mapping[str, Any]],
    max_cue_duration: float,
    max_line_chars: int,
    max_cue_lines: int,
) -> list[list[dict[str, Any]]]:
    """Split a sentence group into cues that fit the duration/line limits."""

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in group:
        if current:
            extended = current + [word]
            if (
                _group_duration(extended) > max_cue_duration + 1e-9
                or _group_text_length(extended) > max_line_chars * max_cue_lines
            ):
                chunks.append(current)
                current = [dict(word)]
                continue
        current.append(dict(word))
    if current:
        chunks.append(current)
    return chunks


def _cue_lines(text: str, max_line_chars: int, max_cue_lines: int) -> list[str]:
    """Wrap cue text into at most max_cue_lines lines of max_line_chars."""

    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if current and len(candidate) > max_line_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    if len(lines) > max_cue_lines:
        lines = lines[: max_cue_lines - 1] + [" ".join(lines[max_cue_lines - 1 :])]
    return lines or [text]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


__all__ = [
    "MAX_CUE_DURATION",
    "MAX_CUE_LINES",
    "MAX_LINE_CHARS",
    "build_caption_cues",
    "build_subtitle_plan",
    "format_srt_timestamp",
    "parse_srt_timestamp",
    "remap_words_to_cut_timeline",
    "serialize_srt",
    "validate_srt",
    "write_srt",
]
