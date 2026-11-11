"""Sound design plan for the Resolve Work Order (Ticket D2).

The pipeline emits a versionable plan file (``outputs/audio/sound_plan.json``)
when the rulebook enables sound design. The plan carries concrete placements
(file, post-cut start, audio track, gain/pan, fades); the worker executes it.
Start times are already remapped to the POST-CUT timeline by the generator
(same discipline as the SRT ticket) — placements match the edited video's
entry boundaries, not raw footage.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SOUND_PLAN_SCHEMA_VERSION = 1
SOUND_PLAN_FILE_NAME = "sound_plan.json"
TRACK_NEW = "new_track"
VOLUME_MIN, VOLUME_MAX = -100.0, 30.0
PAN_MIN, PAN_MAX = -100, 100


class SoundPlanError(ValueError):
    """Raised when a sound plan is invalid."""


def build_sound_plan(run_id: str, entries: list[Mapping[str, Any]], *, created_at: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SOUND_PLAN_SCHEMA_VERSION,
        "artifact_kind": "sound_plan",
        "run_id": run_id,
        "created_at": created_at or _now(),
        "entries": [_json_safe(dict(entry)) for entry in entries],
    }


def validate_sound_plan(
    plan: Mapping[str, Any],
    *,
    project_root: Path | str | None = None,
    check_files_exist: bool = True,
) -> list[str]:
    """Return every violation; an empty list means the plan is valid."""

    violations: list[str] = []
    if not isinstance(plan, Mapping):
        return ["sound plan must be a JSON object"]
    if plan.get("schema_version") != SOUND_PLAN_SCHEMA_VERSION:
        violations.append(f"schema_version {plan.get('schema_version')!r} is unsupported (expected {SOUND_PLAN_SCHEMA_VERSION})")
    if str(plan.get("artifact_kind") or "") != "sound_plan":
        violations.append("artifact_kind must be 'sound_plan'")
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

        raw_file = entry.get("file")
        if not isinstance(raw_file, str) or not raw_file.strip():
            violations.append(f"{prefix} file must be a non-empty path string")
        elif check_files_exist:
            path = Path(raw_file)
            if not path.is_absolute() and project_root is not None:
                path = Path(project_root) / path
            if not path.is_file():
                violations.append(f"{prefix} file references a missing file: {path}")

        start = entry.get("start_seconds")
        if not isinstance(start, (int, float)) or isinstance(start, bool) or float(start) < 0:
            violations.append(f"{prefix} start_seconds must be a number >= 0")

        track = entry.get("track")
        if track != TRACK_NEW:
            if not isinstance(track, int) or isinstance(track, bool) or track < 1:
                violations.append(f"{prefix} track must be 'new_track' or a 1-based track index")

        volume = entry.get("volume_db")
        if volume is not None and (not isinstance(volume, (int, float)) or isinstance(volume, bool) or not VOLUME_MIN <= float(volume) <= VOLUME_MAX):
            violations.append(f"{prefix} volume_db must be null or a number in [{VOLUME_MIN}, {VOLUME_MAX}]")

        pan = entry.get("pan")
        if pan is not None and (not isinstance(pan, int) or isinstance(pan, bool) or not PAN_MIN <= pan <= PAN_MAX):
            violations.append(f"{prefix} pan must be null or an integer in [{PAN_MIN}, {PAN_MAX}]")

        for key in ("fade_in_frames", "fade_out_frames"):
            value = entry.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                violations.append(f"{prefix} {key} must be null or an integer >= 0")

    return violations


def require_valid_sound_plan(plan: Mapping[str, Any], *, project_root: Path | str | None = None, check_files_exist: bool = True) -> None:
    violations = validate_sound_plan(plan, project_root=project_root, check_files_exist=check_files_exist)
    if violations:
        raise SoundPlanError("invalid sound plan: " + "; ".join(violations))


def write_sound_plan(destination: Path | str, plan: Mapping[str, Any], *, project_root: Path | str | None = None, check_files_exist: bool = True) -> Path:
    require_valid_sound_plan(plan, project_root=project_root, check_files_exist=check_files_exist)
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
    "SOUND_PLAN_FILE_NAME",
    "SOUND_PLAN_SCHEMA_VERSION",
    "TRACK_NEW",
    "SoundPlanError",
    "build_sound_plan",
    "require_valid_sound_plan",
    "validate_sound_plan",
    "write_sound_plan",
]
