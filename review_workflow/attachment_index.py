"""Local item-to-PDF-key hints for fast review queue refreshes."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .queue import atomic_json
from .zotero_plugin import ZoteroPluginError, _load_plugin_module


def index_path(kb: Path) -> Path:
    return kb / "screening" / "zotero-attachment-index.json"


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value if isinstance(value, dict) else {}


def _source_key(record: dict[str, Any]) -> str | None:
    details = record.get("attachment_details") or {}
    for item in details.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        if item.get("is_pdf") is True or item.get("content_type") == "application/pdf":
            key = item.get("key")
            if isinstance(key, str) and key.strip():
                return key.strip()
    if "attachment_details" in record:
        return None
    keys = record.get("attachment_keys") or []
    if isinstance(keys, list) and keys and isinstance(keys[0], str):
        return keys[0].strip() or None
    return None


def refresh_index(kb: Path, source: Path) -> tuple[dict[str, Any], dict[str, int]]:
    """Merge archived source hints without making any Zotero request."""
    payload = _read(source)
    records = payload.get("records")
    if not isinstance(records, list):
        raise ValueError(f"候选来源缺少records数组：{source}")
    path = index_path(kb)
    index = _read(path)
    entries = index.get("entries") if isinstance(index.get("entries"), dict) else {}
    entries = dict(entries)
    cache_hits = 0
    missing = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        item_key = record.get("zotero_key")
        if not isinstance(item_key, str) or not item_key:
            continue
        archived_key = _source_key(record)
        cached = entries.get(item_key)
        cached_key = cached.get("pdf_attachment_key") if isinstance(cached, dict) else None
        if isinstance(cached, dict) and cached.get("source") == "archived_candidate" and not archived_key:
            cached_key = None
        if cached_key:
            cache_hits += 1
            key = cached_key
        else:
            key = archived_key
        if not key:
            missing += 1
        entries[item_key] = {
            "pdf_attachment_key": key,
            "status": "available" if key else "missing_pdf_key",
            "warning": None if key else "missing_pdf_key",
            "source": (cached.get("source") if cached_key else "archived_candidate") if key else "missing",
            "checked_at": cached.get("checked_at") if isinstance(cached, dict) else None,
        }
    atomic_json(path, {"schema_version": "review_workflow.attachment_index.v1", "entries": entries})
    return entries, {
        "total_papers": len(records),
        "cache_hit": cache_hits,
        "missing_pdf_key": missing,
        "zotero_queries": 0,
    }


def backfill_missing(kb: Path, *, limit: int = 20, retry_checked: bool = False) -> dict[str, Any]:
    """Explicit, bounded Zotero lookup after the queue has been refreshed."""
    if limit < 1:
        raise ValueError("limit必须是正整数")
    path = index_path(kb)
    index = _read(path)
    entries = index.get("entries") if isinstance(index.get("entries"), dict) else {}
    entries = dict(entries)
    missing_keys = sorted(key for key, row in entries.items() if isinstance(row, dict) and not row.get("pdf_attachment_key") and (retry_checked or not row.get("checked_at")))
    started = time.perf_counter()
    queries = 0
    filled = 0
    warnings: list[str] = []
    if missing_keys:
        plugin = _load_plugin_module()
        for item_key in missing_keys[:limit]:
            queries += 1
            try:
                children = plugin.api_get(f"{plugin.LOCAL_USER}/items/{item_key}/children")
                pdf = next((row for row in children if (row.get("data") or row).get("contentType") == "application/pdf"), None)
                attachment_key = (pdf.get("key") or pdf.get("data", {}).get("key")) if pdf else None
                if attachment_key:
                    entries[item_key] = {"pdf_attachment_key": attachment_key, "status": "available", "source": "zotero_children"}
                    filled += 1
                else:
                    entries[item_key] = {**entries[item_key], "checked_at": datetime.now(timezone.utc).isoformat()}
                    warnings.append(f"{item_key}: missing_pdf_key")
            except (OSError, ValueError, ZoteroPluginError, SystemExit) as exc:
                warnings.append(f"{item_key}: {exc}")
                if isinstance(exc, SystemExit):
                    break
    atomic_json(path, {"schema_version": "review_workflow.attachment_index.v1", "entries": entries})
    return {"total_papers": len(entries), "cache_hit": 0, "missing_pdf_key": sum(not row.get("pdf_attachment_key") for row in entries.values()), "zotero_queries": queries, "filled": filled, "elapsed_seconds": round(time.perf_counter() - started, 3), "warnings": warnings}


def apply_index(pool: dict[str, Any], entries: dict[str, Any]) -> dict[str, Any]:
    for row in pool["results"]:
        entry = entries.get(row.get("zotero_key")) or {}
        key = entry.get("pdf_attachment_key")
        if key and key not in row.get("attachment_keys", []):
            row["attachment_keys"] = [key, *row.get("attachment_keys", [])]
        row["pdf_attachment_key"] = key
        row["pdf_key_status"] = "available" if key else "missing_pdf_key"
    return pool
