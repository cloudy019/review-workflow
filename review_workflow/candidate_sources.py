"""Verify candidate PDFs exclusively through the bundled Zotero plugin."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .core import sha256_file
from .zotero_plugin import ZoteroPluginError, attachment_file_path


class CandidateSourceError(RuntimeError):
    pass


def verify_candidate_source(
    result: Mapping[str, Any], *, helper: Path | None = None
) -> dict[str, Any]:
    """Verify exactly one candidate attachment and bind its local SHA-256."""

    if not isinstance(result, Mapping):
        raise CandidateSourceError("一次只能核验一个候选结果")
    if "results" in result or "records" in result:
        raise CandidateSourceError("检测到候选集合；必须逐篇核验来源")
    normalized = deepcopy(dict(result))
    keys = normalized.get("attachment_keys") or []
    if isinstance(keys, str) or not isinstance(keys, list):
        raise CandidateSourceError("attachment_keys必须是单篇候选的数组")
    if any(not isinstance(key, str) or not key.strip() for key in keys):
        raise CandidateSourceError("attachment_keys只能包含非空字符串")

    failures: list[str] = []
    for key in keys:
        try:
            path = attachment_file_path(key.strip(), helper=helper)
        except (OSError, ValueError, ZoteroPluginError) as exc:
            failures.append(f"{key}: {exc}")
            continue
        if path.suffix.lower() != ".pdf":
            failures.append(f"{key}: 不是PDF")
            continue
        normalized.update(
            {
                "source_verified": True,
                "verified_pdf_path": str(path),
                "verified_pdf_sha256": sha256_file(path),
                "verified_attachment_key": key.strip(),
                "source_verification_error": None,
            }
        )
        return normalized

    normalized.update(
        {
            "source_verified": False,
            "verified_pdf_path": None,
            "verified_pdf_sha256": None,
            "verified_attachment_key": None,
            "source_verification_error": "; ".join(failures)
            or "Zotero记录中没有可核验的PDF附件Key",
        }
    )
    return normalized


def verified_source_is_current(result: Mapping[str, Any]) -> bool:
    if result.get("source_verified") is not True:
        return False
    path = result.get("verified_pdf_path")
    digest = result.get("verified_pdf_sha256")
    if not isinstance(path, str) or not isinstance(digest, str):
        return False
    source = Path(path)
    return source.is_file() and sha256_file(source) == digest


__all__ = [
    "CandidateSourceError",
    "verified_source_is_current",
    "verify_candidate_source",
]
