"""Central path registry for papers managed by the review application."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import ReviewOrchestrator
from .locking import store_lock


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _display_title(job: dict[str, Any], card: dict[str, Any], sections: dict[str, Any]) -> str:
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(
        sections.get("title")
        or identity.get("title")
        or card.get("title")
        or job["paper_id"]
    )


_REVIEWED_STATES = {"human_confirmed", "indexed", "synthesis_eligible", "excluded"}


def review_group(state: str) -> str:
    """Return the only paper-level split exposed by the review home page."""

    return "reviewed" if state in _REVIEWED_STATES else "unreviewed"


def build_review_registry(kb: str | Path) -> dict[str, Any]:
    """Build one authoritative map without duplicating Zotero PDFs."""

    root = Path(kb).expanduser().resolve()
    workflow = ReviewOrchestrator(root / "review-jobs")
    jobs = workflow.list_jobs()
    jobs_by_key = {str(job["zotero_key"]): job for job in jobs}
    papers_by_key: dict[str, dict[str, Any]] = {}

    # Every migrated paper belongs in the application library, even before an AI
    # reading task exists.  A job record below replaces this lightweight card.
    papers_root = root / "papers"
    if papers_root.is_dir():
        for card_path in sorted(papers_root.glob("*/paper.json")):
            card = _read_json(card_path)
            key = str(card.get("zotero_key") or card_path.parent.name)
            if not key or key in jobs_by_key:
                continue
            identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
            artifacts = card.get("artifacts") if isinstance(card.get("artifacts"), dict) else {}
            state = str(card.get("state") or "legacy_unconfirmed")
            doi = str(identity.get("doi") or "").strip()
            papers_by_key[key] = {
                "title": str(identity.get("title") or card.get("title") or key),
                "paper_id": str(card.get("paper_id") or (f"doi:{doi}" if doi else f"zotero:{key}")),
                "zotero_key": key,
                "job_id": None,
                "state": state,
                "review_group": review_group(state),
                "pdf": {
                    "ownership": card.get("source_provider", "zotero"),
                    "path": artifacts.get("pdf_path"),
                    "sha256": card.get("pdf_hash"),
                },
                "directories": {
                    "paper_card": str(card_path.parent.resolve()),
                    "working_job": None,
                    "human_reviews": None,
                },
                "files": {
                    "review_html": None,
                    "deep_paper_note": artifacts.get("ai_note_path"),
                    "review_sections": None,
                    "latest_human_review": None,
                    "paper_card": str(card_path.resolve()),
                },
            }

    for job in jobs:
        key = str(job["zotero_key"])
        job_dir = Path(job["job_dir"]).resolve()
        output_dir = Path(job["agent_output_dir"]).resolve()
        paper_dir = (root / "papers" / key).resolve()
        review_dir = job_dir / "human_reviews"
        sections_path = output_dir / "review-sections.json"
        card_path = paper_dir / "paper.json"
        card = _read_json(card_path)
        sections = _read_json(sections_path)
        state = str(job["state"])
        papers_by_key[key] = {
                "title": _display_title(job, card, sections),
                "paper_id": job["paper_id"],
                "zotero_key": key,
                "job_id": job["job_id"],
                "state": state,
                "review_group": review_group(state),
                "pdf": {
                    "ownership": job.get("source_provider", "zotero"),
                    "path": job.get("pdf_path"),
                    "sha256": job.get("pdf_sha256"),
                },
                "directories": {
                    "paper_card": str(paper_dir),
                    "working_job": str(job_dir),
                    "human_reviews": str(review_dir),
                },
                "files": {
                    "review_html": str(output_dir / "paper-review.html"),
                    "deep_paper_note": str(output_dir / "deep-paper-note.md"),
                    "review_sections": str(sections_path),
                    "latest_human_review": str(review_dir / f"{key}.human-review.json"),
                    "paper_card": str(card_path),
                },
            }
    papers = list(papers_by_key.values())
    papers.sort(key=lambda item: (item["state"], item["title"].casefold()))
    reviewed_count = sum(item["review_group"] == "reviewed" for item in papers)
    return {
        "schema_version": "review_workflow.registry.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "managed_paper_count": len(papers),
        "active_job_count": len(jobs),
        "reviewed_count": reviewed_count,
        "unreviewed_count": len(papers) - reviewed_count,
        "path_policy": {
            "pdf": "原来源管理；项目保存路径与哈希，不复制PDF",
            "working_files": "review-jobs/jobs/<job-id>/agent-output",
            "human_review": "review-jobs/jobs/<job-id>/human_reviews",
            "paper_card": "papers/<source-key>/paper.json",
        },
        "papers": papers,
    }


def write_review_registry(kb: str | Path) -> tuple[Path, Path]:
    with store_lock(Path(kb).expanduser().resolve() / "review-jobs"):
        return _write_review_registry(kb)


def _write_review_registry(kb: str | Path) -> tuple[Path, Path]:
    root = Path(kb).expanduser().resolve()
    app_dir = root / "review-app"
    registry = build_review_registry(root)
    json_path = app_dir / "library.json"
    markdown_path = app_dir / "论文文件总索引.md"
    _atomic_json(json_path, registry)
    lines = [
        "# 论文阅读台账（唯一清单）",
        "",
        f"当前由评审程序管理 {registry['managed_paper_count']} 篇论文：已评审 {registry['reviewed_count']} 篇，未评审 {registry['unreviewed_count']} 篇。PDF 保留在原来源位置。",
        "",
        "本文件由程序根据 `review-app/library.json` 重建。Codex、WorkBuddy 和其他 Agent 必须先读取本台账，不得凭对话记忆判断论文是否读过。",
        "",
        "| 论文 | 来源键 | 阅读状态 | 流程状态 | 评审页面 |",
        "|---|---|---|---|---|",
    ]
    for paper in registry["papers"]:
        title = str(paper["title"]).replace("|", "\\|")
        read_status = "已读" if paper["review_group"] == "reviewed" else "未读"
        review_html = paper["files"].get("review_html")
        review_page = f"`{review_html}`" if review_html and Path(review_html).is_file() else "—"
        lines.append(
            "| {title} | {key} | {read_status} | {state} | {review_page} |".format(
                title=title,
                key=paper["zotero_key"],
                read_status=read_status,
                state=paper["state"],
                review_page=review_page,
            )
        )
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path.resolve(), markdown_path.resolve()


__all__ = ["build_review_registry", "review_group", "write_review_registry"]
