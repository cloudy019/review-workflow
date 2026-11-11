import json
import tempfile
import unittest
from pathlib import Path

from review_workflow.core import (
    BatchGateError,
    ReviewOrchestrator,
    StaleReviewError,
    StateTransitionError,
    ValidationError,
)


class ReviewCoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.workflow = ReviewOrchestrator(self.base / "workflow")

    def tearDown(self):
        self.tempdir.cleanup()

    def _pdf(self, name, content=b"%PDF-1.4\nexample"):
        path = self.base / name
        path.write_bytes(content)
        return path

    def _note(self, name="note.md", content="AI note"):
        path = self.base / name
        path.write_text(content, encoding="utf-8")
        return path

    def _ready_job(self, paper_id):
        return self.workflow.prepare_paper(paper_id, self._pdf(f"{paper_id}.pdf"))

    def _review(self, job, outcome="human_confirmed", allowed=True):
        return {
            "schema_version": "1.0",
            "job_id": job["job_id"],
            "paper_id": job["paper_id"],
            "zotero_key": job["zotero_key"],
            "pdf_sha256": job["pdf_sha256"],
            "ai_note_sha256": job["ai_note_sha256"],
            "review_outcome": outcome,
            "sections": [
                {
                    "section_id": number,
                    "verdict": "correct",
                    "notes": f"section {number}",
                    "answers": {},
                }
                for number in range(1, 9)
            ],
            "citation_permission": {
                "allowed": allowed,
                "approved_claims": ["claim-1"] if allowed else [],
                "notes": "human decision",
            },
            "exported_at": "2026-08-11T12:00:00+08:00",
        }

    def _write_review(self, data, name="review.json"):
        path = self.base / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_prepare_strict_single_paper_and_single_pdf(self):
        pdf = self._pdf("one.pdf")
        with self.assertRaises(ValidationError):
            self.workflow.prepare_paper(["A", "B"], pdf)
        with self.assertRaises(ValidationError):
            self.workflow.prepare_paper("A;B", pdf)
        with self.assertRaises(ValidationError):
            self.workflow.prepare_paper("A", [pdf, pdf])

        job = self.workflow.prepare_paper("10.1000/example", pdf)
        self.assertEqual(job["state"], "source_ready")
        self.assertTrue(Path(job["agent_output_dir"]).is_dir())
        self.assertEqual(
            job["job_id"],
            self.workflow.prepare_paper("10.1000/example", pdf)["job_id"],
        )

    def test_state_machine_rejects_skips(self):
        job = self.workflow.prepare_paper("PAPER1")
        with self.assertRaises(StateTransitionError):
            self.workflow.start_reading(job["job_id"])
        self.workflow.verify_identity(job["job_id"])
        self.workflow.attach_pdf(job["job_id"], self._pdf("p1.pdf"))
        started = self.workflow.start_reading(job["job_id"])
        self.assertEqual(started["state"], "reading")

    def test_batch_limit_gate_and_isolated_agent_manifests(self):
        jobs = [self._ready_job(f"PAPER{i}") for i in range(1, 5)]
        with self.assertRaises(BatchGateError):
            self.workflow.dispatch_reading_batch([job["job_id"] for job in jobs])

        with self.assertRaises(ValidationError):
            self.workflow.dispatch_reading_batch(
                [job["job_id"] for job in jobs[:3]],
                {jobs[2]["job_id"]: ["nature-reader", ""]},
            )
        self.assertTrue(
            all(
                self.workflow.get_job(job["job_id"])["state"] == "source_ready"
                for job in jobs[:3]
            )
        )

        batch = self.workflow.dispatch_reading_batch(
            [job["job_id"] for job in jobs[:3]],
            {jobs[0]["job_id"]: ["nature-reader", "DeepPaperNote"]},
        )
        self.assertEqual(len(batch["agent_tasks"]), 3)
        output_dirs = {task["exclusive_output_dir"] for task in batch["agent_tasks"]}
        self.assertEqual(len(output_dirs), 3)
        for task in batch["agent_tasks"]:
            self.assertEqual(task["paper_limit"], 1)
            self.assertEqual(len(task["read_only_sources"]), 1)
            self.assertEqual(task["write_policy"], "exclusive_output_only")
            self.assertTrue(
                task["temporary_output_dir"].startswith(task["exclusive_output_dir"])
            )
            self.assertEqual(
                task["reader_policy"]["primary_text"], "source_grounded_structured_summary"
            )
            self.assertTrue(task["reader_policy"]["forbid_full_text_reproduction"])
            self.assertTrue(task["reader_policy"]["forbid_full_parallel_translation"])
            self.assertIn("Zotero", task["forbidden_writes"])
            self.assertTrue(Path(task["manifest_path"]).is_file())

        with self.assertRaises(BatchGateError):
            self.workflow.dispatch_reading_batch([jobs[3]["job_id"]])

        for index, job in enumerate(jobs[:3], 1):
            note = self._note(f"note-{index}.md", f"note {index}")
            current = self.workflow.attach_ai_note(job["job_id"], note)
            review = self._review(current, outcome="excluded", allowed=False)
            self.workflow.import_human_review(
                self._write_review(review, f"review-{index}.json")
            )
        self.assertEqual(self.workflow.get_batch(batch["batch_id"])["status"], "completed")
        next_batch = self.workflow.dispatch_reading_batch([jobs[3]["job_id"]])
        self.assertEqual(next_batch["status"], "open")

    def test_review_schema_hashes_permission_and_staleness(self):
        job = self._ready_job("PAPER-HASH")
        batch = self.workflow.dispatch_reading_batch([job["job_id"]])
        current = self.workflow.attach_ai_note(job["job_id"], self._note())
        review = self._review(current)
        imported = self.workflow.import_human_review(self._write_review(review))
        self.assertEqual(imported["state"], "human_confirmed")
        self.assertTrue(imported["citation_permission"]["valid"])
        archive = Path(imported["citation_permission"]["review_archive"])
        self.assertTrue(archive.is_file())
        self.assertEqual(self.workflow.get_batch(batch["batch_id"])["status"], "completed")

        Path(imported["ai_note_path"]).write_text("changed", encoding="utf-8")
        refreshed = self.workflow.refresh_artifacts(job["job_id"])
        self.assertEqual(refreshed["state"], "needs_clarification")
        self.assertFalse(refreshed["citation_permission"]["valid"])
        with self.assertRaises(StaleReviewError):
            self.workflow.import_human_review(self._write_review(review, "old.json"))

    def test_confirmed_requires_all_sections_but_claims_wait_for_argument_package(self):
        job = self._ready_job("PAPER-SCHEMA")
        self.workflow.dispatch_reading_batch([job["job_id"]])
        current = self.workflow.attach_ai_note(job["job_id"], self._note())
        review = self._review(current)
        review["sections"] = review["sections"][:3]
        with self.assertRaises(ValidationError):
            self.workflow.import_human_review(self._write_review(review))

        review = self._review(current)
        review["citation_permission"]["approved_claims"] = []
        imported = self.workflow.import_human_review(
            self._write_review(review, "claims.json")
        )
        self.assertEqual(imported["state"], "human_confirmed")
        self.assertTrue(imported["citation_permission"]["valid"])

    def test_legacy_html_export_is_normalized_but_keeps_claim_gate(self):
        job = self.workflow.prepare_paper(
            "doi:10.1000/legacy",
            self._pdf("legacy.pdf"),
            zotero_key="ABCD1234",
        )
        self.workflow.dispatch_reading_batch([job["job_id"]])
        current = self.workflow.attach_ai_note(job["job_id"], self._note("legacy.md"))
        names = (
            "relevance",
            "question_contribution",
            "methods_data",
            "results_evidence",
            "figures_equations",
            "limitations",
            "knowledge_product",
            "synthesis_permission",
        )
        legacy = {
            "schema_version": "1.0",
            "paper_id": current["paper_id"],
            "zotero_key": current["zotero_key"],
            "pdf_hash": current["pdf_sha256"],
            "note_hash": current["ai_note_sha256"],
            "status": "complete",
            "sections": {
                name: {
                    "verdict": "correct",
                    "categories": [],
                    "notes": "允许支持的具体主张" if index == 8 else "",
                }
                for index, name in enumerate(names, 1)
            },
            "citation_permission": True,
            "exported_at": "2026-08-11T12:00:00.000Z",
        }
        imported = self.workflow.import_human_review(
            self._write_review(legacy, "legacy-review.json")
        )
        self.assertEqual(imported["state"], "human_confirmed")
        self.assertEqual(
            imported["citation_permission"]["approved_claims"],
            ["允许支持的具体主张"],
        )


