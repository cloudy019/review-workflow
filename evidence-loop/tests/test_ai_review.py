import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evidence_loop.core import BatchGateError, StateTransitionError, StaleReviewError, ValidationError
from evidence_loop.review_server import ReviewApplication
from evidence_loop.workflow import Workflow
from test_workflow import human as original_human, ready, result_for


def human(job, allowed=True):
    review = original_human(job, allowed=allowed)
    if job["state"] == "ai_verified" or job.get("citation_permission", {}).get("content_origin") == "ai_verification":
        review["ai_review_sha256"] = job["ai_citation_permission"]["review_sha256"]
    return review


def enable(flow):
    (flow.workspace / "review-project.json").write_text(json.dumps({
        "review_policy": {"mode": "ai_review_with_optional_human"}
    }), encoding="utf-8")


def test_optional_reopen_retains_batch_and_snapshot(tmp_path):
    flow, job, _ = ready(tmp_path)
    with pytest.raises(ValidationError, match="reviewed or stopped"):
        flow.reopen(job["job_id"])
    enable(flow)
    reopened = flow.reopen(job["job_id"])
    assert reopened["state"] == "reading"
    assert reopened["active_batch_id"] == job["active_batch_id"]
    assert not reopened.get("reading_receipt")
    assert reopened["ai_note_sha256"] is None
    snapshots = list((Path(job["job_dir"]) / "input").glob("revision-*.json"))
    assert json.loads(snapshots[0].read_text(encoding="utf-8"))["state"] == "human_reviewing"
    assert flow.issue(job["job_id"], reader="codex").is_file()


def report_for(flow, job):
    return {"schema_version": "1.0", **flow.core.ai_review_bindings(job["job_id"]),
            "reviewer": "test-verifier", "reviewed_at": "2026-11-07T00:00:00Z",
            "human_review_response": "Prior human limitations remain applicable.",
            "sections": [{"section_id": n, "verdict": "qualified",
                          "notes": "The fixture labels a validation, but supplies no validation data.",
                          "source_anchors": ["PDF p.2, Table 1 label"]} for n in range(1, 9)],
            "verified_claims": [{"claim_id": "C1", "text": "A validation label exists in the fixture.",
                                 "source_anchors": ["PDF p.2, Table 1 label"],
                                 "limitations": "The label supplies no validation data or scientific conclusion."}]}


def verify(flow, job, report=None):
    path = flow.workspace / "ai-review.json"
    path.write_text(json.dumps(report or report_for(flow, job)), encoding="utf-8")
    return flow.core.import_ai_review(job["job_id"], path)


def test_default_policy_cannot_grant_ai_permission(tmp_path):
    flow, job, _ = ready(tmp_path)
    with pytest.raises(ValidationError, match="requires ai_review_with_optional_human"):
        verify(flow, job)
    with pytest.raises(StateTransitionError):
        flow.core.transition(job["job_id"], "ai_verified", "shortcut")
    assert flow.core.get_job(job["job_id"])["state"] == "human_reviewing"
    assert flow.knowledge(approved_only=True) == []


