import json
from pathlib import Path
from unittest.mock import patch

import pymupdf

from evidence_loop.protocol import example_result
from evidence_loop.workflow import Workflow
from review_workflow.framework_worker import run
from review_workflow.workbuddy_bridge import request_queue_refresh


def test_one_click_worker_owns_io_and_waits_for_human(tmp_path):
    pdf = tmp_path / "fixture.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((70, 70), "Synthetic pipeline fixture. No real scientific data.")
    document.save(pdf)
    document.close()
    kb = tmp_path / "knowledge"
    flow = Workflow(kb)
    job = flow.add("example", pdf, context="fixture")
    flow.start_batch([job["job_id"]])
    request_path, _ = request_queue_refresh(kb)
    def output(self, request):
        assert self.command[self.command.index("--tools") + 1] == ""
        assert "--dangerously-skip-permissions" not in self.command
        result = example_result(request)
        result.update(title="Synthetic fixture", summary_markdown="No scientific claim established.")
        for section in result["sections"].values():
            section["ai_content"] = "The synthetic source supplies no scientific data."
        result["claims"] = []
        return result
    with patch("review_workflow.framework_worker.CommandReader.generate", output):
        finished = run(kb, request_path)
    assert finished["status"] == "waiting_human_review"
    assert finished["model_tools_enabled"] is False
    current = flow.core.get_job(job["job_id"])
    assert current["state"] == "human_reviewing"
    assert not current["citation_permission"]["allowed"]
    with patch("review_workflow.cli._cmd_auto_start", side_effect=AssertionError("opened next batch")):
        waiting = run(kb, request_path)
    assert waiting["status"] == "waiting_human_review"


def test_old_writing_adapter_uses_current_core_permission_and_claim_text(tmp_path):
    import argparse
    from evidence_loop.review_server import ReviewApplication
    from review_workflow.cli import _cmd_import, _cmd_rebuild, _cmd_build_section
    pdf = tmp_path / "fixture.pdf"
    document = pymupdf.open()
    document.new_page().insert_text((70, 70), "Synthetic validation label. No real scientific data.")
    document.save(pdf)
    document.close()
    kb = tmp_path / "knowledge"
    flow = Workflow(kb)
    job = flow.add("example", pdf, source_key="EXAMPLE1")
    request = json.loads(flow.issue(job["job_id"]).read_text(encoding="utf-8"))
    result = example_result(request)
    result.update(title="Synthetic fixture", summary_markdown="Mock reading for adapter verification.")
    for section in result["sections"].values():
        section["ai_content"] = "The source contains a synthetic label."
    result["claims"][0]["text"] = "The source contains a synthetic validation label."
    job = flow.submit(job["job_id"], request["attempt_id"], result)
    review = {"schema_version": "1.0", **{key: job[key] for key in ("job_id", "paper_id", "zotero_key", "pdf_sha256", "ai_note_sha256")},
              "review_outcome": "human_confirmed", "sections": [{"section_id": n, "verdict": "correct", "notes": "Fixture human decision", "answers": {}} for n in range(1, 9)],
              "citation_permission": {"allowed": True, "approved_claims": ["C1"], "notes": "Explicit fixture permission"}, "exported_at": "2026-01-01T00:00:00Z"}
    path = ReviewApplication(kb).save_review(job["job_id"], review)
    _cmd_import(argparse.Namespace(kb=kb, review=path))
    card = json.loads((kb / "papers" / job["zotero_key"] / "paper.json").read_text(encoding="utf-8"))
    assert card["confirmed_claims"][0]["claim_id"] == "C1"
    assert card["confirmed_claims"][0]["text"] == result["claims"][0]["text"]
    assert _cmd_rebuild(argparse.Namespace(kb=kb))["eligible_card_count"] == 1
    pack = _cmd_build_section(argparse.Namespace(kb=kb, section_id="fixture", goal="test", output=None))
    argument = json.loads(Path(pack["argument_package"]).read_text(encoding="utf-8"))
    assert argument["evidence"][0]["source_kind"] == "source_statement"
    assert argument["evidence"][0]["source_pages"] == [1]
    artifact = Path(job["agent_output_dir"]) / "reading-result.json"
    artifact.write_text(artifact.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
    assert _cmd_rebuild(argparse.Namespace(kb=kb))["eligible_card_count"] == 0
    pack = _cmd_build_section(argparse.Namespace(kb=kb, section_id="fixture", goal="test", output=None))
    argument = json.loads(Path(pack["argument_package"]).read_text(encoding="utf-8"))
    assert argument["evidence"] == []
