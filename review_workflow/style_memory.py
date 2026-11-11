from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def record_edit_pair(
    store: Path,
    *,
    original: str,
    revised: str,
    language: str,
    edit_type: str,
    include_in_style_profile: bool,
) -> dict:
    if not original.strip() or not revised.strip():
        raise ValueError("original and revised text are required")
    if language not in {"zh", "en"}:
        raise ValueError("language must be zh or en")
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "language": language,
        "edit_type": edit_type,
        "original": original,
        "revised": revised,
        "style_consent": bool(include_in_style_profile),
    }
    store.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if store.exists():
        existing = json.loads(store.read_text(encoding="utf-8"))
    existing.append(entry)
    store.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry


def style_training_pairs(store: Path, *, language: str) -> list[dict]:
    if not store.exists():
        return []
    entries = json.loads(store.read_text(encoding="utf-8"))
    return [
        item for item in entries
        if item.get("language") == language and item.get("style_consent") is True
    ]

