"""Caption (Text+) plan for the Resolve Work Order (Ticket E2).

The pipeline emits ``outputs/text/captions_plan.json`` when rulebook captions
are enabled. Entries reuse the B1 SRT discipline: text and timing come from
the post-cut caption cues; Phase E only decides LOOK (template per entry
type). The library convention: each caption template contains exactly one
Text+ tool carrying the caption text (worker flags >1).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

CAPTIONS_PLAN_SCHEMA_VERSION = 1
CAPTIONS_PLAN_FILE_NAME = "captions_plan.json"
CAPTION_TYPES = ("standard_caption", "hook_animated")


class CaptionsPlanError(ValueError):
    """Raised when a captions plan is invalid."""


def build_captions_plan(run_id: str, entries: list[Mapping[str, Any]], *, created_at: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": CAPTIONS_PLAN_SCHEMA_VERSION,
        "artifact_kind": "captions_plan",
        "run_id": run_id,
        "created_at": created_at or _now(),
        "entries": [_json_safe(dict(entry)) for entry in entries],
    }


def validate_captions_plan(
    plan: Mapping[str, Any],
    *,
    check_fields: bool = True,
) -> list[str]:
    """Return every violation; an empty list means the plan is valid."""

    violations: list[str] = []
    if not isinstance(plan, Mapping):
        return ["captions plan must be a JSON object"]
    if plan.get("schema_version") != CAPTIONS_PLAN_SCHEMA_VERSION:
        violations.append(f"schema_version {plan.get('schema_version')!r} is unsupported (expected {CAPTIONS_PLAN_SCHEMA_VERSION})")
    if str(plan.get("artifact_kind") or "") != "captions_plan":
        violations.append("artifact_kind must be 'captions_plan'")
    if not str(plan.get("run_id") or "").strip():
        violations.append("run_id is missing")
    entries = plan.get("entries")
    if not isinstance(entries, list):
        violations.append("entries must be a list")
        return violations

    seen_ids: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        prefix = f"entry[{index}]"
        if not isinstance(entry, Mapping):
            violations.append(f"{prefix} must be an object")
            continue
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            violations.append(f"{prefix} id must be a non-empty string")
        elif entry_id in seen_ids:
            violations.append(f"{prefix} duplicate entry id {entry_id!r}")
        else:
            seen_ids.add(entry_id)

        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            violations.append(f"{prefix} text must be a non-empty string")

        start = entry.get("start_seconds")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or float(start) < 0:
            violations.append(f"{prefix} start_seconds must be a number >= 0")

        duration = entry.get("duration_seconds")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or float(duration) <= 0:
            violations.append(f"{prefix} duration_seconds must be a number > 0")

        template = entry.get("template")
        if not isinstance(template, str) or not template.strip():
            violations.append(f"{prefix} template must be a non-empty media-pool template name")

        entry_type = entry.get("type")
        if entry_type not in CAPTION_TYPES:
            violations.append(f"{prefix} type {entry_type!r} must be one of {CAPTION_TYPES}")

        if not check_fields:
            continue

        center = entry.get("center")
        if center is not None:
            if not isinstance(center, Mapping) or "x" not in center or "y" not in center:
                violations.append(f"{prefix} center must be null or {{x, y}}")
            else:
                for axis in ("x", "y"):
                    value = center.get(axis)
                    if not isinstance(value, (int, float)) or isinstance(value, bool):
                        violations.append(f"{prefix} center.{axis} must be a number")

        size = entry.get("size")
        if size is not None and (not isinstance(size, (int, float)) or isinstance(size, bool) or float(size) <= 0):
            violations.append(f"{prefix} size must be null or a positive number")

        color = entry.get("color_rgb")
        if color is not None:
            if not isinstance(color, (list, tuple)) or len(color) != 3 or not all(
                isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= float(c) <= 1 for c in color
            ):
                violations.append(f"{prefix} color_rgb must be null or [r,g,b] floats in 0..1")

        style = entry.get("style")
        if style is not None and not isinstance(style, str):
            violations.append(f"{prefix} style must be null or a string")

    return violations


def require_valid_captions_plan(plan: Mapping[str, Any], *, check_fields: bool = True) -> None:
    violations = validate_captions_plan(plan, check_fields=check_fields)
    if violations:
        raise CaptionsPlanError("invalid captions plan: " + "; ".join(violations))


def write_captions_plan(destination: Path | str, plan: Mapping[str, Any], *, check_fields: bool = True) -> Path:
    require_valid_captions_plan(plan, check_fields=check_fields)
    path = Path(destination)
    _atomic_json_write(path, plan)
    return path


def _atomic_json_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(_json_safe(dict(value)), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "CAPTIONS_PLAN_FILE_NAME",
    "CAPTIONS_PLAN_SCHEMA_VERSION",
    "CAPTION_TYPES",
    "CaptionsPlanError",
    "build_captions_plan",
    "require_valid_captions_plan",
    "validate_captions_plan",
    "write_captions_plan",
]
