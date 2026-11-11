import json
from pathlib import Path

import pytest

from evidence_loop.workflow import Workflow
from evidence_loop.core import ValidationError


def legacy_reviewed(tmp_path, approved=()):
    flow = Workflow(tmp_path / "workspace")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\nSynthetic source fixture")
    job = flow.add("doi:10.1000/legacy", pdf, source_key="ABCD1234")
    flow.start_batch([job["job_id"]])
    note = Path(job["agent_output_dir"]) / "deep-paper-note.md"
    note.write_text("Reviewed legacy fixture", encoding="utf-8")
    job = flow.core.attach_ai_note(job["job_id"], note)
    review = {
        "schema_version": "1.0", "job_id": job["job_id"], "paper_id": job["paper_id"],
        "zotero_key": job["zotero_key"], "pdf_sha256": job["pdf_sha256"],
        "ai_note_sha256": job["ai_note_sha256"], "review_outcome": "human_confirmed",
        "sections": [{"section_id": n, "verdict": "correct", "notes": "Human fixture decision", "answers": {}} for n in range(1, 9)],
        "citation_permission": {"allowed": True, "approved_claims": list(approved), "notes": "Human fixture consent"},
        "exported_at": "2026-01-01T00:00:00Z",
    }
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    job = flow.core.import_human_review(review_path)
    card = {
        "paper_id": job["paper_id"], "zotero_key": job["zotero_key"], "state": "human_confirmed",
        "pdf_hash": job["pdf_sha256"], "note_hash": job["ai_note_sha256"],
        "identity": {"title": "Legacy fixture"}, "citation_permission": job["citation_permission"],
        "confirmed_knowledge": {
            "confirmed_claims": [{"claim_id": "H1", "text": "Explicitly permitted fixture claim"}],
            "candidate_claims": [{"candidate_id": "AI-SYNTHESIS-1", "text": "Unapproved candidate", "status": "pending_argument_package_approval"}],
        },
    }
    card_path = flow.workspace / "papers" / job["zotero_key"] / "paper.json"
    card_path.parent.mkdir(parents=True)
    card_path.write_text(json.dumps(card), encoding="utf-8")
    return flow, job, card_path


def test_legacy_paper_permission_exports_candidates_without_granting_claims(tmp_path):
    flow, job, card_path = legacy_reviewed(tmp_path)
    protected = [Path(job["job_dir"]) / "job.json", card_path, Path(job["citation_permission"]["review_archive"])]
    before = {path: path.read_bytes() for path in protected}
    result = flow.knowledge()
    assert result[0]["title"] == "Legacy fixture"
    assert [claim["claim_id"] for claim in result[0]["claims"]] == ["H1", "AI-SYNTHESIS-1"]
    assert all(not claim["human_approved"] for claim in result[0]["claims"])
    assert flow.knowledge(approved_only=True)[0]["claims"] == []
    assert {path: path.read_bytes() for path in protected} == before
    assert not (Path(job["agent_output_dir"]) / "reading-result.json").exists()


@pytest.mark.parametrize("approved", ["H1", "Explicitly permitted fixture claim"])
def test_legacy_approved_only_obeys_authoritative_claim_scope(tmp_path, approved):
    flow, _, _ = legacy_reviewed(tmp_path, [approved])
    claims = flow.knowledge(approved_only=True)[0]["claims"]
    assert len(claims) == 1
    assert claims[0]["claim_id"] == "H1" and claims[0]["human_approved"]


@pytest.mark.parametrize("artifact", ["pdf_path", "ai_note_path"])
def test_legacy_changed_artifact_invalidates_permission(tmp_path, artifact):
    flow, job, _ = legacy_reviewed(tmp_path, ["H1"])
    path = Path(job[artifact])
    path.write_bytes(path.read_bytes() + b"\nchanged")
    assert flow.knowledge() == []
    assert not flow.core.get_job(job["job_id"])["citation_permission"]["valid"]


@pytest.mark.parametrize("change", ["note_hash", "bound_pdf_sha256", "revoked", "expired", "allowed", "identity"])
def test_legacy_stale_or_revoked_card_cannot_restore_job_permission(tmp_path, change):
    flow, _, card_path = legacy_reviewed(tmp_path, ["H1"])
    card = json.loads(card_path.read_text(encoding="utf-8"))
    if change == "note_hash":
        card["note_hash"] = "stale"
    elif change == "bound_pdf_sha256":
        card["citation_permission"]["bound_pdf_sha256"] = "stale"
    elif change == "revoked":
        card["citation_permission"]["revoked"] = True
    elif change == "expired":
        card["citation_permission"]["status"] = "expired"
    elif change == "allowed":
        card["citation_permission"]["allowed"] = False
    else:
        card["paper_id"] = "doi:10.1000/other"
    card_path.write_text(json.dumps(card), encoding="utf-8")
    assert flow.knowledge(approved_only=True) == []


def test_legacy_card_cannot_restore_revoked_job(tmp_path):
    flow, job, _ = legacy_reviewed(tmp_path, ["H1"])
    flow.core.transition(job["job_id"], "rescinded", "Human fixture withdrawal")
    assert flow.knowledge() == []


def test_knowledge_keeps_latest_human_correction_separate_from_ai_candidate(tmp_path):
    flow, job, card_path = legacy_reviewed(tmp_path)
    review = json.loads(Path(job["citation_permission"]["review_archive"]).read_text(encoding="utf-8"))
    review.pop("imported_at")
    review["sections"][5].update(verdict="incorrect", notes="The review contains the authors' own synthesis.")
    revised = tmp_path / "revised-review.json"
    revised.write_text(json.dumps(review), encoding="utf-8")
    flow.core.import_human_review(revised)
    card_before = card_path.read_bytes()
    result = flow.knowledge()[0]
    assert result["human_review"]["sections"][5] == review["sections"][5]
    assert result["claims"][1]["text"] == "Unapproved candidate"
    assert not result["claims"][1]["human_approved"]
    assert result["job_id"] == job["job_id"]
    assert result["ai_note_sha256"] == job["ai_note_sha256"]
    assert card_path.read_bytes() == card_before


@pytest.mark.parametrize("change", ["missing", "mismatched", "permission", "incomplete"])
def test_knowledge_reports_unavailable_human_review_context(tmp_path, change):
    flow, job, _ = legacy_reviewed(tmp_path)
    archive = Path(job["citation_permission"]["review_archive"])
    if change == "missing":
        archive.unlink()
    else:
        review = json.loads(archive.read_text(encoding="utf-8"))
        if change == "mismatched":
            review["paper_id"] = "doi:10.1000/another"
        elif change == "permission":
            review["citation_permission"]["allowed"] = False
        else:
            review["sections"].pop()
        archive.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ValidationError, match="Human review archive"):
        flow.knowledge()
