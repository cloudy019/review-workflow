"""Host-neutral reading, provenance and publication around one state store."""
from __future__ import annotations

import json
import hashlib
import re
import secrets
from pathlib import Path

from .core import ReviewOrchestrator, ValidationError, _atomic_json, sha256_file
from .html_review import EIGHT_SECTIONS, write_review_html
from .locking import store_lock
from .protocol import PROTOCOL_VERSION, example_result, validate_result
from .registry import write_review_registry


def extract_source(pdf: Path, target: Path) -> tuple[list, list]:
    """Preserve every source page, including vector figures and their captions."""
    try:
        import pymupdf
    except ImportError as exc:
        raise ValidationError('PDF support requires: pip install "evidence-loop[pdf]"') from exc
    target.mkdir(parents=True, exist_ok=True)
    pages, figures = [], []
    with pymupdf.open(pdf) as document:
        if document.needs_pass or not len(document):
            raise ValidationError("PDF is encrypted or empty")
        for index, page in enumerate(document, 1):
            text = page.get_text(sort=True)
            image = target / f"page-{index:04d}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).save(image)
            pages.append({"page": index, "text": text, "image": image.name, "image_sha256": sha256_file(image)})
            for match in re.finditer(r"(?im)^\s*((?:fig(?:ure)?\.?|table|图|表)\s*[A-Z]?\d+[.:：\s].*)", text):
                figures.append({"id": f"p{index}-f{len(figures)+1}", "page": index, "caption": match.group(1).strip()})
    if not any(page["text"].strip() for page in pages):
        raise ValidationError("No extractable text; supply OCR text or use a visual-capable reader")
    return pages, figures


