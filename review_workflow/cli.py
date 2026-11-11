"""Command-line entry points for the human-in-the-loop review workflow."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.request import urlopen
import webbrowser
from collections import Counter

from .core import ReviewOrchestrator
from .html_review import write_review_html
from .library import build_confirmed_outputs, load_paper_cards
from .candidate_sources import verified_source_is_current
from .queue import (
    QueueError,
    atomic_json as queue_atomic_json,
    build_candidate_pool,
    queue_lock,
    reserve_next_batch,
    selection_markdown,
    verify_pool_sources,
)
from .skill_router import route_skills
from .review_server import make_server
from .registry import write_review_registry
from .writing import build_argument_package, load_confirmed_cards
from .workbuddy_bridge import _pid_is_running, launch_workbuddy, request_queue_refresh
from .attachment_index import apply_index, backfill_missing, refresh_index
from .reading import accept_reading_output, extract_reading_source, prepare_reading_task, render_reading_output
from .reading_kit import build_reading_kit, default_kit, install_reading_kit, reading_capabilities


DEFAULT_KB = Path("文献知识库")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _orchestrator(kb: Path) -> ReviewOrchestrator:
    return ReviewOrchestrator(kb / "review-jobs")


def _approved_claims(permission: Mapping[str, Any]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for number, value in enumerate(permission.get("approved_claims") or [], 1):
        if isinstance(value, Mapping):
            claim = dict(value)
            claim.setdefault("claim_id", f"H{number}")
            claim.setdefault("text", str(claim.get("claim") or claim["claim_id"]))
        else:
            claim = {"claim_id": f"H{number}", "text": str(value)}
        claims.append(claim)
    return claims


def _ai_candidate_claims(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    if job.get("reading_receipt_invalid"):
        return []
    canonical = Path(job["agent_output_dir"]) / "reading-result.json"
    if job.get("reading_receipt") and canonical.is_file():
        result = _read_json(canonical)
        if result.get("pdf_sha256") != job.get("pdf_sha256"):
            return []
        return [{**claim, "candidate_id": claim["claim_id"], "status": "pending_argument_package_approval", "source": "evidence_loop.reading_result"} for claim in result.get("claims", [])]
    sections_path = Path(job["agent_output_dir"]) / "review-sections.json"
    if not sections_path.is_file():
        return []
    sections_record = _read_json(sections_path)
    sections = sections_record.get("sections")
    if not isinstance(sections, Mapping):
        return []
    synthesis = sections.get("synthesis_permission")
    if not isinstance(synthesis, Mapping):
        return []
    text = str(synthesis.get("ai_content") or "").strip()
    if not text:
        return []
    return [
        {
            "candidate_id": "AI-SYNTHESIS-1",
            "text": text,
            "source": "review-sections.synthesis_permission",
            "status": "pending_argument_package_approval",
        }
    ]


def _materialize_card(kb: Path, job: Mapping[str, Any]) -> Path:
    """Serially write the durable card without granting AI-derived approval."""

    job = _orchestrator(kb).refresh_artifacts(job["job_id"])
    key = str(job["zotero_key"])
    paper_dir = kb / "papers" / key
    paper_dir.mkdir(parents=True, exist_ok=True)
    card_path = paper_dir / "paper.json"
    existing = _read_json(card_path) if card_path.is_file() else {}
    permission = dict(job.get("citation_permission") or {})
    archive_path = Path(str(permission.get("review_archive") or ""))
    review = _read_json(archive_path) if archive_path.is_file() else {}
    claims = _approved_claims(permission)
    if job.get("reading_receipt"):
        from evidence_loop.workflow import Workflow
        approved = next((paper for paper in Workflow(kb).knowledge(approved_only=True) if paper["paper_id"] == job["paper_id"]), {})
        claims = [{**claim, "locator": "PDF p." + ",".join(str(page) for page in claim["source_pages"])} for claim in approved.get("claims", [])]
    candidate_claims = _ai_candidate_claims(job)

    allowed = bool(permission.get("allowed"))
    valid = bool(permission.get("valid"))
    normalized_permission = {
        **permission,
        "allowed": allowed,
        "valid": valid,
        "approved_claims": [item["claim_id"] for item in claims],
        # The aggregation layer deliberately binds approval to both artifacts.
        "pdf_hash": job.get("pdf_sha256"),
        "note_hash": job.get("ai_note_sha256"),
    }
    identity = dict(existing.get("identity") or {})
    identity.setdefault("title", existing.get("title") or job["paper_id"])

    card = {
        **existing,
        "schema_version": "review_workflow.paper.v1",
        "zotero_key": key,
        "paper_id": job["paper_id"],
        "state": "human_confirmed" if job["state"] in {"human_confirmed", "indexed", "synthesis_eligible"} else job["state"],
        "workflow_state": job["state"],
        "identity": identity,
        "pdf_hash": job.get("pdf_sha256"),
        "note_hash": job.get("ai_note_sha256"),
        "artifacts": {
            "pdf_path": job.get("pdf_path"),
            "ai_note_path": job.get("ai_note_path"),
            "review_html": str(paper_dir / "review.html"),
            "human_review_archive": str(archive_path) if archive_path else None,
        },
        "human_review": {
            "status": review.get("review_outcome", job["state"]),
            "sections": review.get("sections", []),
            "approved_claims": [item["claim_id"] for item in claims],
            "exported_at": review.get("exported_at"),
            "imported_at": review.get("imported_at"),
        },
        "citation_permission": normalized_permission,
        "approved_claims": [item["claim_id"] for item in claims],
        "confirmed_claims": claims,
        "confirmed_knowledge": {
            "confirmed_claims": claims,
            "candidate_claims": candidate_claims,
            "human_sections": review.get("sections", []),
        },
        "candidate_claims": candidate_claims,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    _write_json(card_path, card)

    lines = [
        f"# {identity['title']}",
        "",
        f"- Zotero Key：`{key}`",
        f"- 状态：`{job['state']}`",
        f"- PDF SHA-256：`{job.get('pdf_sha256') or '无'}`",
        f"- AI 笔记 SHA-256：`{job.get('ai_note_sha256') or '无'}`",
        f"- 人工引用许可：`{'有效' if allowed and valid else '无效'}`",
        "",
        "## 人工获准主张",
        "",
    ]
    lines.extend(
        [f"- `{item['claim_id']}` {item['text']}" for item in claims]
        or ["- 尚无人工获准主张。"]
    )
    lines.extend(
        [
            "",
            "## 分层说明",
            "",
            "原文内容、AI分析和人工判断保存在不同产物中；本文件仅把人工许可后的主张暴露给综合与写作层。",
        ]
    )
    (paper_dir / "paper.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return card_path


def _matrix_markdown(outputs: Mapping[str, Any]) -> str:
    lines = [
        "# 人工确认文献矩阵",
        "",
        "> 本文件只由人工确认、哈希有效且获准引用的单篇卡片生成。",
        "",
        f"- 可综合论文：{len(outputs['eligible_zotero_keys'])}",
        f"- 获准主张：{len(outputs['writing_candidates'])}",
        "",
        "| Zotero Key | 题名 | 主张/用途 |",
        "|---|---|---|",
    ]
    for row in outputs["writing_candidates"]:
        claim = row.get("text") or row.get("claim") or row.get("value") or ""
        lines.append(
            "| {key} | {title} | {claim} |".format(
                key=str(row.get("zotero_key") or "").replace("|", "\\|"),
                title=str(row.get("title") or "").replace("|", "\\|"),
                claim=str(claim).replace("|", "\\|").replace("\n", " "),
            )
        )
    if not outputs["writing_candidates"]:
        lines.append("| — | — | 当前无获准主张 |")
    return "\n".join(lines) + "\n"


def _update_readme_status(
    kb: Path, cards: Sequence[Mapping[str, Any]], outputs: Mapping[str, Any]
) -> None:
    readme = kb / "README.md"
    original = readme.read_text(encoding="utf-8") if readme.is_file() else "# 文献知识库\n"
    state_counts = Counter(str(card.get("state") or "unknown") for card in cards)
    states = "；".join(f"{name} {count}" for name, count in sorted(state_counts.items()))
    start = "<!-- review-workflow-status:start -->"
    end = "<!-- review-workflow-status:end -->"
    block = "\n".join(
        [
            start,
            "## 人工评审状态（自动生成）",
            "",
            f"- 单篇卡片：{len(cards)}；状态：{states or '无'}。",
            f"- 可进入综合：{outputs['stats']['eligible_card_count']}；获准写作主张：{outputs['stats']['writing_candidate_count']}。",
            "- 只有人工确认且 PDF/AI 笔记哈希仍匹配的主张会进入生成矩阵。",
            end,
        ]
    )
    if start in original and end in original:
        prefix, rest = original.split(start, 1)
        _, suffix = rest.split(end, 1)
        updated = prefix.rstrip() + "\n\n" + block + suffix
    else:
        updated = original.rstrip() + "\n\n" + block + "\n"
    readme.write_text(updated, encoding="utf-8")


def _cmd_prepare(args: argparse.Namespace) -> dict[str, Any]:
    job = _orchestrator(args.kb).prepare_paper(
        args.paper_id, pdf_path=args.pdf, zotero_key=args.zotero_key
    )
    return job


def _cmd_dispatch(args: argparse.Namespace) -> dict[str, Any]:
    route = route_skills(
        "read",
        paper_type=args.paper_type,
        has_statistics=args.statistics,
        has_structure_data=args.structure_data,
    )
    allowed = {job_id: route.skills for job_id in args.job_ids}
    return _orchestrator(args.kb).dispatch_reading_batch(args.job_ids, allowed)


def _cmd_attach_note(args: argparse.Namespace) -> dict[str, Any]:
    return _orchestrator(args.kb).attach_ai_note(args.job_id, args.note)


def _cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    orchestrator = _orchestrator(args.kb)
    job = orchestrator.get_job(args.job_id)
    supplied = _read_json(args.record)
    reader_path = getattr(args, "reader", None) or Path(job["agent_output_dir"]) / "deep-paper-note.md"
    if not reader_path.is_file():
        raise ValueError(f"缺少本论文的深度总结笔记：{reader_path}")
    human_review_dir = Path(job["job_dir"]) / "human_reviews"
    human_review_dir.mkdir(parents=True, exist_ok=True)
    record = {
        **supplied,
        "job_id": job["job_id"],
        "paper_id": job["paper_id"],
        "zotero_key": job["zotero_key"],
        "pdf_sha256": job["pdf_sha256"],
        "ai_note_sha256": job["ai_note_sha256"],
        "reader_markdown": reader_path.read_text(encoding="utf-8-sig"),
        "reader_base_dir": str(reader_path.parent.resolve()),
        "reader_mode": "paper_summary",
        "human_review_dir": str(human_review_dir.resolve()),
    }
    output = args.output or args.kb / "papers" / job["zotero_key"] / "review.html"
    write_review_html(record, output)
    if job["state"] == "ai_note_ready":
        orchestrator.begin_human_review(job["job_id"])
    write_review_registry(args.kb)
    return {"job_id": job["job_id"], "review_html": str(output.resolve())}


def _cmd_import(args: argparse.Namespace) -> dict[str, Any]:
    orchestrator = _orchestrator(args.kb)
    job = orchestrator.import_human_review(args.review)
    card_path = _materialize_card(args.kb, job)
    job = orchestrator.get_job(job["job_id"])
    if job["state"] == "human_confirmed":
        job = orchestrator.mark_indexed(job["job_id"])
    if job["state"] == "indexed" and job.get("citation_permission", {}).get("valid"):
        job = orchestrator.mark_synthesis_eligible(job["job_id"])
    if job["state"] in {"indexed", "synthesis_eligible"}:
        # Cards remain human_confirmed as the semantic approval state; job state
        # separately records indexing/synthesis progress.
        card = _read_json(card_path)
        card["state"] = "human_confirmed"
        card["workflow_state"] = orchestrator.get_job(job["job_id"])["state"]
        _write_json(card_path, card)
    final_job = orchestrator.get_job(job["job_id"])
    write_review_registry(args.kb)
    return {
        "job_id": final_job["job_id"],
        "state": final_job["state"],
        "paper_card": str(card_path),
    }


def _cmd_serve_reviews(args: argparse.Namespace) -> dict[str, Any]:
    # 固定 token 让评审链接在服务重启后依然可用；缺省仍由上游随机生成。
    server = make_server(args.kb, port=args.port, token=getattr(args, "token", None))
    host, port = server.server_address
    base = f"http://{host}:{port}"
    if args.job_id:
        # Validate before opening the browser or entering the serving loop.
        server.app.review_page(args.job_id)
        path = f"/jobs/{args.job_id}"
    else:
        path = "/"
    url = f"{base}{path}?token={server.app.token}"
    print(json.dumps({"review_url": url, "message": "关闭此窗口即可停止本机评审服务"}, ensure_ascii=False, indent=2), flush=True)
    ready_file = getattr(args, "ready_file", None)
    if ready_file:
        _write_json(ready_file, {"review_url": url})
    pid_file = getattr(args, "pid_file", None)
    if pid_file:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    if args.open:
        def open_when_ready() -> None:
            for _ in range(80):
                try:
                    with urlopen(url, timeout=0.5) as response:
                        if response.status == 200:
                            webbrowser.open(url)
                            return
                except OSError:
                    time.sleep(0.1)

        threading.Thread(target=open_when_ready, daemon=True).start()
    parent_pid = getattr(args, "parent_pid", None)
    if parent_pid and sys.platform == "win32":
        def stop_when_parent_exits() -> None:
            synchronize = 0x00100000
            infinite = 0xFFFFFFFF
            handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, parent_pid)
            if not handle:
                server.shutdown()
                return
            try:
                ctypes.windll.kernel32.WaitForSingleObject(handle, infinite)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
            server.shutdown()

        threading.Thread(target=stop_when_parent_exits, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if ready_file:
            Path(ready_file).unlink(missing_ok=True)
    return {"stopped": True}


def _sync_core_cards(kb: Path) -> None:
    for job in _orchestrator(kb).list_jobs():
        if job["state"] in {"human_confirmed", "indexed", "synthesis_eligible", "needs_clarification", "rescinded", "excluded"}:
            _materialize_card(kb, job)


def _cmd_rebuild(args: argparse.Namespace) -> dict[str, Any]:
    _sync_core_cards(args.kb)
    cards = load_paper_cards(args.kb / "papers")
    outputs = build_confirmed_outputs(cards)
    generated = args.kb / "generated"
    _write_json(generated / "confirmed-library.json", outputs)
    (generated / "literature-matrix.md").write_text(
        _matrix_markdown(outputs), encoding="utf-8"
    )
    _write_json(generated / "knowledge-graph.json", outputs["knowledge_relations"])
    _update_readme_status(args.kb, cards, outputs)
    dashboard_builder = args.kb / "dashboard" / "build_dashboard.py"
    if dashboard_builder.is_file():
        subprocess.run([sys.executable, str(dashboard_builder)], check=True)
    registry_json, registry_markdown = write_review_registry(args.kb)
    return {
        "generated": str(generated.resolve()),
        "review_registry": str(registry_json),
        "review_registry_markdown": str(registry_markdown),
        **outputs["stats"],
    }


def _cmd_build_section(args: argparse.Namespace) -> dict[str, Any]:
    _sync_core_cards(args.kb)
    cards = load_confirmed_cards(args.kb / "papers")
    package = build_argument_package(args.section_id, cards, section_goal=args.goal)
    output = (
        args.output
        or args.kb / "写作" / "argument-packages" / f"{args.section_id}.json"
    )
    _write_json(output, package)
    return {"argument_package": str(output.resolve()), "evidence_count": len(package["evidence"])}


def _screening_dir(kb: Path) -> Path:
    return kb / "screening"


def _load_pool(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise ValueError("candidate-pool.json格式无效")
    return value


def _cmd_screen_pool(args: argparse.Namespace) -> dict[str, Any]:
    pool = build_candidate_pool(args.source)
    entries, _ = refresh_index(args.kb, args.source)
    pool = apply_index(pool, entries)
    output = args.output or _screening_dir(args.kb) / "candidate-pool.json"
    if output.is_file():
        previous = _load_pool(output)
        if previous.get("source_sha256") == pool["source_sha256"] and previous.get("rubric_version") == pool["rubric_version"]:
            verified_by_key = {row["zotero_key"]: row for row in previous["results"] if row.get("source_verified") is True}
            for row in pool["results"]:
                old = verified_by_key.get(row["zotero_key"])
                if old and old.get("candidate_record_sha256") == row["candidate_record_sha256"] and verified_source_is_current(old):
                    for field in ("source_verified", "verified_pdf_path", "verified_pdf_sha256", "verified_attachment_key", "source_verification_error"):
                        row[field] = old.get(field)
    queue_atomic_json(output, pool)
    result_dir = _screening_dir(args.kb) / "results"
    for result in pool["results"]:
        queue_atomic_json(result_dir / f"{result['zotero_key']}.json", result)
    counts = Counter(item["status"] for item in pool["results"])
    return {
        "candidate_pool": str(output.resolve()),
        "paper_count": pool["record_count"],
        "status_counts": dict(sorted(counts.items())),
        "rubric_version": pool["rubric_version"],
    }


def _cmd_verify_sources(args: argparse.Namespace) -> dict[str, Any]:
    pool_path = args.pool or _screening_dir(args.kb) / "candidate-pool.json"
    pool = _load_pool(pool_path)
    verified = verify_pool_sources(pool, max_attempts=args.max_attempts)
    queue_atomic_json(pool_path, verified)
    result_dir = _screening_dir(args.kb) / "results"
    for result in verified["results"]:
        queue_atomic_json(result_dir / f"{result['zotero_key']}.json", result)
    return {"candidate_pool": str(pool_path.resolve()), **verified["source_verification"]}


def _ensure_no_open_batch(orchestrator: ReviewOrchestrator) -> None:
    for path in sorted(orchestrator.batches_dir.glob("batch-*.json")):
        batch = orchestrator.get_batch(path.stem)
        if batch.get("status") == "open":
            raise QueueError(f"批次{batch['batch_id']}尚未进入人工终态")


def _selection_existing_records(kb: Path, orchestrator: ReviewOrchestrator) -> list[dict[str, Any]]:
    return [*load_paper_cards(kb / "papers"), *orchestrator.list_jobs()]


def _cmd_select_next(args: argparse.Namespace) -> dict[str, Any]:
    screening_dir = _screening_dir(args.kb)
    pool_path = args.pool or screening_dir / "candidate-pool.json"
    reservation_path = args.output or screening_dir / "next-batch.json"
    orchestrator = _orchestrator(args.kb)
    with queue_lock(args.kb / "review-jobs"):
        _ensure_no_open_batch(orchestrator)
        reservation = reserve_next_batch(
            _load_pool(pool_path),
            existing_tasks=_selection_existing_records(args.kb, orchestrator),
            reservation_path=reservation_path,
            max_papers=args.max_papers,
        )
        markdown = reservation_path.with_suffix(".md")
        markdown.write_text(selection_markdown(reservation), encoding="utf-8")
    return {
        "selection_id": reservation["selection_id"],
        "status": reservation["status"],
        "selected_count": len(reservation["selected"]),
        "selected": [
            {
                "title": item["title"],
                "zotero_key": item["zotero_key"],
                "score": item["score"],
                "axes": item["axes"],
                "reason": item["selection_reason"],
            }
            for item in reservation["selected"]
        ],
        "review": str(markdown.resolve()),
    }


def _job_for_selected(
    orchestrator: ReviewOrchestrator, selected: Mapping[str, Any]
) -> dict[str, Any]:
    key = str(selected["zotero_key"])
    matches = [job for job in orchestrator.list_jobs() if job.get("zotero_key") == key]
    if len(matches) > 1:
        raise QueueError(f"Zotero Key {key}存在多个任务")
    pdf = Path(str(selected["verified_pdf_path"]))
    if matches:
        job = matches[0]
        if job["state"] == "candidate":
            job = orchestrator.verify_identity(job["job_id"])
        if job["state"] == "identity_verified":
            job = orchestrator.attach_pdf(job["job_id"], pdf)
        if job["state"] != "source_ready":
            raise QueueError(f"{key}已有任务处于{job['state']}，不能重复领取")
        if job.get("pdf_sha256") != selected.get("verified_pdf_sha256"):
            raise QueueError(f"{key}现有任务PDF与本次来源核验不一致")
        return job
    paper_id = str(selected.get("doi") or f"zotero:{key}")
    if selected.get("doi"):
        paper_id = f"doi:{paper_id}"
    return orchestrator.prepare_paper(paper_id, pdf, zotero_key=key)


def _route_selected(selected: Mapping[str, Any]) -> tuple[str, ...]:
    axes = set(selected.get("axes") or [])
    paper_type = "ml" if "method" in axes else "materials"
    return route_skills("read", paper_type=paper_type).skills


def _cmd_start_next(args: argparse.Namespace) -> dict[str, Any]:
    selection_path = args.selection or _screening_dir(args.kb) / "next-batch.json"
    orchestrator = _orchestrator(args.kb)
    with queue_lock(args.kb / "review-jobs"):
        reservation = _read_json(selection_path)
        if reservation.get("status") == "started":
            return {
                "selection_id": reservation["selection_id"],
                "status": "started",
                "batch_id": reservation["batch_id"],
                "agent_tasks": reservation.get("agent_tasks", []),
            }
        if reservation.get("status") != "reserved":
            raise QueueError("下一批没有有效的reserved预约")
        _ensure_no_open_batch(orchestrator)
        selected = reservation.get("selected") or []
        if not 1 <= len(selected) <= 3:
            raise QueueError("预约必须包含1至3篇论文")
        if len({item.get("zotero_key") for item in selected}) != len(selected):
            raise QueueError("预约中存在重复Zotero Key")
        if not all(verified_source_is_current(item) for item in selected):
            reservation["status"] = "stale"
            queue_atomic_json(selection_path, reservation)
            raise QueueError("至少一篇PDF已变化或不可用，预约已标记stale")

        jobs = [_job_for_selected(orchestrator, item) for item in selected]
        allowed = {
            job["job_id"]: _route_selected(item)
            for job, item in zip(jobs, selected, strict=True)
        }
        batch = orchestrator.dispatch_reading_batch(
            [job["job_id"] for job in jobs], allowed
        )
        reservation["status"] = "started"
        reservation["batch_id"] = batch["batch_id"]
        reservation["agent_tasks"] = batch["agent_tasks"]
        queue_atomic_json(selection_path, reservation)
    return {
        "selection_id": reservation["selection_id"],
        "status": "started",
        "batch_id": batch["batch_id"],
        "agent_tasks": batch["agent_tasks"],
    }


def _cmd_auto_start(args: argparse.Namespace) -> dict[str, Any]:
    # Fail before rebuilding the candidate pool or touching source-verification
    # state when the three-paper human gate is still open.
    _ensure_no_open_batch(_orchestrator(args.kb))
    _cmd_screen_pool(
        argparse.Namespace(
            kb=args.kb,
            source=args.source,
            output=args.kb / "screening" / "candidate-pool.json",
        )
    )
    _cmd_verify_sources(
        argparse.Namespace(
            kb=args.kb,
            pool=args.kb / "screening" / "candidate-pool.json",
            max_attempts=args.max_attempts,
        )
    )
    selection = _cmd_select_next(
        argparse.Namespace(
            kb=args.kb,
            pool=args.kb / "screening" / "candidate-pool.json",
            output=args.kb / "screening" / "next-batch.json",
            max_papers=args.max_papers,
        )
    )
    started = _cmd_start_next(
        argparse.Namespace(
            kb=args.kb,
            selection=args.kb / "screening" / "next-batch.json",
        )
    )
    return {"selection": selection, "reading_batch": started}


def _cmd_request_workbuddy(args: argparse.Namespace) -> dict[str, Any]:
    request_path, request = request_queue_refresh(args.kb)
    result: dict[str, Any] = {
        "request": str(request_path),
        "status": request.get("status"),
        "counts": request.get("counts", {}),
        "attachment_stats": request.get("attachment_stats", {}),
    }
    process_running = _pid_is_running(request.get("workbuddy_pid"))
    if args.launch and not process_running:
        result.update(launch_workbuddy(args.kb, request_path))
    return result


def _cmd_backfill_pdf_keys(args: argparse.Namespace) -> dict[str, Any]:
    return backfill_missing(args.kb, limit=args.limit, retry_checked=args.retry_checked)


def _cmd_reading_kit(args: argparse.Namespace) -> Any:
    if args.command == "build-reading-kit":
        return build_reading_kit(args.output, args.skills_root)
    kit = args.kit or default_kit(args.kb.resolve().parent, args.engine)
    if args.command == "install-reading-kit":
        engines = ("codex", "workbuddy") if args.engine == "both" else (args.engine,)
        if args.target and args.engine == "both":
            raise ValueError("--target需指定单一engine")
        return [install_reading_kit(args.kit, args.target or default_kit(args.kb.resolve().parent, engine)) for engine in engines]
    if args.command == "reading-capabilities":
        return reading_capabilities(kit)
    return prepare_reading_task(args.kb, args.job_id, args.engine, kit, handoff=args.handoff)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="人在回路综述编排器")
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB, help="文献知识库目录")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare-paper", help="创建且仅创建一个单篇任务")
    prepare.add_argument("paper_id")
    prepare.add_argument("--zotero-key", required=True)
    prepare.add_argument("--pdf", type=Path)
    prepare.set_defaults(handler=_cmd_prepare)

    dispatch = commands.add_parser("dispatch-reading-batch", help="生成1—3个隔离阅读任务")
    dispatch.add_argument("job_ids", nargs="+")
    dispatch.add_argument("--paper-type", choices=("general", "ml", "materials"), default="general")
    dispatch.add_argument("--statistics", action="store_true")
    dispatch.add_argument("--structure-data", action="store_true")
    dispatch.set_defaults(handler=_cmd_dispatch)

    attach = commands.add_parser("attach-note", help="绑定一个单篇AI笔记")
    attach.add_argument("job_id")
    attach.add_argument("note", type=Path)
    attach.set_defaults(handler=_cmd_attach_note)

    render = commands.add_parser("render-review", help="生成单篇离线人工评审页")
    render.add_argument("job_id")
    render.add_argument("record", type=Path, help="八节AI阅读材料JSON")
    render.add_argument("--reader", type=Path, help="本论文的DeepPaperNote总结Markdown")
    render.add_argument("--output", type=Path)
    render.set_defaults(handler=_cmd_render)

    import_review = commands.add_parser("import-human-review", help="校验人工记录并正式入库")
    import_review.add_argument("review", type=Path)
    import_review.set_defaults(handler=_cmd_import)

    serve_reviews = commands.add_parser("serve-reviews", help="启动仅限本机的人工评审页面")
    serve_reviews.add_argument("--job-id", help="直接打开某一篇论文；省略则显示论文列表")
    serve_reviews.add_argument("--port", type=int, default=0)
    serve_reviews.add_argument("--token", help="固定访问令牌；省略则每次启动随机生成，链接随进程失效")
    serve_reviews.add_argument("--open", action="store_true", help="自动用默认浏览器打开")
    serve_reviews.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    serve_reviews.add_argument("--pid-file", type=Path, help=argparse.SUPPRESS)
    serve_reviews.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    serve_reviews.set_defaults(handler=_cmd_serve_reviews)

    rebuild = commands.add_parser("rebuild-library", help="只从确认卡片重建综合产物")
    rebuild.set_defaults(handler=_cmd_rebuild)

    section = commands.add_parser("build-section", help="生成待人工审批的章节论点包")
    section.add_argument("section_id")
    section.add_argument("--goal", required=True)
    section.add_argument("--output", type=Path)
    section.set_defaults(handler=_cmd_build_section)

    screen_pool = commands.add_parser("screen-candidate-pool", help="按冻结标准逐篇初筛候选池")
    screen_pool.add_argument("source", type=Path)
    screen_pool.add_argument("--output", type=Path)
    screen_pool.set_defaults(handler=_cmd_screen_pool)

    verify_sources = commands.add_parser("verify-candidate-sources", help="经内置Zotero插件逐篇核验PDF")
    verify_sources.add_argument("--pool", type=Path)
    verify_sources.add_argument("--max-attempts", type=int, default=24)
    verify_sources.set_defaults(handler=_cmd_verify_sources)

    select_next = commands.add_parser("select-next-batch", help="AI自动选择下一批，不接受论文ID")
    select_next.add_argument("--pool", type=Path)
    select_next.add_argument("--output", type=Path)
    select_next.add_argument("--max-papers", type=int, choices=(1, 2, 3), default=3)
    select_next.set_defaults(handler=_cmd_select_next)

    start_next = commands.add_parser("start-next-batch", help="复核预约并生成逐篇代理任务")
    start_next.add_argument("--selection", type=Path)
    start_next.set_defaults(handler=_cmd_start_next)

    auto_start = commands.add_parser("auto-start-next-batch", help="初筛、核验来源、自动选批并启动")
    auto_start.add_argument(
        "--source",
        type=Path,
        default=Path("workspace/candidates/papers-screened.json"),
    )
    auto_start.add_argument("--max-attempts", type=int, default=24)
    auto_start.add_argument("--max-papers", type=int, choices=(1, 2, 3), default=3)
    auto_start.set_defaults(handler=_cmd_auto_start)

    workbuddy = commands.add_parser(
        "request-workbuddy-refresh", help="按项目台账把下一批阅读任务交给WorkBuddy"
    )
    workbuddy.add_argument("--launch", action="store_true", help="立即启动本机WorkBuddy处理")
    workbuddy.set_defaults(handler=_cmd_request_workbuddy)
    backfill = commands.add_parser("backfill-pdf-keys", help="队列更新后独立批量补全PDF附件键")
    backfill.add_argument("--limit", type=int, default=20)
    backfill.add_argument("--retry-checked", action="store_true", help="重新检查曾确认无PDF的条目")
    backfill.set_defaults(handler=_cmd_backfill_pdf_keys)
    kit_build = commands.add_parser("build-reading-kit", help="导出可迁移的双端AI阅读技能包")
    kit_build.add_argument("--output", type=Path, required=True)
    kit_build.add_argument("--skills-root", type=Path)
    kit_build.set_defaults(handler=_cmd_reading_kit)
    kit_install = commands.add_parser("install-reading-kit", help="安装同一能力包到Codex/WorkBuddy")
    kit_install.add_argument("--kit", type=Path, required=True)
    kit_install.add_argument("--engine", choices=("codex", "workbuddy", "both"), default="both")
    kit_install.add_argument("--target", type=Path)
    kit_install.set_defaults(handler=_cmd_reading_kit)
    for command in ("reading-capabilities", "prepare-reading-task"):
        reader = commands.add_parser(command)
        reader.add_argument("--kit", type=Path)
        reader.add_argument("--engine", choices=("codex", "workbuddy"), required=True)
        if command == "prepare-reading-task":
            reader.add_argument("job_id")
            reader.add_argument("--handoff", action="store_true")
        reader.set_defaults(handler=_cmd_reading_kit)
    reader_render = commands.add_parser("render-reading-output", help="单篇读取器仅生成隔离评审页")
    reader_render.add_argument("task", type=Path)
    reader_render.set_defaults(handler=lambda args: render_reading_output(args.task))
    reader_extract = commands.add_parser("extract-reading-source", help="抽取单篇原文供当前宿主模型阅读")
    reader_extract.add_argument("task", type=Path)
    reader_extract.set_defaults(handler=lambda args: extract_reading_source(args.task))
    reader_accept = commands.add_parser("accept-reading-output", help="主任务串行登记AI阅读完成，等待人工评审")
    reader_accept.add_argument("job_id")
    reader_accept.set_defaults(handler=lambda args: accept_reading_output(args.kb, args.job_id))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = args.handler(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
