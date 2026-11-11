"""Core orchestration primitives for a human-in-the-loop literature review.

The module deliberately does not call an LLM, a skill, or Zotero.  It defines
the local contract used by those integrations: one paper per job, isolated
agent output, explicit state transitions, batch gates, and hash-bound human
approval.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .locking import serialized


SCHEMA_VERSION = "1.0"

STATE_CANDIDATE = "candidate"
STATE_IDENTITY_VERIFIED = "identity_verified"
STATE_SOURCE_READY = "source_ready"
STATE_READING = "reading"
STATE_AI_NOTE_READY = "ai_note_ready"
STATE_AI_VERIFIED = "ai_verified"
STATE_HUMAN_REVIEWING = "human_reviewing"
STATE_HUMAN_CONFIRMED = "human_confirmed"
STATE_INDEXED = "indexed"
STATE_SYNTHESIS_ELIGIBLE = "synthesis_eligible"
STATE_EXCLUDED = "excluded"
STATE_BLOCKED_SOURCE = "blocked_source"
STATE_NEEDS_CLARIFICATION = "needs_clarification"
STATE_RESCINDED = "rescinded"

ALL_STATES = frozenset(
    {
        STATE_CANDIDATE,
        STATE_IDENTITY_VERIFIED,
        STATE_SOURCE_READY,
        STATE_READING,
        STATE_AI_NOTE_READY,
        STATE_AI_VERIFIED,
        STATE_HUMAN_REVIEWING,
        STATE_HUMAN_CONFIRMED,
        STATE_INDEXED,
        STATE_SYNTHESIS_ELIGIBLE,
        STATE_EXCLUDED,
        STATE_BLOCKED_SOURCE,
        STATE_NEEDS_CLARIFICATION,
        STATE_RESCINDED,
    }
)

# A batch can be released after a human decision or an explicit terminal stop.
BATCH_TERMINAL_STATES = frozenset(
    {
        STATE_HUMAN_CONFIRMED,
        STATE_INDEXED,
        STATE_SYNTHESIS_ELIGIBLE,
        STATE_EXCLUDED,
        STATE_BLOCKED_SOURCE,
        STATE_RESCINDED,
    }
)

_TRANSITIONS: Mapping[str, frozenset[str]] = {
    STATE_AI_VERIFIED: frozenset({STATE_HUMAN_CONFIRMED, STATE_NEEDS_CLARIFICATION,
                                  STATE_EXCLUDED, STATE_BLOCKED_SOURCE, STATE_RESCINDED}),
    STATE_CANDIDATE: frozenset(
        {STATE_IDENTITY_VERIFIED, STATE_EXCLUDED, STATE_BLOCKED_SOURCE}
    ),
    STATE_IDENTITY_VERIFIED: frozenset(
        {STATE_SOURCE_READY, STATE_EXCLUDED, STATE_BLOCKED_SOURCE}
    ),
    STATE_SOURCE_READY: frozenset(
        {STATE_READING, STATE_EXCLUDED, STATE_BLOCKED_SOURCE}
    ),
    STATE_READING: frozenset(
        {STATE_AI_NOTE_READY, STATE_EXCLUDED, STATE_BLOCKED_SOURCE}
    ),
    STATE_AI_NOTE_READY: frozenset(
        {STATE_HUMAN_REVIEWING, STATE_EXCLUDED, STATE_BLOCKED_SOURCE}
    ),
    STATE_HUMAN_REVIEWING: frozenset(
        {
            STATE_HUMAN_CONFIRMED,
            STATE_NEEDS_CLARIFICATION,
            STATE_EXCLUDED,
            STATE_BLOCKED_SOURCE,
        }
    ),
    STATE_NEEDS_CLARIFICATION: frozenset(
        {
            STATE_HUMAN_REVIEWING,
            STATE_HUMAN_CONFIRMED,
            STATE_EXCLUDED,
            STATE_BLOCKED_SOURCE,
        }
    ),
    STATE_HUMAN_CONFIRMED: frozenset({STATE_INDEXED, STATE_RESCINDED}),
    STATE_INDEXED: frozenset({STATE_SYNTHESIS_ELIGIBLE, STATE_RESCINDED}),
    STATE_SYNTHESIS_ELIGIBLE: frozenset({STATE_RESCINDED}),
    STATE_EXCLUDED: frozenset(),
    STATE_BLOCKED_SOURCE: frozenset(),
    STATE_RESCINDED: frozenset(),
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SINGLE_ID_FORBIDDEN_RE = re.compile(r"[\s;,|]")


class ReviewWorkflowError(RuntimeError):
    """Base class for workflow contract violations."""


class ValidationError(ReviewWorkflowError):
    """Input or persisted data does not satisfy the workflow contract."""


class StateTransitionError(ReviewWorkflowError):
    """A requested state transition is not legal."""


class BatchGateError(ReviewWorkflowError):
    """A reading batch violates concurrency or human-review gates."""


class StaleReviewError(ReviewWorkflowError):
    """A human review targets an older PDF or AI note."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: os.PathLike[str] | str) -> str:
    """Return a streaming SHA-256 digest for a regular file."""

    source = Path(path)
    if not source.is_file():
        raise ValidationError(f"文件不存在或不是普通文件：{source}")
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"无法读取JSON：{path}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"JSON顶层必须是对象：{path}")
    return data


def _validate_paper_id(paper_id: Any) -> str:
    if not isinstance(paper_id, str):
        raise ValidationError("paper_id必须是单个字符串，不能是数组或对象")
    value = paper_id.strip()
    if not value:
        raise ValidationError("paper_id不能为空")
    if _SINGLE_ID_FORBIDDEN_RE.search(value):
        raise ValidationError("paper_id只能包含一个无空白、无分隔符的唯一标识")
    return value


def _validate_single_pdf(pdf_path: Any) -> Path | None:
    if pdf_path is None:
        return None
    if isinstance(pdf_path, (list, tuple, set, frozenset, dict)):
        raise ValidationError("一个任务最多只能提供一个PDF")
    if not isinstance(pdf_path, (str, os.PathLike)):
        raise ValidationError("pdf_path必须是单个路径")
    path = Path(pdf_path).expanduser().resolve()
    if path.suffix.lower() != ".pdf":
        raise ValidationError("全文来源必须是单个PDF文件")
    if not path.is_file():
        raise ValidationError(f"PDF不存在：{path}")
    return path


