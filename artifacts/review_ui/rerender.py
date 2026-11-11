"""Re-render one evidence-loop review page with the current template.

Why this exists: the eight-section page is written once by
``Workflow.submit``.  Redesigning the page must not rewrite the bound
``deep-paper-note.md`` (its sha256 is the anchor the human judgement is bound
to), so this script rebuilds only the *page* from the already-bound artefacts
and then updates the one tracked hash that legitimately changes:
``reading_receipt.artifacts['paper-review.html']``.

Everything goes through the framework's own writer and verifier, so
``refresh_artifacts`` stays clean and the job keeps its ``human_reviewing``
state.

Usage:
    python artifacts/review_ui/rerender.py <job_id> [...] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evidence-loop" / "src"))

from evidence_loop.core import ReviewOrchestrator  # noqa: E402
from evidence_loop.html_review import write_review_html  # noqa: E402
from evidence_loop.locking import store_lock  # noqa: E402

KB = ROOT / "文献知识库"
JOBS = KB / "review-jobs" / "jobs"
TRASH = ROOT / ".workbuddy" / "trash"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_record(job: dict) -> dict:
    """Mirror Workflow.submit's record construction exactly."""

    output = Path(job["agent_output_dir"])
    attempt_id = job["reader_attempt"]["id"]
    request = json.loads((output / "runs" / attempt_id / "request.json").read_text(encoding="utf-8"))
    result = json.loads((output / "reading-result.json").read_text(encoding="utf-8"))
    note = Path(job["ai_note_path"])
    reading = note.read_text(encoding="utf-8") + "\n\n## 原文页图\n"
    for page in request["source_pages"]:
        reading += (
            f"\n### 第 {page['page']} 页\n\n"
            f"![原文第 {page['page']} 页](runs/{attempt_id}/source-pages/{page['image']})\n"
        )
    return {
        **result,
        "job_id": job["job_id"],
        "zotero_key": job["zotero_key"],
        "ai_note_sha256": job["ai_note_sha256"],
        "reader_markdown": reading,
        "reader_base_dir": str(output),
        "human_review_dir": str(Path(job["job_dir"]) / "human_reviews"),
    }


def ai_binding_check(job: dict, output: Path) -> dict:
    """Prove that a view-only re-render changes none of the AI-verified inputs.

    ``refresh_artifacts`` invalidates the AI permission when any bound artifact
    hash changes.  A template/crop fix legitimately changes only the derived
    ``paper-review.html``; every other bound input must still match byte for
    byte, otherwise the rebind is not allowed and the job needs re-verification.
    """

    permission = job.get("ai_citation_permission") or {}
    bound = permission.get("bound_artifacts") or {}
    if not permission.get("valid") or not bound:
        return {"needed": False, "mismatched": [], "bound": {}}
    mismatched = [
        relative for relative, digest in bound.items()
        if relative != "paper-review.html"
        and (not (output / relative).is_file() or sha256(output / relative) != digest)
    ]
    return {"needed": True, "mismatched": mismatched, "bound": bound}


def rerender(
    orchestrator: ReviewOrchestrator,
    job_id: str,
    dry_run: bool = False,
    rebind_ai_view: bool = False,
) -> dict:
    with store_lock(orchestrator.root):
        job = orchestrator.get_job(job_id)
        page = Path(job["agent_output_dir"]) / "paper-review.html"
        output = Path(job["agent_output_dir"])
        before = sha256(page) if page.is_file() else None
        binding = ai_binding_check(job, output) if rebind_ai_view else {"needed": False}
        if rebind_ai_view and not binding["needed"]:
            raise SystemExit(f"{job_id}: 没有有效的 AI 核验绑定，不能使用 --rebind-ai-view")
        if rebind_ai_view and binding["mismatched"]:
            raise SystemExit(
                f"{job_id}: 除页面外的 AI 核验产物已变，禁止重绑定，需重新核验："
                + "、".join(binding["mismatched"])
            )
        if not rebind_ai_view and ai_binding_check(job, output)["needed"] and not dry_run:
            print(
                f"[warn] {job_id} 有 AI 核验绑定；仅改页面会让 refresh_artifacts 判为产物变更。"
                " 如确认只改视图，请加 --rebind-ai-view。",
                file=sys.stderr,
            )
        record = build_record(job)
        if dry_run:
            from evidence_loop.html_review import render_review_html

            html = render_review_html(record)
            return {
                "job_id": job_id,
                "state": job["state"],
                "bytes": len(html.encode("utf-8")),
                "claims": len(record.get("claims") or []),
                "figure_decisions": len(record.get("figure_decisions") or []),
            }
        job_path = Path(job["job_dir"]) / "job.json"
        TRASH.mkdir(parents=True, exist_ok=True)
        shutil.copy2(job_path, TRASH / f"{job_id}.job.json.{(before or 'new')[:12]}.bak")
        write_review_html(record, page)
        after = sha256(page)
        artifacts = job.setdefault("reading_receipt", {}).setdefault("artifacts", {})
        tracked = "paper-review.html" in artifacts
        artifacts["paper-review.html"] = after
        rebind_log = None
        if rebind_ai_view:
            permission = job["ai_citation_permission"]
            bound = permission["bound_artifacts"]
            inputs_digest = hashlib.sha256(
                json.dumps(
                    {key: value for key, value in bound.items() if key != "paper-review.html"},
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            rebind_log = {
                "at": datetime.now(timezone.utc).isoformat(),
                "artifact": "paper-review.html",
                "sha_before": before,
                "sha_after": after,
                "reason": "视图层重渲染（模板/裁图修复）；所有 AI 核验输入逐字节未变",
                "unchanged_inputs_sha256": inputs_digest,
                "unchanged_inputs_count": len(bound) - 1,
            }
            bound["paper-review.html"] = after
            job.setdefault("view_rebind_log", []).append(rebind_log)
        orchestrator._save_job(job)

    verified = orchestrator.refresh_artifacts(job_id)
    return {
        "job_id": job_id,
        "state_before": job["state"],
        "state_after": verified["state"],
        "receipt_invalid": bool(verified.get("reading_receipt_invalid")),
        "hash_tracked": tracked,
        "ai_view_rebound": rebind_ai_view,
        "sha_before": (before or "")[:12],
        "sha_after": after[:12],
        "mb": round(page.stat().st_size / 1e6, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_ids", nargs="+")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--rebind-ai-view",
        action="store_true",
        help="只改 paper-review.html 时，校验其余 AI 核验产物未变并同步该绑定哈希",
    )
    args = parser.parse_args()
    orchestrator = ReviewOrchestrator(KB / "review-jobs")
    for job_id in args.job_ids:
        print(json.dumps(
            rerender(orchestrator, job_id, args.dry_run, args.rebind_ai_view),
            ensure_ascii=False,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
