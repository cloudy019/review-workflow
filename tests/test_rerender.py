"""Regression tests for the view-only AI-binding rebind in rerender.py.

A template/crop fix legitimately changes ``paper-review.html`` but must not
silently rebind any other AI-verified input.  ``refresh_artifacts`` compares
those hashes, so a careless re-render drops ``ai_verified`` to
``needs_clarification`` and voids the human permission.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "artifacts" / "review_ui"))

from rerender import ai_binding_check, sha256  # noqa: E402


def _job(bound: dict[str, str], valid: bool = True) -> dict:
    return {"ai_citation_permission": {"valid": valid, "bound_artifacts": bound}}


def test_view_only_rebind_is_allowed_when_all_other_inputs_match(tmp_path):
    note = tmp_path / "deep-paper-note.md"
    note.write_text("逐字未变的阅读笔记", encoding="utf-8")
    page = tmp_path / "paper-review.html"
    page.write_text("旧页面", encoding="utf-8")
    job = _job({
        "deep-paper-note.md": sha256(note),
        "paper-review.html": "0" * 64,  # 允许变化的那一项
    })

    result = ai_binding_check(job, tmp_path)

    assert result["needed"] is True
    assert result["mismatched"] == []
    assert set(result["bound"]) == {"deep-paper-note.md", "paper-review.html"}


def test_changed_ai_input_blocks_the_rebind(tmp_path):
    note = tmp_path / "deep-paper-note.md"
    note.write_text("笔记已被改写", encoding="utf-8")
    job = _job({
        "deep-paper-note.md": "0" * 64,
        "paper-review.html": "1" * 64,
    })

    result = ai_binding_check(job, tmp_path)

    assert result["mismatched"] == ["deep-paper-note.md"]


def test_missing_ai_input_also_blocks_the_rebind(tmp_path):
    job = _job({"reading-result.json": "0" * 64, "paper-review.html": "1" * 64})
    assert ai_binding_check(job, tmp_path)["mismatched"] == ["reading-result.json"]


def test_job_without_a_valid_permission_cannot_be_rebound(tmp_path):
    assert ai_binding_check(_job({}, valid=False), tmp_path)["needed"] is False
    assert ai_binding_check({}, tmp_path)["needed"] is False
