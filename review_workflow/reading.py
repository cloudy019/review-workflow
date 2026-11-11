"""Engine-neutral single-paper worker and serial publication boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import ReviewOrchestrator, ValidationError, sha256_file
from .html_review import EIGHT_SECTIONS, _local_image_data_uri, write_review_html
from .registry import _atomic_json, write_review_registry


ENGINES = ("codex", "workbuddy")
REQUIRED_FILES = (
    "reader/paper.md", "reader/source_map.json", "deep-paper-note.md",
    "review-sections.json", "figure-completeness.json",
)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValidationError(f"需要JSON对象：{path}")
    return value


def _within(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValidationError(f"产物必须在单篇隔离目录内：{relative}")
    return path


def prepare_reading_task(
    kb: str | Path, job_id: str, engine: str, kit: str | Path,
    *, handoff: bool = False,
) -> dict[str, Any]:
    if engine not in ENGINES:
        raise ValidationError(f"未知阅读执行端：{engine}")
    root = Path(kb).expanduser().resolve()
    job = ReviewOrchestrator(root / "review-jobs").get_job(job_id)
    if job.get("reader_attempt"):
        raise ValidationError("此任务由Evidence Loop统一领取，请使用python -m evidence_loop issue/submit")
    if job["state"] != "reading":
        raise ValidationError("只接续reading任务；本命令不选批、不启动新批次")
    pdf = Path(job["pdf_path"]).resolve()
    if not pdf.is_file() or sha256_file(pdf) != job["pdf_sha256"]:
        raise ValidationError("PDF缺失或哈希已变化，请主任务重新核验来源")
    output = (root / "review-jobs" / "jobs" / job_id / "agent-output").resolve()
    if output != Path(job["agent_output_dir"]).resolve():
        raise ValidationError("项目已迁移，请先更新job中的本地路径再接续")
    kit_root = Path(kit).expanduser().resolve()
    manifest = _json(kit_root / "kit-manifest.json")
    required_skills = list(dict.fromkeys([
        "pdf", "nature-reader", "deeppapernote", *job_skills(root, job_id),
    ]))
    skill_paths = {}
    for name in required_skills:
        skill = kit_root / "references" / "skills" / name / "SKILL.md"
        if not skill.is_file():
            raise ValidationError(f"迁移包缺少当前论文需要的技能：{name}")
        skill_paths[name] = str(skill)
    output.mkdir(parents=True, exist_ok=True)
    task_file = output / "reading-task.json"
    if task_file.is_file():
        previous = _json(task_file)
        if previous["engine"] != engine and not handoff:
            raise ValidationError("本篇已交给另一个执行端；确认原执行停止后用--handoff接续")
        if previous["engine"] == engine and previous.get("kit_digest") == manifest["digest"] and all(previous.get(field) == job.get(field) for field in ("job_id", "paper_id", "zotero_key", "pdf_sha256")):
            return {**previous, "task_path": str(task_file)}
    task = {
        "schema_version": "review_workflow.reading_task.v1",
        "engine": engine, "job_id": job_id, "paper_id": job["paper_id"],
        "zotero_key": job["zotero_key"], "batch_id": job.get("active_batch_id"),
        "pdf_path": str(pdf), "pdf_sha256": job["pdf_sha256"],
        "paper_limit": 1, "pdf_limit": 1, "exclusive_output_dir": str(output),
        "knowledge_base": str(root), "kit_digest": manifest["digest"],
        "skill_paths": skill_paths, "required_files": list(REQUIRED_FILES),
        "runner": str(kit_root / "scripts" / "review.py"),
        "workflow": str(kit_root / "references" / "single-paper.md"),
        "review_project": str(root / "review-project.json"),
    }
    _atomic_json(task_file, task)
    return {**task, "task_path": str(task_file)}


def job_skills(kb: Path, job_id: str) -> list[str]:
    manifest = kb / "review-jobs" / "jobs" / job_id / "agent-task.json"
    return list(_json(manifest).get("allowed_skills") or []) if manifest.is_file() else []


def _load_task(task_path: str | Path) -> tuple[dict[str, Any], Path]:
    path = Path(task_path).expanduser().resolve()
    task = _json(path)
    output = Path(task["exclusive_output_dir"]).resolve()
    if path != output / "reading-task.json" or task.get("paper_limit") != 1 or task.get("pdf_limit") != 1:
        raise ValidationError("需要一个隔离单篇阅读任务")
    pdf = Path(task["pdf_path"])
    if not pdf.is_file() or sha256_file(pdf) != task["pdf_sha256"]:
        raise ValidationError("阅读期间PDF已变化或不可用")
    return task, output


def render_reading_output(task_path: str | Path) -> dict[str, Any]:
    """Worker writes only isolated output; does not advance any shared state."""
    task, output = _load_task(task_path)
    for name in REQUIRED_FILES:
        path = _within(output, name)
        if not path.is_file() or not path.read_text(encoding="utf-8-sig").strip():
            raise ValidationError(f"单篇阅读产物未完成：{name}")
    source_map = _json(output / "reader/source_map.json")
    if source_map.get("pdf_sha256", task["pdf_sha256"]) != task["pdf_sha256"]:
        raise ValidationError("原文映射属于另一份PDF")
    supplied = _json(output / "review-sections.json")
    for field in ("job_id", "paper_id", "zotero_key", "pdf_sha256"):
        if field in supplied and supplied[field] != task[field]:
            raise ValidationError(f"评审材料属于另一篇论文：{field}")
    sections = supplied.get("sections", {})
    if not isinstance(sections, dict) or set(sections) != {section["id"] for section in EIGHT_SECTIONS}:
        raise ValidationError("评审材料需要完整八节AI内容")
    for section in sections.values():
        if not isinstance(section, dict) or not str(section.get("ai_content") or "").strip() or not str(section.get("source_anchors") or "").strip():
            raise ValidationError("八节均须包含AI内容和原文锚点")
    note = output / "deep-paper-note.md"
    markdown = note.read_text(encoding="utf-8-sig")
    figures = _json(output / "figure-completeness.json")
    items = figures.get("items")
    if not isinstance(items, list):
        raise ValidationError("图表清单需要items数组；无图表论文可用空数组")
    seen: set[str] = set()
    assets = []
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or item["id"] in seen:
            raise ValidationError("图表清单有缺失或重复标识")
        seen.add(item["id"])
        if not item.get("source_page"):
            raise ValidationError("图表必须记录原文页码")
        if item.get("decision") == "insert":
            relative = str(item.get("asset") or "")
            asset = _within(output, relative)
            if not relative or relative not in markdown or not _local_image_data_uri(str(asset), output):
                raise ValidationError(f"图表未有效插入速读笔记：{item['id']}")
            assets.append(relative)
        elif item.get("decision") != "omit" or not str(item.get("reason") or "").strip():
            raise ValidationError("每张图表须插入，或给出省略原因")
    record = {
        **supplied, **{name: task[name] for name in ("job_id", "paper_id", "zotero_key", "pdf_sha256")},
        "ai_note_sha256": sha256_file(note), "reader_markdown": markdown,
        "reader_base_dir": str(output), "reader_mode": "paper_summary",
        "human_review_dir": str(output.parent / "human_reviews"),
    }
    page = write_review_html(record, output / "paper-review.html")
    receipt = {
        "schema_version": "review_workflow.reading_result.v1", "status": "ready_for_registration",
        "engine": task["engine"], "job_id": task["job_id"], "paper_id": task["paper_id"],
        "zotero_key": task["zotero_key"], "pdf_sha256": task["pdf_sha256"],
        "kit_digest": task["kit_digest"], "review_html": page.name,
        "artifacts": {name: sha256_file(_within(output, name)) for name in dict.fromkeys([*REQUIRED_FILES, *assets, page.name])},
        "citation_permission": {"allowed": False, "valid": False},
    }
    _atomic_json(output / "reading-result.json", receipt)
    return receipt


def extract_reading_source(task_path: str | Path) -> dict[str, Any]:
    """Extract one local PDF for the host model, without any Zotero/network call."""
    task, output = _load_task(task_path)
    import pymupdf

    directory = _within(output, "work-tmp")
    directory.mkdir(parents=True, exist_ok=True)
    pages = directory / "source-pages.jsonl"
    index = directory / "source-pages.json"
    if pages.is_file() and index.is_file():
        existing = _json(index)
        if existing.get("pdf_sha256") == task["pdf_sha256"] and existing.get("text_sha256") == sha256_file(pages):
            return {**existing, "pages_jsonl": str(pages), "cached": True}
    records = []
    with pymupdf.open(task["pdf_path"]) as document:
        for number, page in enumerate(document, 1):
            records.append({"page": number, "text": page.get_text("text", sort=True)})
    pages.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    result = {
        "pdf_sha256": task["pdf_sha256"], "page_count": len(records),
        "text_sha256": sha256_file(pages),
        "pages_requiring_ocr": [record["page"] for record in records if not record["text"].strip()],
        "model_reading_required": True,
    }
    _atomic_json(index, result)
    return {**result, "pages_jsonl": str(pages), "cached": False}


def accept_reading_output(kb: str | Path, job_id: str) -> dict[str, Any]:
    """Coordinator serially registers a worker's hash-bound finished page."""
    root = Path(kb).expanduser().resolve()
    workflow = ReviewOrchestrator(root / "review-jobs")
    job = workflow.get_job(job_id)
    if job.get("reader_attempt"):
        raise ValidationError("此任务由Evidence Loop统一领取，旧accept入口不得覆盖；请使用submit")
    output = Path(job["agent_output_dir"]).resolve()
    task, task_output = _load_task(output / "reading-task.json")
    result = _json(output / "reading-result.json")
    if task_output != output or any(result.get(name) != job.get(name) or task.get(name) != job.get(name) for name in ("job_id", "paper_id", "zotero_key", "pdf_sha256")):
        raise ValidationError("阅读结果与当前任务不一致")
    if result.get("status") != "ready_for_registration":
        raise ValidationError("单篇结果尚未达到待登记状态")
    if any(result.get(field) != task.get(field) for field in ("engine", "kit_digest")):
        raise ValidationError("执行端已交接或能力包已更新，请当前读取器重新render")
    hashes = result.get("artifacts", {})
    if not set([*REQUIRED_FILES, "paper-review.html"]).issubset(hashes):
        raise ValidationError("阅读结果缺少必需产物")
    for name, digest in hashes.items():
        artifact = _within(output, name)
        if not artifact.is_file() or sha256_file(artifact) != digest:
            raise ValidationError(f"阅读产物已变化，需重新render：{name}")
    note = output / "deep-paper-note.md"
    if job["state"] == "reading":
        job = workflow.attach_ai_note(job_id, note)
    if job["state"] == "ai_note_ready":
        if job.get("ai_note_sha256") != hashes["deep-paper-note.md"]:
            raise ValidationError("已绑定笔记哈希与阅读结果不一致")
        job = workflow.begin_human_review(job_id)
    elif job["state"] != "human_reviewing":
        raise ValidationError("只登记reading/ai_note_ready/human_reviewing任务")
    if job.get("ai_note_sha256") != hashes["deep-paper-note.md"]:
        raise ValidationError("已绑定笔记哈希与阅读结果不一致")
    write_review_registry(root)
    return {"job_id": job_id, "state": job["state"], "review_html": str(output / "paper-review.html"), "engine": result["engine"]}