def test_ai_verification_releases_batch_without_human_approval(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    before = job["citation_permission"]
    verified = verify(flow, job)
    assert verified["state"] == "ai_verified"
    assert verified["citation_permission"] == before
    assert "approved_claims" not in verified["ai_citation_permission"]
    assert not list((Path(job["job_dir"]) / "human_reviews").glob("archive/*.json"))
    assert flow.core.get_batch(job["active_batch_id"])["status"] == "completed"
    assert verified["active_batch_id"] is None
    evidence = flow.knowledge(approved_only=True)[0]
    assert evidence["verification_origin"] == "ai"
    assert evidence["claims"][0]["ai_verified"]
    assert evidence["claims"][0]["human_approved"] is False
    assert evidence["claims"][0]["citation_allowed"] is True
    next_job = flow.add("next", job["pdf_path"])
    assert flow.issue(next_job["job_id"]).is_file()


@pytest.mark.parametrize("change", ["identity", "pdf", "note", "artifacts", "eight_sections", "anchor", "human_fields"])
def test_invalid_ai_reports_do_not_change_state(tmp_path, change):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    report = report_for(flow, job)
    if change == "identity":
        report["paper_id"] = "different"
    elif change in {"pdf", "note"}:
        report["pdf_sha256" if change == "pdf" else "ai_note_sha256"] = "0" * 64
    elif change == "artifacts":
        report["artifact_sha256"].pop("paper-review.html")
    elif change == "eight_sections":
        report["sections"].pop()
    elif change == "anchor":
        report["verified_claims"][0]["source_anchors"] = []
    else:
        report["citation_permission"] = {"allowed": True, "approved_claims": ["C1"]}
    with pytest.raises((ValidationError, StaleReviewError)):
        verify(flow, job, report)
    assert flow.core.get_job(job["job_id"])["state"] == "human_reviewing"


@pytest.mark.parametrize("artifact", ["pdf", "note", "reading-result.json", "review-sections.json", "paper-review.html", "archive", "request"])
def test_changed_ai_evidence_invalidates_knowledge_and_next_batch(tmp_path, artifact):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verified = verify(flow, job)
    paths = {"pdf": job["pdf_path"], "note": job["ai_note_path"],
             "archive": verified["ai_citation_permission"]["review_archive"]}
    if artifact == "request":
        relative = next(name for name in verified["ai_citation_permission"]["bound_artifacts"] if name.endswith("request.json"))
        path = Path(job["agent_output_dir"]) / relative
    else:
        path = Path(paths.get(artifact, str(Path(job["agent_output_dir"]) / artifact)))
    path.write_bytes(path.read_bytes() + b"\nchanged")
    next_job = flow.add("next", job["pdf_path"])
    # Exercise the batch gate first; knowledge() must not be needed to invalidate.
    with pytest.raises(BatchGateError, match="AI"):
        flow.issue(next_job["job_id"])
    assert flow.knowledge(approved_only=True) == []
    current = flow.core.get_job(job["job_id"])
    assert current["state"] == "needs_clarification"
    assert not current["ai_citation_permission"]["valid"]


@pytest.mark.parametrize("outcome,allowed", [("human_confirmed", True), ("human_confirmed", False),
                                           ("excluded", False), ("needs_clarification", False)])
def test_original_human_ui_overrides_ai_permission(tmp_path, outcome, allowed):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verified = verify(flow, job)
    ai_archive = Path(verified["ai_citation_permission"]["review_archive"])
    previous = ai_archive.read_bytes()
    review = human(verified, allowed=allowed)
    review["review_outcome"] = outcome
    review["sections"][5]["notes"] = "Human correction supersedes the AI finding."
    ReviewApplication(flow.workspace).save_review(job["job_id"], review)
    current = flow.core.get_job(job["job_id"])
    assert current["state"] == outcome
    assert not current["ai_citation_permission"]["valid"]
    assert ai_archive.read_bytes() == previous
    evidence = flow.knowledge(approved_only=True)
    assert bool(evidence) == allowed
    if allowed:
        assert evidence[0]["claims"][0]["human_approved"]
        assert evidence[0]["claims"][0]["text"] == report_for(flow, current)["verified_claims"][0]["text"]
        assert evidence[0]["content_origin"] == "ai_verification"
        assert evidence[0]["verification_origin"] == "human"
        assert evidence[0]["human_review"]["sections"][5]["notes"] == review["sections"][5]["notes"]
    with pytest.raises(StateTransitionError):
        verify(flow, current)


def test_ai_review_keeps_prior_human_archive_and_binds_it(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    review = human(job, allowed=False)
    review["review_outcome"] = "needs_clarification"
    ReviewApplication(flow.workspace).save_review(job["job_id"], review)
    current = flow.core.transition(job["job_id"], "human_reviewing", "clarification completed")
    archive = Path(current["citation_permission"]["review_archive"])
    previous = archive.read_bytes()
    verify(flow, current)
    assert archive.read_bytes() == previous
    assert flow.knowledge()[0]["human_review"]["historical"]
    archive.write_bytes(previous + b"\n")
    assert flow.knowledge() == []


def test_legacy_products_can_be_verified_without_a_fake_human_card(tmp_path):
    flow, job, _ = ready(tmp_path)
    # Emulate pre-protocol tasks, which have sections/HTML/note but no receipt.
    job.pop("reading_receipt")
    job.pop("reader_attempt")
    flow.core._save_job(job)
    (Path(job["agent_output_dir"]) / "reading-result.json").unlink()
    enable(flow)
    verified = verify(flow, job)
    assert flow.knowledge(approved_only=True)[0]["claims"][0]["verification_origin"] == "ai"
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(verified))
    assert flow.knowledge(approved_only=True)[0]["claims"][0]["human_approved"]
    assert flow.knowledge()[0]["content_origin"] == "ai_verification"


def test_ai_permission_survives_relocation_but_not_mode_change_or_revoke(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verify(flow, job)
    moved = tmp_path / "moved"
    shutil.copytree(flow.workspace, moved)
    migrated = Workflow(moved)
    assert migrated.knowledge()[0]["verification_origin"] == "ai"
    (moved / "review-project.json").write_text("{}", encoding="utf-8")
    assert migrated.knowledge() == []
    next_job = migrated.add("next", job["pdf_path"])
    with pytest.raises(BatchGateError):
        migrated.issue(next_job["job_id"])
    enable(migrated)
    migrated.core.transition(job["job_id"], "rescinded", "user withdrawal")
    assert migrated.knowledge() == []
    assert not migrated.core.get_job(job["job_id"])["ai_citation_permission"]["valid"]


@pytest.mark.parametrize("outcome", ["excluded", "needs_clarification"])
def test_optional_human_can_revise_a_prior_confirmation(tmp_path, outcome):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verified = verify(flow, job)
    app = ReviewApplication(flow.workspace)
    app.save_review(job["job_id"], human(verified))
    later = human(verified, allowed=False)
    later["review_outcome"] = outcome
    app.save_review(job["job_id"], later)
    assert flow.core.get_job(job["job_id"])["state"] == outcome
    assert flow.knowledge() == []


def test_review_ui_uses_verified_claim_scope_without_changing_bound_html(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    report = report_for(flow, job)
    report["verified_claims"][0]["text"] = "Narrowed <script>not executed</script> claim."
    verify(flow, job, report)
    path = Path(job["agent_output_dir"]) / "paper-review.html"
    previous = path.read_bytes()
    page = ReviewApplication(flow.workspace).review_page(job["job_id"]).decode("utf-8")
    assert "Narrowed &lt;script&gt;not executed&lt;/script&gt; claim." in page
    assert "The label supplies no validation data" in page
    assert "<script>not executed</script>" not in page
    assert path.read_bytes() == previous
    assert flow.knowledge()[0]["claims"][0]["text"] == report["verified_claims"][0]["text"]


def test_ai_content_stays_hash_protected_after_human_confirmation(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verified = verify(flow, job)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(verified))
    archive = Path(verified["ai_citation_permission"]["review_archive"])
    archive.write_bytes(archive.read_bytes() + b"\n")
    assert flow.knowledge() == []


@pytest.mark.parametrize("outcome", ["human_confirmed", "needs_clarification"])
def test_human_can_reconsider_denial_without_restoring_old_claim_text(tmp_path, outcome):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verified = verify(flow, job)
    app = ReviewApplication(flow.workspace)
    denied = human(verified, allowed=False)
    denied["review_outcome"] = outcome
    app.save_review(job["job_id"], denied)
    assert flow.knowledge() == []
    assert "A validation label exists in the fixture." in app.review_page(job["job_id"]).decode("utf-8")
    app.save_review(job["job_id"], human(verified))
    assert flow.knowledge(approved_only=True)[0]["claims"][0]["text"] == "A validation label exists in the fixture."


def test_reopen_does_not_reuse_previous_ai_content_basis(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    verified = verify(flow, job)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(verified))
    flow.reopen(job["job_id"])
    request = json.loads(flow.issue(job["job_id"]).read_text(encoding="utf-8"))
    result = result_for(request)
    result["claims"][0]["text"] = "New reading after an explicit revision."
    fresh = flow.submit(job["job_id"], request["attempt_id"], result)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(fresh))
    assert flow.knowledge(approved_only=True)[0]["claims"][0]["text"] == result["claims"][0]["text"]
    assert flow.knowledge()[0]["content_origin"] == "reading"


def test_old_human_page_cannot_approve_changed_ai_text_with_same_note_hash(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    first = verify(flow, job)
    stale = human(first)
    flow.reopen(job["job_id"])
    request = json.loads(flow.issue(job["job_id"]).read_text(encoding="utf-8"))
    fresh = flow.submit(job["job_id"], request["attempt_id"], result_for(request))
    assert fresh["ai_note_sha256"] == first["ai_note_sha256"]
    report = report_for(flow, fresh)
    report["verified_claims"][0]["text"] = "A newly corrected claim with the same identifier."
    second = verify(flow, fresh, report)
    app = ReviewApplication(flow.workspace)
    with pytest.raises(ValueError, match="content changed"):
        app.save_review(job["job_id"], stale)
    missing_binding = original_human(second)
    with pytest.raises(ValueError, match="content changed"):
        app.save_review(job["job_id"], missing_binding)
    page = app.review_page(job["job_id"]).decode("utf-8")
    assert 'draft.ai_review_sha256 || null' in page
    assert '...(paper.ai_review_sha256 ? [paper.ai_review_sha256] : [])' in page
    app.save_review(job["job_id"], human(second))
    assert flow.knowledge(approved_only=True)[0]["claims"][0]["text"] == report["verified_claims"][0]["text"]


def test_ai_cli_uses_the_same_core(tmp_path):
    flow, job, _ = ready(tmp_path)
    enable(flow)
    command = [sys.executable, "-X", "utf8", "-m", "evidence_loop", "--workspace", str(flow.workspace)]
    bindings = subprocess.run([*command, "ai-review-input", job["job_id"]], capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(bindings.stdout) == flow.core.ai_review_bindings(job["job_id"])
    path = flow.workspace / "report.json"
    path.write_text(json.dumps(report_for(flow, job)), encoding="utf-8")
    result = subprocess.run([*command, "verify-ai", job["job_id"], str(path)], capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(result.stdout)["state"] == "ai_verified"