class AIVerificationGateTests(unittest.TestCase):
    """AI verification must not silently pass a text-only, visually un-QA'd page."""

    JOB_ID = "paper-0000000000000001"

    def setUp(self):
        import hashlib

        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.workflow = ReviewOrchestrator(self.base / "workflow")
        (self.base / "review-project.json").write_text(
            json.dumps({"review_policy": {"mode": "ai_review_with_optional_human"}}),
            encoding="utf-8",
        )
        job_dir = self.workflow.jobs_dir / self.JOB_ID
        self.output = job_dir / "agent-output"
        self.output.mkdir(parents=True)
        self.pdf = self.base / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nexample")
        self.note = self.base / "note.md"
        self.note.write_text("AI note", encoding="utf-8")
        self.result = self.output / "reading-result.json"
        self.result.write_text(
            json.dumps({"figure_decisions": [
                {"id": "p3-f1", "decision": "include", "reason": "Fig. 1 给出流程。"}
            ]}),
            encoding="utf-8",
        )
        (self.output / "review-sections.json").write_text("{}", encoding="utf-8")
        (self.output / "paper-review.html").write_text("<html></html>", encoding="utf-8")
        attempt = "attempt-1"
        audit_dir = self.output / "runs" / attempt
        audit_dir.mkdir(parents=True)
        self.audit = audit_dir / "visual-audit.json"
        self.audit.write_text(
            json.dumps({"html_visual_qa": {"status": "pending"}}), encoding="utf-8"
        )
        digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.job = {
            "job_id": self.JOB_ID,
            "paper_id": "doi:10.1000/gate",
            "zotero_key": "GATE0001",
            "state": "ai_note_ready",
            "pdf_path": str(self.pdf),
            "ai_note_path": str(self.note),
            "pdf_sha256": digest(self.pdf),
            "ai_note_sha256": digest(self.note),
            "job_dir": str(job_dir),
            "agent_output_dir": str(self.output),
            "reader_attempt": {"id": attempt},
            "reading_receipt": {"artifacts": {}, "visual_review": False},
            "history": [],
        }
        self.job_path = job_dir / "job.json"
        self._save()

    def tearDown(self):
        self.tempdir.cleanup()

    def _save(self):
        self.job_path.write_text(json.dumps(self.job), encoding="utf-8")

    def _review_file(self):
        bindings = self.workflow.ai_review_bindings(self.JOB_ID)
        review = {
            **bindings,
            "schema_version": "1.0",
            "human_review_response": "",
            "reviewer": "AI verifier",
            "reviewed_at": "2026-11-10T00:00:00+08:00",
            "sections": [
                {"section_id": number, "verdict": "verified", "notes": f"section {number}",
                 "source_anchors": ["p. 1"]}
                for number in range(1, 9)
            ],
            "verified_claims": [
                {"claim_id": "C1", "text": "一条已核验主张", "source_anchors": ["p. 1"],
                 "limitations": "仅限本篇"}
            ],
        }
        path = self.base / "ai-review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        return path

    def test_gate_flags_missing_crops_and_pending_visual_qa(self):
        report = self.workflow.visual_evidence_report(self.job)
        self.assertEqual(report["expected_crops"], 1)
        self.assertEqual(report["available_crops"], 0)
        self.assertEqual(report["html_visual_qa"], "pending")
        self.assertEqual(len(report["problems"]), 3)

    def test_import_ai_review_refuses_to_register_without_evidence(self):
        with self.assertRaises(ValidationError) as caught:
            self.workflow.import_ai_review(self.JOB_ID, self._review_file())
        message = str(caught.exception)
        self.assertIn("AI 核验前置条件不满足", message)
        self.assertIn("没有任何单图裁图", message)
        self.assertIn("html_visual_qa 未通过", message)
        self.assertEqual(self.workflow.get_job(self.JOB_ID)["state"], "ai_note_ready")

    def test_a_signed_waiver_is_the_only_bypass_and_is_recorded(self):
        self.job["visual_qa_waiver"] = {
            "approved_by": "人工审阅者",
            "reason": "本环境无法做浏览器视觉 QA，已逐页核对 PDF 原文图",
            "approved_at": "2026-11-10T00:00:00+08:00",
        }
        self._save()
        imported = self.workflow.import_ai_review(self.JOB_ID, self._review_file())
        self.assertEqual(imported["state"], "ai_verified")
        gate = imported["ai_verification_gate"]
        self.assertTrue(gate["waived"])
        self.assertEqual(len(gate["problems"]), 3)

    def test_partial_waiver_is_not_accepted(self):
        self.job["visual_qa_waiver"] = {"approved_by": "人工审阅者"}
        self._save()
        with self.assertRaises(ValidationError):
            self.workflow.import_ai_review(self.JOB_ID, self._review_file())