def _validate_sha256(value: Any, field: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValidationError(f"{field}必须是64位小写SHA-256，或符合字段约束的null")


class ReviewOrchestrator:
    """Filesystem-backed orchestration with no shared child-agent writes."""

    def __init__(self, root: os.PathLike[str] | str):
        self.root = Path(root).resolve()
        self.jobs_dir = self.root / "jobs"
        self.batches_dir = self.root / "batches"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.batches_dir.mkdir(parents=True, exist_ok=True)

    @serialized
    def prepare_paper(
        self,
        paper_id: str,
        pdf_path: os.PathLike[str] | str | None = None,
        *,
        zotero_key: str | None = None,
    ) -> dict[str, Any]:
        """Create an idempotent single-paper job and its isolated directories."""

        identifier = _validate_paper_id(paper_id)
        stable_zotero_key = _validate_paper_id(zotero_key or identifier)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", stable_zotero_key) or stable_zotero_key.endswith(".") or stable_zotero_key.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *[f"COM{n}" for n in range(1, 10)], *[f"LPT{n}" for n in range(1, 10)]}:
            if zotero_key is not None:
                raise ValidationError("source key must be a safe single file identifier")
            stable_zotero_key = "source-" + hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:16]
        pdf = _validate_single_pdf(pdf_path)
        existing = self._find_by_paper_id(identifier)
        if existing is not None:
            existing_pdf = existing.get("pdf_path")
            if pdf is not None and existing_pdf not in (None, str(pdf)):
                raise ValidationError("该论文已有任务且绑定了不同PDF")
            return existing

        job_id = f"paper-{hashlib.sha256(identifier.encode('utf-8')).hexdigest()[:16]}"
        job_dir = self.jobs_dir / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "agent-output"
        reviews_dir = job_dir / "human_reviews"
        for directory in (input_dir, output_dir, reviews_dir):
            directory.mkdir(parents=True, exist_ok=False)

        state = STATE_CANDIDATE
        history = [{"state": state, "at": _now(), "reason": "prepared"}]
        if pdf is not None:
            state = STATE_SOURCE_READY
            history.extend(
                [
                    {
                        "state": STATE_IDENTITY_VERIFIED,
                        "at": _now(),
                        "reason": "single PDF supplied",
                    },
                    {
                        "state": STATE_SOURCE_READY,
                        "at": _now(),
                        "reason": "single PDF supplied",
                    },
                ]
            )
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "job_id": job_id,
            "paper_id": identifier,
            "zotero_key": stable_zotero_key,
            "state": state,
            "pdf_path": str(pdf) if pdf else None,
            "pdf_sha256": sha256_file(pdf) if pdf else None,
            "ai_note_path": None,
            "ai_note_sha256": None,
            "citation_permission": {
                "allowed": False,
                "valid": False,
                "approved_claims": [],
                "invalidated_reason": "尚未完成人工确认",
            },
            "active_batch_id": None,
            "job_dir": str(job_dir),
            "agent_output_dir": str(output_dir),
            "created_at": _now(),
            "updated_at": _now(),
            "history": history,
        }
        self._save_job(record)
        return record

    @serialized
    def verify_identity(self, job_id: str) -> dict[str, Any]:
        return self.transition(job_id, STATE_IDENTITY_VERIFIED, "identity verified")

    @serialized
    def attach_pdf(
        self, job_id: str, pdf_path: os.PathLike[str] | str
    ) -> dict[str, Any]:
        pdf = _validate_single_pdf(pdf_path)
        assert pdf is not None
        job = self.get_job(job_id)
        if job["state"] == STATE_CANDIDATE:
            job = self.transition(job_id, STATE_IDENTITY_VERIFIED, "identity verified")
        if job["state"] != STATE_IDENTITY_VERIFIED:
            raise StateTransitionError("仅identity_verified任务可以绑定初始PDF")
        job["pdf_path"] = str(pdf)
        job["pdf_sha256"] = sha256_file(pdf)
        self._append_state(job, STATE_SOURCE_READY, "single PDF attached")
        self._save_job(job)
        return job

    @serialized
    def start_reading(self, job_id: str) -> dict[str, Any]:
        return self.transition(job_id, STATE_READING, "single-paper reading started")

    @serialized
    def attach_ai_note(
        self, job_id: str, note_path: os.PathLike[str] | str
    ) -> dict[str, Any]:
        note = Path(note_path).expanduser().resolve()
        if not note.is_file():
            raise ValidationError(f"AI笔记不存在：{note}")
        job = self.get_job(job_id)
        if job["state"] != STATE_READING:
            raise StateTransitionError("只有reading任务可以提交AI笔记")
        if job.get("reader_attempt"):
            raise ValidationError("fenced reader tasks must publish through Workflow.submit")
        job["ai_note_path"] = str(note)
        job["ai_note_sha256"] = sha256_file(note)
        self._append_state(job, STATE_AI_NOTE_READY, "AI note attached")
        self._save_job(job)
        return job

    @serialized
    def begin_human_review(self, job_id: str) -> dict[str, Any]:
        return self.transition(job_id, STATE_HUMAN_REVIEWING, "human review opened")

    @serialized
    def transition(
        self, job_id: str, target_state: str, reason: str = "manual transition"
    ) -> dict[str, Any]:
        if target_state not in ALL_STATES:
            raise StateTransitionError(f"未知状态：{target_state}")
        job = self.get_job(job_id)
        allowed = _TRANSITIONS.get(job["state"], frozenset())
        if target_state not in allowed:
            raise StateTransitionError(
                f"非法状态转换：{job['state']} -> {target_state}"
            )
        self._append_state(job, target_state, reason)
        if target_state in {STATE_RESCINDED, STATE_EXCLUDED}:
            self._invalidate_permission(job, f"状态变更为{target_state}")
        self._save_job(job)
        self._refresh_batches_for_job(job_id)
        return job

    @serialized
    def dispatch_reading_batch(
        self,
        job_ids: Sequence[str],
        allowed_skills: Mapping[str, Sequence[str]] | None = None,
    ) -> dict[str, Any]:
        """Create up to three one-paper agent manifests with isolated writes."""

        if isinstance(job_ids, (str, bytes)):
            raise ValidationError("job_ids必须是任务列表，而不是字符串")
        requested = list(job_ids)
        if not requested:
            raise ValidationError("阅读批次至少需要一个任务")
        if len(requested) > 3:
            raise BatchGateError("每个阅读批次最多三篇论文")
        if len(set(requested)) != len(requested):
            raise BatchGateError("同一论文任务不能在一个批次中重复领取")
        if allowed_skills is not None and not isinstance(allowed_skills, Mapping):
            raise ValidationError("allowed_skills必须按job_id映射Skill列表")
        self._assert_no_open_batch()

        jobs = [self.get_job(job_id) for job_id in requested]
        paper_ids = [job["paper_id"] for job in jobs]
        if len(set(paper_ids)) != len(paper_ids):
            raise BatchGateError("同一论文不能分配给两个主读代理")
        for job in jobs:
            if job["state"] != STATE_SOURCE_READY:
                raise BatchGateError(
                    f"任务{job['job_id']}处于{job['state']}，不能进入阅读批次"
                )
            if job.get("active_batch_id"):
                raise BatchGateError(f"任务{job['job_id']}已被其他批次领取")
            selected_skills = list((allowed_skills or {}).get(job["job_id"], []))
            if any(
                not isinstance(name, str) or not name.strip()
                for name in selected_skills
            ):
                raise ValidationError("allowed_skills只能包含非空Skill名称")

        batch_id = f"batch-{uuid.uuid4().hex[:12]}"
        manifests: list[dict[str, Any]] = []
        output_dirs: set[str] = set()
        for job in jobs:
            selected_skills = list((allowed_skills or {}).get(job["job_id"], []))
            output_dir = str(Path(job["agent_output_dir"]).resolve())
            if output_dir in output_dirs:
                raise BatchGateError("子代理输出目录发生冲突")
            output_dirs.add(output_dir)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "batch_id": batch_id,
                "job_id": job["job_id"],
                "paper_id": job["paper_id"],
                "zotero_key": job["zotero_key"],
                "pdf_path": job["pdf_path"],
                "pdf_sha256": job["pdf_sha256"],
                "allowed_skills": selected_skills,
                "read_only_sources": [job["pdf_path"]],
                "exclusive_output_dir": output_dir,
                "temporary_output_dir": str(Path(output_dir) / "work-tmp"),
                "write_policy": "exclusive_output_only",
                "forbidden_writes": [
                    "Zotero",
                    "README",
                    "cross-paper-matrix",
                    "knowledge-graph",
                    "manuscript",
                    "other-agent-job-directories",
                    "project-level-tmp",
                ],
                "paper_limit": 1,
                "worker_protocol": {
                    "skill": "evidence-loop",
                    "engines": ["external-agent", "openai-compatible"],
                    "subagent_required": False,
                    "prepare_command": "python -m evidence_loop issue",
                    "register_command": "python -m evidence_loop submit",
                },
                "reader_policy": {
                    "primary_text": "source_grounded_structured_summary",
                    "facets": [
                        "overview",
                        "method",
                        "result",
                        "limitations",
                        "review_value",
                    ],
                    "source_mapping": "page_and_figure_anchoring",
                    "forbid_full_text_reproduction": True,
                    "forbid_full_parallel_translation": True,
                },
            }
            manifest_path = Path(job["job_dir"]) / "agent-task.json"
            _atomic_json(manifest_path, manifest)
            manifests.append({**manifest, "manifest_path": str(manifest_path)})

            job["active_batch_id"] = batch_id
            self._append_state(job, STATE_READING, f"dispatched in {batch_id}")
            self._save_job(job)

        batch = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": batch_id,
            "status": "open",
            "job_ids": requested,
            "created_at": _now(),
            "completed_at": None,
            "agent_tasks": manifests,
        }
        _atomic_json(self._batch_path(batch_id), batch)
        return batch

    def review_mode(self) -> str:
        """The workspace opts in; an absent policy retains the human gate."""
        path = self.root.parent / "review-project.json"
        policy = _load_json(path).get("review_policy", {}) if path.is_file() else {}
        if not isinstance(policy, dict):
            raise ValidationError("review_policy must be an object")
        mode = policy.get("mode", "human_required")
        if mode not in {"human_required", "ai_review_with_optional_human"}:
            raise ValidationError("Unsupported review_policy.mode")
        return mode

    def ai_review_bindings(self, job_id: str) -> dict[str, Any]:
        """Return current inputs for a separate, substantive verification report."""
        job = self.get_job(job_id)
        output = Path(job["agent_output_dir"])
        relatives = set(job.get("reading_receipt", {}).get("artifacts", {}))
        relatives.update({"review-sections.json", "paper-review.html"})
        if (output / "reading-result.json").is_file():
            relatives.add("reading-result.json")
        artifacts = {}
        for relative in sorted(relatives):
            path = (output / relative).resolve()
            if not path.is_relative_to(output.resolve()):
                raise ValidationError("AI review artifact must stay inside agent-output")
            artifacts[relative] = sha256_file(path)
        archive = job.get("citation_permission", {}).get("review_archive")
        return {**{key: job[key] for key in ("job_id", "paper_id", "zotero_key")},
                "pdf_sha256": sha256_file(job["pdf_path"]),
                "ai_note_sha256": sha256_file(job["ai_note_path"]),
                "artifact_sha256": artifacts,
                "human_review_sha256": sha256_file(archive) if archive else None}

    _QA_PASSED = {"passed", "verified", "ok"}

    @staticmethod
    def visual_evidence_report(job: Mapping[str, Any]) -> dict[str, Any]:
        """Objectively checkable prerequisites for registering AI verification.

        A text-only page with pending visual QA must not silently become
        ``ai_verified``.  Only a human-signed waiver in ``job.json`` may bypass
        the findings, so the exception is visible rather than implicit.
        """

        output = Path(job.get("agent_output_dir") or "")
        decisions: list[Mapping[str, Any]] = []
        result_path = output / "reading-result.json"
        if result_path.is_file():
            try:
                result = _load_json(result_path)
                decisions = [
                    item for item in (result.get("figure_decisions") or [])
                    if isinstance(item, Mapping)
                ]
            except (OSError, ValueError):
                decisions = []
        expected = sum(
            1 for item in decisions if str(item.get("decision") or "include") != "omit"
        )
        assets = output / "reader" / "assets"
        crops = (
            [
                path.name for path in sorted(assets.iterdir())
                if re.fullmatch(r"p\d+_.+\.(?:png|jpg|jpeg)", path.name, re.IGNORECASE)
            ]
            if assets.is_dir()
            else []
        )
        receipt = job.get("reading_receipt") or {}
        visual_review = bool(receipt.get("visual_review"))
        attempt = (job.get("reader_attempt") or {}).get("id")
        qa_status: str | None = None
        qa_path = output / "runs" / str(attempt) / "visual-audit.json" if attempt else None
        if qa_path is not None and qa_path.is_file():
            try:
                audit = _load_json(qa_path)
                block = audit.get("html_visual_qa") if isinstance(audit, Mapping) else None
                if isinstance(block, Mapping):
                    qa_status = str(block.get("status") or "") or None
                elif isinstance(block, str):
                    qa_status = block
            except (OSError, ValueError):
                qa_status = None
        problems: list[str] = []
        if expected and not crops:
            problems.append(f"{expected} 条「纳入证据」的图表决策没有任何单图裁图")
        if not visual_review:
            problems.append("reading_receipt.visual_review 为 False")
        if qa_status not in ReviewOrchestrator._QA_PASSED:
            problems.append(f"html_visual_qa 未通过（status={qa_status!r}）")
        return {
            "figure_decisions": len(decisions),
            "expected_crops": expected,
            "available_crops": len(crops),
            "visual_review": visual_review,
            "html_visual_qa": qa_status,
            "problems": problems,
        }

    @staticmethod
    def has_visual_qa_waiver(job: Mapping[str, Any]) -> bool:
        waiver = job.get("visual_qa_waiver")
        return isinstance(waiver, Mapping) and all(
            str(waiver.get(key) or "").strip()
            for key in ("approved_by", "reason", "approved_at")
        )

    @staticmethod
    def validate_ai_review(review: Mapping[str, Any]) -> None:
        """AI findings are not answers to, or approval from, a human review."""
        fields = {"schema_version", "job_id", "paper_id", "zotero_key", "pdf_sha256",
                  "ai_note_sha256", "artifact_sha256", "human_review_sha256",
                  "human_review_response", "reviewer", "reviewed_at", "sections", "verified_claims"}
        if set(review) != fields or review.get("schema_version") != SCHEMA_VERSION:
            raise ValidationError("AI review fields/schema_version do not match the contract")
        def text(value, label):
            if not isinstance(value, str) or not value.strip():
                raise ValidationError(f"AI review {label} must be non-empty text")
        def anchors(value):
            if not isinstance(value, list) or not value:
                raise ValidationError("AI review requires source_anchors")
            for anchor in value:
                text(anchor, "source anchor")
        for key in ("job_id", "paper_id", "zotero_key", "reviewer", "reviewed_at"):
            text(review[key], key)
        for key in ("pdf_sha256", "ai_note_sha256"):
            _validate_sha256(review[key], key)
        _validate_sha256(review["human_review_sha256"], "human_review_sha256", nullable=True)
        if not isinstance(review["human_review_response"], str):
            raise ValidationError("human_review_response must be text")
        if review["human_review_sha256"]:
            text(review["human_review_response"], "response to prior human findings")
        try:
            stamp = datetime.fromisoformat(review["reviewed_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError
        except ValueError as exc:
            raise ValidationError("reviewed_at must include a timezone") from exc
        artifacts = review["artifact_sha256"]
        if not isinstance(artifacts, dict) or not artifacts:
            raise ValidationError("AI review requires artifact_sha256")
        for relative, digest in artifacts.items():
            text(relative, "artifact path")
            _validate_sha256(digest, relative)
        sections = review["sections"]
        if not isinstance(sections, list) or len(sections) != 8:
            raise ValidationError("AI review requires all eight sections")
        ids = []
        for section in sections:
            if not isinstance(section, dict) or set(section) != {"section_id", "verdict", "notes", "source_anchors"}:
                raise ValidationError("Invalid AI review section")
            if type(section["section_id"]) is not int or section["verdict"] not in {"verified", "qualified"}:
                raise ValidationError("AI review section has unresolved findings")
            ids.append(section["section_id"])
            text(section["notes"], "section findings")
            anchors(section["source_anchors"])
        if set(ids) != set(range(1, 9)):
            raise ValidationError("AI review requires distinct sections 1–8")
        claims = review["verified_claims"]
        if not isinstance(claims, list) or not claims:
            raise ValidationError("AI review requires at least one verified claim")
        ids = []
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {"claim_id", "text", "source_anchors", "limitations"}:
                raise ValidationError("Invalid AI verified claim")
            for key in ("claim_id", "text", "limitations"):
                text(claim[key], key)
            anchors(claim["source_anchors"])
            ids.append(claim["claim_id"])
        if len(ids) != len(set(ids)):
            raise ValidationError("AI verified claim IDs must be unique")

    @serialized
    def import_ai_review(self, job_id: str, review_path: os.PathLike[str] | str) -> dict[str, Any]:
        if self.review_mode() != "ai_review_with_optional_human":
            raise ValidationError("AI verification requires ai_review_with_optional_human")
        review = _load_json(Path(review_path))
        self.validate_ai_review(review)
        job = self.refresh_artifacts(job_id)
        if job["state"] not in {STATE_AI_NOTE_READY, STATE_HUMAN_REVIEWING}:
            raise StateTransitionError("Only a completed reading awaiting review can be AI verified")
        if job.get("reading_receipt_invalid"):
            raise StaleReviewError("Reading artifacts changed; reopen and submit a new reading")
        gate = self.visual_evidence_report(job)
        waived = self.has_visual_qa_waiver(job)
        if gate["problems"] and not waived:
            raise ValidationError(
                "AI 核验前置条件不满足：" + "；".join(gate["problems"])
                + "。如确需豁免，须在 job.json 写入带 approved_by / reason / approved_at "
                "的 visual_qa_waiver。"
            )
        bindings = self.ai_review_bindings(job_id)
        if any(review[key] != value for key, value in bindings.items()):
            raise StaleReviewError("AI review identity/source/artifact bindings do not match")
        digest = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        archive = Path(job["job_dir"]) / "ai_reviews" / "archive" / f"{digest}.json"
        if not archive.exists():
            _atomic_json(archive, review)
        job["ai_citation_permission"] = {
            "allowed": True, "valid": True, "verification_origin": "ai",
            "bound_pdf_sha256": job["pdf_sha256"], "bound_ai_note_sha256": job["ai_note_sha256"],
            "bound_artifacts": bindings["artifact_sha256"],
            "bound_human_review_sha256": bindings["human_review_sha256"],
            "review_archive": str(archive), "review_sha256": sha256_file(archive),
            "verified_claim_ids": [claim["claim_id"] for claim in review["verified_claims"]],
            "invalidated_reason": None,
        }
        job["ai_verification_gate"] = {**gate, "waived": waived, "checked_at": _now()}
        self._append_state(job, STATE_AI_VERIFIED, "source-bound AI verification; human review remains available")
        self._save_job(job)
        self._refresh_batches_for_job(job_id)
        return self.get_job(job_id)

    def ai_permission_valid(self, job: Mapping[str, Any]) -> bool:
        """Call after refresh_artifacts so byte changes cannot retain permission."""
        permission = job.get("ai_citation_permission", {})
        return bool(self.review_mode() == "ai_review_with_optional_human"
                    and job["state"] == STATE_AI_VERIFIED
                    and permission.get("allowed") and permission.get("valid")
                    and permission.get("bound_pdf_sha256") == job.get("pdf_sha256")
                    and permission.get("bound_ai_note_sha256") == job.get("ai_note_sha256"))

    @staticmethod
    def _view_rebind_explains_page_change(
        job: Mapping[str, Any], review: Mapping[str, Any], bindings: Mapping[str, Any]
    ) -> bool:
        """Allow only a fully recorded view-layer re-render to move the page hash.

        ``rerender.py --rebind-ai-view`` writes ``view_rebind_log`` entries that
        chain the archived page hash to the current one.  The archived AI review
        is deliberately left untouched (it *is* the verification evidence), so
        ``ai_review_content`` has to recognise that one derived artifact moved.
        Every other artifact must still match byte for byte, and the log chain
        must be continuous from the archived hash to the on-disk page.
        """

        logs = job.get("view_rebind_log") or []
        if not logs:
            return False
        archived = review.get("artifact_sha256") or {}
        current = bindings.get("artifact_sha256") or {}
        if set(archived) != set(current):
            return False
        if any(
            archived[key] != current[key]
            for key in archived
            if key != "paper-review.html"
        ):
            return False
        expected = archived.get("paper-review.html")
        for entry in logs:
            if not isinstance(entry, Mapping) or entry.get("artifact") != "paper-review.html":
                return False
            if entry.get("sha_before") != expected:
                return False
            expected = entry.get("sha_after")
            if not expected:
                return False
        return expected == current.get("paper-review.html")

    def ai_review_content(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Read bound candidate content even after a human supersedes AI permission."""
        permission = job.get("ai_citation_permission", {})
        archive = Path(permission.get("review_archive") or "")
        if not archive.is_file() or sha256_file(archive) != permission.get("review_sha256"):
            raise ValidationError("AI review content archive missing or changed")
        review = _load_json(archive)
        self.validate_ai_review(review)
        bindings = self.ai_review_bindings(job["job_id"])
        mismatched = [
            key for key, value in bindings.items()
            if key != "human_review_sha256" and review.get(key) != value
        ]
        if mismatched and not (
            mismatched == ["artifact_sha256"]
            and self._view_rebind_explains_page_change(job, review, bindings)
        ):
            raise StaleReviewError("AI review content no longer matches current reading")
        return review

    @serialized
    def import_human_review(
        self, review_path: os.PathLike[str] | str
    ) -> dict[str, Any]:
        """Validate, archive, and apply a human review bound to current hashes."""

        source = Path(review_path).expanduser().resolve()
        review = self._normalize_review_payload(_load_json(source))
        self.validate_human_review(review)
        job = self.get_job(review["job_id"])
        if review["paper_id"] != job["paper_id"]:
            raise ValidationError("人工评审paper_id与任务不一致")
        if review["zotero_key"] != job["zotero_key"]:
            raise ValidationError("人工评审zotero_key与任务不一致")
        if job["state"] not in {
            STATE_AI_VERIFIED,
            STATE_AI_NOTE_READY,
            STATE_HUMAN_REVIEWING,
            STATE_HUMAN_CONFIRMED,
            STATE_NEEDS_CLARIFICATION,
            STATE_INDEXED,
            STATE_SYNTHESIS_ELIGIBLE,
        }:
            raise StateTransitionError(f"当前状态{job['state']}不能导入人工评审")

        job = self.refresh_artifacts(job["job_id"])
        if job.get("reading_receipt_invalid"):
            raise StaleReviewError("阅读产物已改变，请重新生成并重新评审")
        if review["pdf_sha256"] != job.get("pdf_sha256"):
            raise StaleReviewError("PDF哈希已变化，旧人工评审失效")
        if review["ai_note_sha256"] != job.get("ai_note_sha256"):
            raise StaleReviewError("AI笔记哈希已变化，旧人工评审失效")

        outcome = review["review_outcome"]
        sections = review["sections"]
        section_ids = {item["section_id"] for item in sections}
        unable = any(item["verdict"] == "unable_to_judge" for item in sections)
        if outcome == STATE_HUMAN_CONFIRMED:
            if section_ids != set(range(1, 9)) or unable:
                raise ValidationError(
                    "human_confirmed要求八节全部完成且不能包含unable_to_judge"
                )
        permission = review["citation_permission"]
        if permission["allowed"] and outcome != STATE_HUMAN_CONFIRMED:
            raise ValidationError("只有human_confirmed评审可以授予引用许可")
        uses_ai_content = bool(
            job["state"] == STATE_AI_VERIFIED
            or job.get("citation_permission", {}).get("content_origin") == "ai_verification")
        expected_ai_hash = job.get("ai_citation_permission", {}).get("review_sha256") if uses_ai_content else None
        if review.get("ai_review_sha256") != expected_ai_hash:
            raise StaleReviewError("AI verification content changed; reopen the review page before judging it")
        if uses_ai_content and permission["allowed"]:
            self.ai_review_content(job)

        archived = dict(review)
        archived["imported_at"] = _now()
        review_digest = hashlib.sha256(
            json.dumps(review, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        archive_path = (
            Path(job["job_dir"]) / "human_reviews" / "archive" / f"{review_digest}.json"
        )
        if not archive_path.exists():
            _atomic_json(archive_path, archived)

        if job.get("ai_citation_permission"):
            job["ai_citation_permission"]["valid"] = False
            job["ai_citation_permission"]["invalidated_reason"] = "superseded by human review"
        job["citation_permission"] = {
            "allowed": bool(permission["allowed"]),
            "valid": bool(permission["allowed"] and outcome == STATE_HUMAN_CONFIRMED),
            "approved_claims": list(permission["approved_claims"]),
            "notes": permission.get("notes", ""),
            "bound_pdf_sha256": job["pdf_sha256"],
            "bound_ai_note_sha256": job["ai_note_sha256"],
            "review_archive": str(archive_path),
            "invalidated_reason": None,
        }
        if uses_ai_content:
            job["citation_permission"]["content_origin"] = "ai_verification"
            job["citation_permission"]["bound_ai_review_sha256"] = expected_ai_hash
        current = job["state"]
        if current in {STATE_HUMAN_CONFIRMED, STATE_INDEXED, STATE_SYNTHESIS_ELIGIBLE}:
            if outcome != STATE_HUMAN_CONFIRMED:
                if self.review_mode() != "ai_review_with_optional_human":
                    raise StateTransitionError("已入库论文只能修订为human_confirmed评审")
                self._append_state(job, outcome, "human review superseded prior decision")
                self._save_job(job)
                self._refresh_batches_for_job(job["job_id"])
                return job
            job["history"].append(
                {"state": current, "at": _now(), "reason": "human review revised"}
            )
            job["updated_at"] = _now()
            self._save_job(job)
            return job
        if outcome not in _TRANSITIONS.get(current, frozenset()):
            # Import is also allowed immediately after ai_note_ready; it implies
            # opening the human-review checkpoint before applying the outcome.
            if current == STATE_AI_NOTE_READY:
                self._append_state(job, STATE_HUMAN_REVIEWING, "review imported")
                current = STATE_HUMAN_REVIEWING
            if outcome not in _TRANSITIONS.get(current, frozenset()):
                raise StateTransitionError(f"人工结论{outcome}不适用于状态{current}")
        self._append_state(job, outcome, "human review imported")
        self._save_job(job)
        self._refresh_batches_for_job(job["job_id"])
        return job

    @staticmethod
    def validate_human_review(review: Mapping[str, Any]) -> None:
        """Validate the bundled JSON schema contract without third-party libs."""

        required = {
            "schema_version",
            "job_id",
            "paper_id",
            "zotero_key",
            "pdf_sha256",
            "ai_note_sha256",
            "review_outcome",
            "sections",
            "citation_permission",
            "exported_at",
        }
        missing = required - set(review)
        if missing:
            raise ValidationError(f"人工评审缺少字段：{', '.join(sorted(missing))}")
        extra = set(review) - required - {"ai_review_sha256"}
        if extra:
            raise ValidationError(f"人工评审包含未定义字段：{', '.join(sorted(extra))}")
        if review["schema_version"] != SCHEMA_VERSION:
            raise ValidationError("不支持的人工评审schema_version")
        if not isinstance(review["job_id"], str) or not review["job_id"]:
            raise ValidationError("job_id必须是非空字符串")
        _validate_paper_id(review["paper_id"])
        _validate_paper_id(review["zotero_key"])
        _validate_sha256(review["pdf_sha256"], "pdf_sha256", nullable=True)
        _validate_sha256(review["ai_note_sha256"], "ai_note_sha256")
        if "ai_review_sha256" in review:
            _validate_sha256(review["ai_review_sha256"], "ai_review_sha256")
        if review["review_outcome"] not in {
            STATE_HUMAN_CONFIRMED,
            STATE_EXCLUDED,
            STATE_NEEDS_CLARIFICATION,
            STATE_BLOCKED_SOURCE,
        }:
            raise ValidationError("review_outcome不是允许的人工结论")
        sections = review["sections"]
        if not isinstance(sections, list) or not 1 <= len(sections) <= 8:
            raise ValidationError("sections必须包含1至8节人工判断")
        seen: set[int] = set()
        for section in sections:
            if not isinstance(section, dict):
                raise ValidationError("每个section必须是对象")
            if set(("section_id", "verdict", "notes")) - set(section):
                raise ValidationError("section缺少section_id、verdict或notes")
            section_extra = set(section) - {"section_id", "verdict", "notes", "answers"}
            if section_extra:
                raise ValidationError(
                    f"section包含未定义字段：{', '.join(sorted(section_extra))}"
                )
            section_id = section["section_id"]
            if not isinstance(section_id, int) or not 1 <= section_id <= 8:
                raise ValidationError("section_id必须为1至8的整数")
            if section_id in seen:
                raise ValidationError("section_id不能重复")
            seen.add(section_id)
            if section["verdict"] not in {
                "correct",
                "partially_correct",
                "incorrect",
                "unable_to_judge",
                "skipped",
            }:
                raise ValidationError("未知的section verdict")
            if not isinstance(section["notes"], str):
                raise ValidationError("section notes必须是字符串")
            if "answers" in section and not isinstance(section["answers"], dict):
                raise ValidationError("section answers必须是对象")
        permission = review["citation_permission"]
        if not isinstance(permission, dict):
            raise ValidationError("citation_permission必须是对象")
        permission_extra = set(permission) - {"allowed", "approved_claims", "notes"}
        if permission_extra:
            raise ValidationError(
                "citation_permission包含未定义字段："
                + ", ".join(sorted(permission_extra))
            )
        if not isinstance(permission.get("allowed"), bool):
            raise ValidationError("citation_permission.allowed必须是布尔值")
        claims = permission.get("approved_claims")
        if not isinstance(claims, list) or any(
            not isinstance(claim, str) or not claim.strip() for claim in claims
        ):
            raise ValidationError("approved_claims必须是非空字符串组成的数组")
        if "notes" in permission and not isinstance(permission["notes"], str):
            raise ValidationError("citation_permission.notes必须是字符串")
        if permission["allowed"] and review["review_outcome"] != STATE_HUMAN_CONFIRMED:
            raise ValidationError("只有human_confirmed评审可以授予引用许可")
        exported_at = review["exported_at"]
        if (
            not isinstance(exported_at, str)
            or not exported_at.strip()
            or "T" not in exported_at
        ):
            raise ValidationError("exported_at必须是ISO-8601时间字符串")
        try:
            parsed_exported_at = datetime.fromisoformat(
                exported_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ValidationError("exported_at不是有效的ISO-8601时间") from exc
        if parsed_exported_at.tzinfo is None:
            raise ValidationError("exported_at必须包含时区")

    def _normalize_review_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Convert the first HTML prototype contract to the canonical schema.

        Canonical records pass through unchanged.  Compatibility never weakens
        citation approval: a legacy boolean permission still needs an explicit
        claim in the eighth section's human notes.
        """

        review = dict(payload)
        is_legacy_html = any(
            field in review for field in ("pdf_hash", "note_hash", "status")
        )
        if not is_legacy_html:
            return review

        paper_id = review.get("paper_id")
        if not isinstance(paper_id, str):
            raise ValidationError("旧HTML评审缺少单篇paper_id")
        job = self._find_by_paper_id(paper_id)
        if job is None:
            raise ValidationError("无法把旧HTML评审映射到现有单篇任务")
        review.setdefault("job_id", job["job_id"])
        review.setdefault("zotero_key", job["zotero_key"])
        review.setdefault("pdf_sha256", review.pop("pdf_hash", None))
        review.setdefault("ai_note_sha256", review.pop("note_hash", None))

        legacy_sections = review.get("sections")
        section_names = (
            "relevance",
            "question_contribution",
            "methods_data",
            "results_evidence",
            "figures_equations",
            "limitations",
            "knowledge_product",
            "synthesis_permission",
        )
        if not isinstance(legacy_sections, dict):
            raise ValidationError("旧HTML评审sections必须是按八节ID索引的对象")
        sections: list[dict[str, Any]] = []
        verdict_aliases = {
            "correct": "correct",
            "partly_correct": "partially_correct",
            "partially_correct": "partially_correct",
            "incorrect": "incorrect",
            "cannot_judge": "unable_to_judge",
            "unable_to_judge": "unable_to_judge",
        }
        for number, name in enumerate(section_names, 1):
            value = legacy_sections.get(name)
            if value is None:
                continue
            if not isinstance(value, dict):
                raise ValidationError(f"旧HTML评审的{name}节必须是对象")
            raw_verdict = value.get("verdict")
            verdict = verdict_aliases.get(raw_verdict)
            if verdict is None:
                raise ValidationError(f"旧HTML评审的{name}节缺少有效判断")
            sections.append(
                {
                    "section_id": number,
                    "verdict": verdict,
                    "notes": str(value.get("notes") or ""),
                    "answers": {"categories": list(value.get("categories") or [])},
                }
            )
        review["sections"] = sections

        critical_ids = {1, 3, 4, 6, 8}
        critical_unresolved = any(
            item["section_id"] in critical_ids
            and item["verdict"] == "unable_to_judge"
            for item in sections
        )
        relevance = legacy_sections.get("relevance") or {}
        categories = relevance.get("categories") or []
        legacy_status = review.pop("status", None)
        if "排除" in categories:
            outcome = STATE_EXCLUDED
        elif legacy_status != "complete" or critical_unresolved:
            outcome = STATE_NEEDS_CLARIFICATION
        else:
            outcome = STATE_HUMAN_CONFIRMED
        review.setdefault("review_outcome", outcome)

        legacy_permission = review.get("citation_permission")
        if isinstance(legacy_permission, bool):
            section_eight = legacy_sections.get("synthesis_permission") or {}
            claim = str(section_eight.get("notes") or "").strip()
            review["citation_permission"] = {
                "allowed": legacy_permission,
                "approved_claims": [claim] if legacy_permission and claim else [],
                "notes": "由旧版HTML评审格式归一化",
            }
        return review

    @serialized
    def refresh_artifacts(self, job_id: str) -> dict[str, Any]:
        """Rehash tracked artifacts and invalidate approval on any change."""

        job = self.get_job(job_id)
        changes: list[str] = []
        for path_field, hash_field, label in (
            ("pdf_path", "pdf_sha256", "PDF"),
            ("ai_note_path", "ai_note_sha256", "AI笔记"),
        ):
            path = job.get(path_field)
            if not path:
                continue
            if not Path(path).is_file():
                changes.append(f"{label}不可用")
                continue
            current_hash = sha256_file(path)
            if job.get(hash_field) != current_hash:
                job[hash_field] = current_hash
                changes.append(label)
        for relative, expected in job.get("reading_receipt", {}).get("artifacts", {}).items():
            artifact = Path(job["agent_output_dir"]) / relative
            if not artifact.is_file() or sha256_file(artifact) != expected:
                changes.append(f"阅读产物 {relative}")
                job["reading_receipt_invalid"] = True
        ai_permission = job.get("ai_citation_permission", {})
        human_permission = job.get("citation_permission", {})
        if ai_permission.get("valid") or (human_permission.get("valid") and human_permission.get("content_origin") == "ai_verification"):
            for relative, expected in ai_permission.get("bound_artifacts", {}).items():
                artifact = Path(job["agent_output_dir"]) / relative
                if not artifact.is_file() or sha256_file(artifact) != expected:
                    changes.append(f"AI核验产物 {relative}")
                    job["reading_receipt_invalid"] = True
            archive = Path(ai_permission.get("review_archive") or "")
            if not archive.is_file() or sha256_file(archive) != ai_permission.get("review_sha256"):
                changes.append("AI核验归档")
            human_archive = job.get("citation_permission", {}).get("review_archive")
            bound_human = ai_permission.get("bound_human_review_sha256")
            if ai_permission.get("valid") and ((human_archive and (not Path(human_archive).is_file() or sha256_file(human_archive) != bound_human)) or (bound_human and not human_archive)):
                changes.append("人工意见归档")
        if changes:
            reason = f"{'、'.join(changes)}哈希发生变化"
            self._invalidate_permission(job, reason)
            if job["state"] in {
                STATE_AI_VERIFIED,
                STATE_HUMAN_CONFIRMED,
                STATE_INDEXED,
                STATE_SYNTHESIS_ELIGIBLE,
                STATE_HUMAN_REVIEWING,
                STATE_AI_NOTE_READY,
            }:
                self._append_state(job, STATE_NEEDS_CLARIFICATION, reason)
            else:
                job["updated_at"] = _now()
            self._save_job(job)
        return job

    @serialized
    def mark_indexed(self, job_id: str) -> dict[str, Any]:
        return self.transition(job_id, STATE_INDEXED, "confirmed card indexed")

    @serialized
    def mark_synthesis_eligible(self, job_id: str) -> dict[str, Any]:
        job = self.refresh_artifacts(job_id)
        permission = job.get("citation_permission", {})
        if not (permission.get("allowed") and permission.get("valid")):
            raise ValidationError("没有有效人工引用许可，不能进入综合层")
        return self.transition(
            job_id, STATE_SYNTHESIS_ELIGIBLE, "valid human permission verified"
        )

    def get_job(self, job_id: str) -> dict[str, Any]:
        path = self._job_path(job_id)
        if not path.is_file():
            raise ValidationError(f"未知任务：{job_id}")
        job = _load_json(path)
        old = str(job.get("job_dir") or "").replace("\\", "/").rstrip("/")
        directory = self.jobs_dir / job_id
        def relocated(value):
            normalized = str(value or "").replace("\\", "/")
            if old and normalized.startswith(old + "/"):
                return str(directory / normalized[len(old) + 1:])
            return value
        for name in ("ai_note_path", "pdf_path"):
            job[name] = relocated(job.get(name))
        for name in ("citation_permission", "ai_citation_permission"):
            permission = job.get(name, {})
            if permission.get("review_archive"):
                permission["review_archive"] = relocated(permission["review_archive"])
        job["job_dir"] = str(directory)
        job["agent_output_dir"] = str(directory / "agent-output")
        return job

    @serialized
    def get_batch(self, batch_id: str) -> dict[str, Any]:
        path = self._batch_path(batch_id)
        if not path.is_file():
            raise ValidationError(f"未知批次：{batch_id}")
        batch = _load_json(path)
        self._refresh_batch(batch)
        return _load_json(path)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.get_job(path.parent.name) for path in sorted(self.jobs_dir.glob("*/job.json"))]

    def _find_by_paper_id(self, paper_id: str) -> dict[str, Any] | None:
        for job in self.list_jobs():
            if job.get("paper_id") == paper_id:
                return job
        return None

    def _append_state(self, job: dict[str, Any], state: str, reason: str) -> None:
        job["state"] = state
        job["updated_at"] = _now()
        job.setdefault("history", []).append(
            {"state": state, "at": job["updated_at"], "reason": reason}
        )

    def _invalidate_permission(self, job: dict[str, Any], reason: str) -> None:
        permission = job.setdefault("citation_permission", {})
        permission["valid"] = False
        permission["invalidated_reason"] = reason
        if job.get("ai_citation_permission"):
            job["ai_citation_permission"]["valid"] = False
            job["ai_citation_permission"]["invalidated_reason"] = reason

    def _save_job(self, job: Mapping[str, Any]) -> None:
        _atomic_json(self._job_path(str(job["job_id"])), job)

    def _job_path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not re.fullmatch(r"paper-[0-9a-f]{16}", job_id):
            raise ValidationError("非法job_id")
        return self.jobs_dir / job_id / "job.json"

    def _batch_path(self, batch_id: str) -> Path:
        if not isinstance(batch_id, str) or not re.fullmatch(
            r"batch-[0-9a-f]{12}", batch_id
        ):
            raise ValidationError("非法batch_id")
        return self.batches_dir / f"{batch_id}.json"

    def _assert_no_open_batch(self) -> None:
        # A completed batch is historical. Its stale AI evidence must still stop
        # a new batch until clarified, stopped, or explicitly reopened.
        for job in self.list_jobs():
            if job.get("ai_citation_permission") and job["state"] in {STATE_AI_VERIFIED, STATE_NEEDS_CLARIFICATION}:
                job = self.refresh_artifacts(job["job_id"])
                if not self.ai_permission_valid(job):
                    raise BatchGateError(f"任务{job['job_id']}的AI核验无效，不能启动下一批")
        for path in sorted(self.batches_dir.glob("batch-*.json")):
            batch = _load_json(path)
            self._refresh_batch(batch)
            if _load_json(path)["status"] == "open":
                raise BatchGateError(
                    f"批次{batch['batch_id']}尚未全部完成有效评审，不能启动下一批"
                )

    def _refresh_batches_for_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        batch_id = job.get("active_batch_id")
        if not batch_id:
            return
        batch = self.get_batch(batch_id)
        self._refresh_batch(batch)

    def _refresh_batch(self, batch: dict[str, Any]) -> None:
        jobs = [self.get_job(job_id) for job_id in batch["job_ids"]]
        jobs = [self.refresh_artifacts(job["job_id"]) if job["state"] == STATE_AI_VERIFIED else job for job in jobs]
        if all(job["state"] in BATCH_TERMINAL_STATES or self.ai_permission_valid(job) for job in jobs):
            if batch.get("status") != "completed":
                batch["status"] = "completed"
                batch["completed_at"] = _now()
                _atomic_json(self._batch_path(batch["batch_id"]), batch)
                for job in jobs:
                    if job.get("active_batch_id") == batch["batch_id"]:
                        job["active_batch_id"] = None
                        self._save_job(job)


__all__ = [
    "ALL_STATES",
    "BATCH_TERMINAL_STATES",
    "BatchGateError",
    "ReviewOrchestrator",
    "ReviewWorkflowError",
    "SCHEMA_VERSION",
    "StaleReviewError",
    "StateTransitionError",
    "ValidationError",
    "sha256_file",
]
