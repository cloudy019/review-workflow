import json
import tempfile
import unittest
from pathlib import Path

from evidence_loop.core import (
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


if __name__ == "__main__":
    unittest.main()