class ViewRebindArchiveTests(unittest.TestCase):
    """A view-only re-render must not make an AI-verified page unreachable.

    The review server calls ``ai_review_content`` before serving the page.  An
    archived AI review still records the pre-render page hash, so a template or
    crop fix used to turn the page into ``404 AI review content no longer
    matches current reading``.  The fix recognises the ``view_rebind_log``
    chain instead of rewriting the archive (which would falsify the evidence).
    """

    JOB_ID = "paper-0000000000000002"

    def setUp(self):
        import hashlib

        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.workflow = ReviewOrchestrator(self.base / "workflow")
        (self.base / "review-project.json").write_text(
            json.dumps({"review_policy": {"mode": "ai_review_with_optional_human"}}),
            encoding="utf-8",
        )
        job_dir = self.workflow.jobs_dir / self.JOB_ID
        self.output = job_dir / "agent-output"
        self.output.mkdir(parents=True)
        self.pdf = self.base / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nexample")
        self.note = self.base / "note.md"
        self.note.write_text("AI note", encoding="utf-8")
        (self.output / "reading-result.json").write_text(
            json.dumps({"figure_decisions": []}), encoding="utf-8"
        )
        (self.output / "review-sections.json").write_text("{}", encoding="utf-8")
        self.page = self.output / "paper-review.html"
        self.page.write_text("<html>before</html>", encoding="utf-8")
        attempt = "attempt-1"
        (self.output / "runs" / attempt).mkdir(parents=True)
        digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        self.job = {
            "job_id": self.JOB_ID,
            "paper_id": "doi:10.1000/rebind",
            "zotero_key": "REBIND01",
            "state": "ai_note_ready",
            "pdf_path": str(self.pdf),
            "ai_note_path": str(self.note),
            "pdf_sha256": digest(self.pdf),
            "ai_note_sha256": digest(self.note),
            "job_dir": str(job_dir),
            "agent_output_dir": str(self.output),
            "reader_attempt": {"id": attempt},
            "reading_receipt": {"artifacts": {}, "visual_review": False},
            "visual_qa_waiver": {
                "approved_by": "人工审阅者",
                "reason": "本测试只验证绑定链，视觉 QA 另行处理",
                "approved_at": "2026-11-10T00:00:00+08:00",
            },
            "history": [],
        }
        self.job_path = job_dir / "job.json"
        self.job_path.write_text(json.dumps(self.job), encoding="utf-8")

        bindings = self.workflow.ai_review_bindings(self.JOB_ID)
        review = {
            **bindings,
            "schema_version": "1.0",
            "human_review_response": "",
            "reviewer": "AI verifier",
            "reviewed_at": "2026-11-10T00:00:00+08:00",
            "sections": [
                {"section_id": number, "verdict": "verified", "notes": f"section {number}",
                 "source_anchors": ["p. 1"]}
                for number in range(1, 9)
            ],
            "verified_claims": [
                {"claim_id": "C1", "text": "一条已核验主张", "source_anchors": ["p. 1"],
                 "limitations": "仅限本篇"}
            ],
        }
        path = self.base / "ai-review.json"
        path.write_text(json.dumps(review), encoding="utf-8")
        imported = self.workflow.import_ai_review(self.JOB_ID, path)
        self.assertEqual(imported["state"], "ai_verified")
        self.archived_sha = bindings["artifact_sha256"]["paper-review.html"]
        self.archive_sha256 = self._sha(
            imported["ai_citation_permission"]["review_archive"]
        )

    def tearDown(self):
        self.tempdir.cleanup()

    @staticmethod
    def _sha(path):
        import hashlib

        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def _job(self):
        return json.loads(self.job_path.read_text(encoding="utf-8"))

    def _content(self):
        return self.workflow.ai_review_content(self._job())

    def _rebind(self, text, forge_before=None):
        before = self._sha(self.page)
        self.page.write_text(text, encoding="utf-8")
        after = self._sha(self.page)
        job = self._job()
        job["ai_citation_permission"]["bound_artifacts"]["paper-review.html"] = after
        job.setdefault("view_rebind_log", []).append({
            "at": "2026-11-10T00:00:00+08:00",
            "artifact": "paper-review.html",
            "sha_before": forge_before if forge_before is not None else before,
            "sha_after": after,
            "reason": "视图层重渲染（模板/裁图修复）；所有 AI 核验输入逐字节未变",
        })
        self.job_path.write_text(json.dumps(job), encoding="utf-8")
        return job

    def test_unrecorded_page_change_still_blocked(self):
        self.page.write_text("<html>changed without a log</html>", encoding="utf-8")
        with self.assertRaises(StaleReviewError):
            self._content()

    def test_recorded_view_rebind_keeps_the_archive_authoritative(self):
        job = self._rebind("<html>after</html>")
        review = self._content()
        self.assertEqual(review["verified_claims"][0]["claim_id"], "C1")
        self.assertEqual(
            review["artifact_sha256"]["paper-review.html"], self.archived_sha,
            "归档必须保留核验当时的页面哈希，不能被改写",
        )
        self.assertEqual(
            self._sha(job["ai_citation_permission"]["review_archive"]),
            self.archive_sha256,
        )

    def test_broken_rebind_chain_is_rejected(self):
        self._rebind("<html>after</html>", forge_before="0" * 64)
        with self.assertRaises(StaleReviewError):
            self._content()

    def test_other_artifact_change_is_never_excused(self):
        self._rebind("<html>after</html>")
        (self.output / "review-sections.json").write_text('{"changed": true}', encoding="utf-8")
        with self.assertRaises(StaleReviewError):
            self._content()

    def test_identity_change_is_never_excused_by_a_page_log(self):
        self._rebind("<html>after</html>")
        self.pdf.write_bytes(b"%PDF-1.4\nchanged")
        with self.assertRaises(StaleReviewError):
            self._content()

    def test_two_consecutive_rebinds_form_a_valid_chain(self):
        self._rebind("<html>second</html>")
        self._rebind("<html>third</html>")
        self.assertEqual(self._content()["schema_version"], "1.0")


if __name__ == "__main__":
    unittest.main()