class Workflow:
    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).expanduser().resolve()
        self.core = ReviewOrchestrator(self.workspace / "review-jobs")

    def add(self, paper_id: str, pdf: str | Path | None = None, *, context: str = "", source_key: str | None = None):
        with store_lock(self.core.root):
            job = self.core.prepare_paper(paper_id, pdf, zotero_key=source_key)
            if job["state"] in {"candidate", "source_ready"}:
                job.setdefault("source_provider", "local-pdf")
                self.core._save_job(job)
            if context and job["state"] in {"candidate", "source_ready"}:
                job["research_context"] = context
                self.core._save_job(job)
            write_review_registry(self.workspace)
            return job

    def start_batch(self, jobs):
        return self.core.dispatch_reading_batch(jobs)

    def issue(self, job_id: str, *, reader: str = "external-agent", handoff: bool = False):
        """Issue one fenced attempt. Hosts never own the authoritative state."""
        with store_lock(self.core.root):
            job = self.core.get_job(job_id)
            if job["state"] == "source_ready":
                # Even a single-paper request obeys the active batch gate.
                self.start_batch([job_id])
                job = self.core.get_job(job_id)
            if job["state"] != "reading":
                raise ValidationError("Only a reading job can receive a reader request")
            if job.get("reader_attempt") and not handoff:
                raise ValidationError("Reader already owns an attempt; use explicit handoff")
            pdf = Path(job["pdf_path"])
            if sha256_file(pdf) != job["pdf_sha256"]:
                raise ValidationError("Source changed; register a new source revision")
            attempt = secrets.token_hex(16)
            run = Path(job["agent_output_dir"]) / "runs" / attempt
            pages, figures = extract_source(pdf, run / "source-pages")
            context = job.get("research_context", "")
            context_path = self.workspace / "review-context.md"
            project_path = self.workspace / "review-project.json"
            if not context and context_path.is_file():
                context = context_path.read_text(encoding="utf-8")
            elif not context and project_path.is_file():
                settings = json.loads(project_path.read_text(encoding="utf-8"))
                context = json.dumps({key: settings[key] for key in ("research_question", "central_thesis", "knowledge_products", "human_gates") if key in settings}, ensure_ascii=False)
            request = {
                "schema_version": PROTOCOL_VERSION, "job_id": job_id,
                "paper_id": job["paper_id"], "pdf_sha256": job["pdf_sha256"],
                "attempt_id": attempt, "reader": reader, "paper_limit": 1,
                "pdf_path": str(pdf), "research_context": context,
                "source_pages": pages, "figure_inventory": figures,
                "figure_inventory_scope": "caption_detection; all original pages retained",
                "visual_review": False,
            }
            request["instructions"] = (Path(__file__).parent / "prompts" / "read.md").read_text(encoding="utf-8")
            request["output_example"] = example_result(request)
            path = run / "request.json"
            _atomic_json(path, request)
            job["reader_attempt"] = {"id": attempt, "reader": reader, "request_sha256": sha256_file(path)}
            self.core._save_job(job)
            return path

    def submit(self, job_id: str, attempt_id: str, result: dict):
        with store_lock(self.core.root):
            job = self.core.get_job(job_id)
            owner = job.get("reader_attempt") or {}
            if job["state"] != "reading" or owner.get("id") != attempt_id:
                raise ValidationError("Stale reader attempt; result cannot overwrite current work")
            run = Path(job["agent_output_dir"]) / "runs" / attempt_id
            request_path = run / "request.json"
            if sha256_file(request_path) != owner["request_sha256"]:
                raise ValidationError("Reading request was modified")
            request = json.loads(request_path.read_text(encoding="utf-8"))
            if sha256_file(job["pdf_path"]) != request["pdf_sha256"]:
                raise ValidationError("PDF changed while reading")
            for page in request["source_pages"]:
                expected = page.get("image_sha256")
                if expected is None:
                    # Earlier v1 requests omitted the page hash. Verify against
                    # the bound original PDF, never trust the current image.
                    import pymupdf
                    with pymupdf.open(job["pdf_path"]) as document:
                        expected = hashlib.sha256(document[page["page"] - 1].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).tobytes("png")).hexdigest()
                if sha256_file(run / "source-pages" / page["image"]) != expected:
                    raise ValidationError("Original page image changed while reading")
            validate_result(result, request)
            output = Path(job["agent_output_dir"])
            _atomic_json(run / "result.json", result)
            _atomic_json(output / "reading-result.json", result)
            note = output / "deep-paper-note.md"
            note.write_text(f"# {result['title']}\n\n{result['summary_markdown']}\n\n" +
                            "\n\n".join(f"## {s['title']}\n\n{result['sections'][s['id']]['ai_content']}\n\n来源：{result['sections'][s['id']]['source_anchors']}" for s in EIGHT_SECTIONS) +
                            "\n\n## 候选主张（待人工批准）\n\n" +
                            "\n\n".join(f"**{claim['claim_id']}** — {claim['text']}（{claim['kind']}；原文页 {claim['source_pages']}）" for claim in result["claims"]), encoding="utf-8")
            job["ai_note_path"] = str(note)
            job["ai_note_sha256"] = sha256_file(note)
            # Full original pages prevent an omitted caption detector hit from
            # removing visual evidence from the human's reading materials.
            reading = note.read_text(encoding="utf-8") + "\n\n## 原文页图\n"
            for page in request["source_pages"]:
                reading += f"\n### 第 {page['page']} 页\n\n![原文第 {page['page']} 页](runs/{attempt_id}/source-pages/{page['image']})\n"
            record = {**result, "job_id": job_id, "zotero_key": job["zotero_key"],
                      "ai_note_sha256": job["ai_note_sha256"], "reader_markdown": reading,
                      "reader_base_dir": str(output), "human_review_dir": str(Path(job["job_dir"]) / "human_reviews")}
            _atomic_json(output / "review-sections.json", {key: value for key, value in record.items() if key != "reader_markdown"})
            write_review_html(record, output / "paper-review.html")
            protected = [note, output / "reading-result.json", output / "review-sections.json", output / "paper-review.html", request_path]
            protected.extend((run / "source-pages").glob("*.png"))
            job["reading_receipt"] = {"reader": owner["reader"], "attempt_id": attempt_id,
                                      "visual_review": request["visual_review"],
                                      "artifacts": {str(path.relative_to(output)): sha256_file(path) for path in protected}}
            self.core._append_state(job, "ai_note_ready", "validated reading result")
            self.core._append_state(job, "human_reviewing", "human review opened")
            self.core._save_job(job)
            write_review_registry(self.workspace)
            return job

    def read_api(self, job_id, reader):
        request_path = self.issue(job_id, reader=reader.name)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        result = reader.generate(request)
        return self.submit(job_id, request["attempt_id"], result)

    def relocate_source(self, job_id: str, pdf: str | Path):
        with store_lock(self.core.root):
            job = self.core.get_job(job_id)
            source = Path(pdf).expanduser().resolve()
            if sha256_file(source) != job["pdf_sha256"]:
                raise ValidationError("Relocation requires identical source bytes")
            job["pdf_path"] = str(source)
            self.core._save_job(job)
            return job

    def reopen(self, job_id: str, pdf: str | Path | None = None):
        """Explicit revision preserves prior records and requires a new review."""
        with store_lock(self.core.root):
            job = self.core.get_job(job_id)
            allowed = {"needs_clarification", "human_confirmed", "indexed", "synthesis_eligible", "rescinded", "blocked_source", "excluded"}
            if self.core.review_mode() == "ai_review_with_optional_human":
                allowed.update({"human_reviewing", "ai_verified"})
            if job["state"] not in allowed:
                raise ValidationError("Only a reviewed or stopped job can be reopened")
            source = Path(pdf or job["pdf_path"]).expanduser().resolve()
            if source.suffix.lower() != ".pdf" or not source.is_file():
                raise ValidationError("A single verified PDF is required to reopen")
            _atomic_json(Path(job["job_dir"]) / "input" / f"revision-{secrets.token_hex(8)}.json", job)
            job.update(pdf_path=str(source), pdf_sha256=sha256_file(source), ai_note_path=None, ai_note_sha256=None)
            job.pop("reader_attempt", None)
            job.pop("reading_receipt", None)
            job.pop("reading_receipt_invalid", None)
            self.core._invalidate_permission(job, "explicit reading revision requires new human judgment")
            # The snapshot/archive retain the previous review's content basis;
            # a new reading must not inherit it before a new verification.
            job.get("citation_permission", {}).pop("content_origin", None)
            self.core._append_state(job, "reading" if job.get("active_batch_id") else "source_ready", "explicit reading revision")
            self.core._save_job(job)
            write_review_registry(self.workspace)
            return job

    def knowledge(self, *, approved_only: bool = False):
        """Export current evidence under the configured review policy."""
        results = []
        with store_lock(self.core.root):
            for job in self.core.list_jobs():
                job = self.core.refresh_artifacts(job["job_id"])
                if self.core.ai_permission_valid(job):
                    permission = job["ai_citation_permission"]
                    archive = Path(permission["review_archive"])
                    review = json.loads(archive.read_text(encoding="utf-8"))
                    self.core.validate_ai_review(review)
                    bindings = self.core.ai_review_bindings(job["job_id"])
                    if any(review[key] != value for key, value in bindings.items()):
                        raise ValidationError(f"AI review archive does not match {job['job_id']}")
                    record = json.loads((Path(job["agent_output_dir"]) / "review-sections.json").read_text(encoding="utf-8"))
                    entry = {**{key: job[key] for key in ("paper_id", "job_id", "zotero_key", "pdf_sha256", "ai_note_sha256")},
                             "title": record.get("title") or job["paper_id"], "verification_origin": "ai",
                             "claims": [{**claim, "human_approved": False, "ai_verified": True,
                                         "verification_origin": "ai", "citation_allowed": True}
                                        for claim in review["verified_claims"]],
                             "ai_review": {"sections": review["sections"], "reviewer": review["reviewer"],
                                           "reviewed_at": review["reviewed_at"], "review_archive": str(archive),
                                           "human_review_response": review["human_review_response"]}}
                    human_archive = job.get("citation_permission", {}).get("review_archive")
                    if human_archive:
                        human = json.loads(Path(human_archive).read_text(encoding="utf-8"))
                        entry["human_review"] = {"sections": human["sections"], "exported_at": human["exported_at"],
                                                 "review_archive": human_archive, "historical": True}
                    results.append(entry)
                    continue
                permission = job.get("citation_permission", {})
                if (job["state"] not in {"human_confirmed", "indexed", "synthesis_eligible"}
                        or not permission.get("allowed") or not permission.get("valid")
                        or permission.get("revoked") or permission.get("status") in {"revoked", "rescinded", "expired"}
                        or permission.get("bound_pdf_sha256") != job.get("pdf_sha256")
                        or permission.get("bound_ai_note_sha256") != job.get("ai_note_sha256")):
                    continue
                result_path = Path(job["agent_output_dir"]) / "reading-result.json"
                if permission.get("content_origin") == "ai_verification":
                    verified = self.core.ai_review_content(job)
                    record = json.loads((Path(job["agent_output_dir"]) / "review-sections.json").read_text(encoding="utf-8"))
                    result = {"title": record.get("title") or job["paper_id"], "claims": verified["verified_claims"]}
                elif result_path.exists():
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                else:
                    # Existing reviewed tasks predate the reader-result protocol.
                    # Their cards supply content; the job still owns permission.
                    card_path = self.workspace / "papers" / str(job["zotero_key"]) / "paper.json"
                    if job.get("reader_attempt") or job.get("reading_receipt") or not card_path.is_file():
                        continue
                    card = json.loads(card_path.read_text(encoding="utf-8"))
                    card_permission = card.get("citation_permission", {})
                    if (card.get("state") != "human_confirmed"
                            or card.get("paper_id") != job["paper_id"] or card.get("zotero_key") != job["zotero_key"]
                            or card.get("rescinded_at") or card.get("revoked_at")
                            or not isinstance(card_permission, dict)
                            or not card_permission.get("allowed") or not card_permission.get("valid")
                            or card_permission.get("revoked") or card_permission.get("status") in {"revoked", "rescinded", "expired"}
                            or card.get("pdf_hash") != job["pdf_sha256"]
                            or card.get("note_hash") != job["ai_note_sha256"]
                            or card_permission.get("bound_pdf_sha256") != job["pdf_sha256"]
                            or card_permission.get("bound_ai_note_sha256") != job["ai_note_sha256"]):
                        continue
                    payload = card.get("confirmed_knowledge") or card
                    legacy_claims = [*payload.get("confirmed_claims", []), *payload.get("candidate_claims", [])]
                    result = {
                        "title": card.get("identity", {}).get("title") or job["paper_id"],
                        "claims": [{**claim, "claim_id": claim.get("claim_id") or claim.get("candidate_id")}
                                   for claim in legacy_claims if claim.get("text") and (claim.get("claim_id") or claim.get("candidate_id"))],
                    }
                approved = permission.get("approved_claims", [])
                claims = [{**claim, "human_approved": claim["claim_id"] in approved or claim["text"] in approved,
                           "verification_origin": "human"}
                          for claim in result["claims"]]
                if approved_only:
                    claims = [claim for claim in claims if claim["human_approved"]]
                archive = Path(permission.get("review_archive") or "")
                if not archive.is_file():
                    raise ValidationError(f"Human review archive missing for {job['job_id']}")
                review = json.loads(archive.read_text(encoding="utf-8"))
                self.core.validate_human_review({key: value for key, value in review.items() if key != "imported_at"})
                if any(review.get(key) != job.get(key) for key in
                       ("job_id", "paper_id", "zotero_key", "pdf_sha256", "ai_note_sha256")):
                    raise ValidationError(f"Human review archive does not match {job['job_id']}")
                if (review["review_outcome"] != "human_confirmed"
                        or {section["section_id"] for section in review["sections"]} != set(range(1, 9))
                        or any(section["verdict"] == "unable_to_judge" for section in review["sections"])
                        or review["citation_permission"]["allowed"] != permission["allowed"]
                        or review["citation_permission"]["approved_claims"] != approved):
                    raise ValidationError(f"Human review archive conflicts with permission for {job['job_id']}")
                if permission.get("content_origin") == "ai_verification" and (
                        review.get("ai_review_sha256") != permission.get("bound_ai_review_sha256")
                        or review.get("ai_review_sha256") != job["ai_citation_permission"].get("review_sha256")):
                    raise ValidationError(f"Human review does not bind current AI claim content for {job['job_id']}")
                results.append({"paper_id": job["paper_id"], "job_id": job["job_id"],
                                "zotero_key": job["zotero_key"], "pdf_sha256": job["pdf_sha256"],
                                "ai_note_sha256": job["ai_note_sha256"],
                                "verification_origin": "human",
                                "content_origin": permission.get("content_origin", "reading"),
                                "title": result["title"], "claims": claims,
                                "human_review": {"sections": review["sections"],
                                                 "exported_at": review["exported_at"],
                                                 "review_archive": str(archive)}})
        return results
