import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import pytest

from evidence_loop.core import BatchGateError, ValidationError
from evidence_loop.protocol import example_result
from evidence_loop.review_server import ReviewApplication
from evidence_loop.workflow import Workflow


def source(tmp_path):
    path = tmp_path / "source.pdf"
    document = pymupdf.open()
    for text in ["Synthetic research example. Fig. 1. Measured response.", "Table 1. Independent validation. This is a test fixture, not a real paper."]:
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def result_for(request):
    result = example_result(request)
    result.update(title="Synthetic fixture", summary_markdown="The fixture has two pages. No scientific claim is established.")
    for section in result["sections"].values():
        section["ai_content"] = "The supplied fixture gives no experimental design beyond a page label."
    result["claims"][0]["text"] = "The fixture contains a validation label, without validation data."
    for item in result["figure_decisions"]:
        item["reason"] = "Retain original page for the human to inspect the label."
    return result


def ready(tmp_path, reader="codex"):
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path), context="Assess reusable scientific knowledge")
    request_path = flow.issue(job["job_id"], reader=reader)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    job = flow.submit(job["job_id"], request["attempt_id"], result_for(request))
    return flow, job, request


def human(job, allowed=True):
    return {"schema_version": "1.0", "job_id": job["job_id"], "paper_id": job["paper_id"],
            "zotero_key": job["zotero_key"], "pdf_sha256": job["pdf_sha256"], "ai_note_sha256": job["ai_note_sha256"],
            "review_outcome": "human_confirmed", "sections": [{"section_id": n, "verdict": "correct", "notes": "Human fixture decision", "answers": {}} for n in range(1, 9)],
            "citation_permission": {"allowed": allowed, "approved_claims": ["C1"] if allowed else [], "notes": "Explicit human permission"},
            "exported_at": "2026-01-01T00:00:00Z"}


@pytest.mark.parametrize("reader", ["codex", "workbuddy", "openai-compatible", "future-host"])
def test_hosts_share_contract_and_never_grant_human_permission(tmp_path, reader):
    flow, job, request = ready(tmp_path, reader)
    assert job["state"] == "human_reviewing"
    assert flow.knowledge() == []
    assert len(request["source_pages"]) == 2
    html = (Path(job["agent_output_dir"]) / "paper-review.html").read_text(encoding="utf-8")
    # The page must stay self-contained: source pages are no longer rendered at
    # all, and any figure crop it does show has to be embedded, not linked.
    assert "runs/" not in html
    assert "__PAGES_HTML__" not in html and "__FIGURES_HTML__" not in html
    assert 'id="citation-consent"' in html
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(job))
    assert flow.core.get_job(job["job_id"])["state"] == "human_confirmed"
    assert flow.knowledge(approved_only=True)[0]["claims"][0]["human_approved"]


def test_handoff_fences_old_reader(tmp_path):
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    old = json.loads(flow.issue(job["job_id"], reader="codex").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="already owns"):
        flow.issue(job["job_id"], reader="workbuddy")
    new = json.loads(flow.issue(job["job_id"], reader="workbuddy", handoff=True).read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="Stale"):
        flow.submit(job["job_id"], old["attempt_id"], result_for(old))
    flow.submit(job["job_id"], new["attempt_id"], result_for(new))


@pytest.mark.parametrize("artifact", ["reading-result.json", "review-sections.json", "paper-review.html"])
def test_changed_evidence_revokes_permission(tmp_path, artifact):
    flow, job, _ = ready(tmp_path)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(job))
    path = Path(job["agent_output_dir"]) / artifact
    path.write_text(path.read_text(encoding="utf-8") + "\nchanged", encoding="utf-8")
    assert flow.knowledge() == []
    assert not flow.core.get_job(job["job_id"])["citation_permission"]["valid"]


def test_moving_workspace_preserves_human_permission(tmp_path):
    flow, job, _ = ready(tmp_path)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(job))
    moved = tmp_path / "moved"
    shutil.copytree(flow.workspace, moved)
    moved_source = tmp_path / "moved-source.pdf"
    shutil.copyfile(job["pdf_path"], moved_source)
    migrated = Workflow(moved)
    migrated.relocate_source(job["job_id"], moved_source)
    assert migrated.knowledge(approved_only=True)[0]["paper_id"] == "example"
    assert str(moved) in migrated.core.get_job(job["job_id"])["ai_note_path"]


def test_human_confirmation_without_citation_consent(tmp_path):
    flow, job, _ = ready(tmp_path)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(job, allowed=False))
    assert flow.core.get_job(job["job_id"])["state"] == "human_confirmed"
    assert flow.knowledge() == []
    second = flow.add("second", Path(job["pdf_path"]))
    flow.issue(second["job_id"])


def test_open_human_batch_blocks_next_batch(tmp_path):
    flow, job, _ = ready(tmp_path)
    second = flow.add("second", job["pdf_path"])
    with pytest.raises(BatchGateError):
        flow.issue(second["job_id"])


def test_competing_hosts_only_one_claims_job(tmp_path):
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    def claim(reader):
        try:
            return flow.issue(job["job_id"], reader=reader)
        except ValidationError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(value is not None for value in pool.map(claim, ["codex", "workbuddy"])) == 1


def test_placeholder_and_human_decisions_rejected(tmp_path):
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    request = json.loads(flow.issue(job["job_id"]).read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="placeholders"):
        flow.submit(job["job_id"], request["attempt_id"], example_result(request))
    result = result_for(request)
    result["review_outcome"] = "human_confirmed"
    with pytest.raises(ValidationError, match="human decisions"):
        flow.submit(job["job_id"], request["attempt_id"], result)
    assert flow.core.get_job(job["job_id"])["state"] == "reading"


def test_source_page_image_must_still_match_pdf_extraction(tmp_path):
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    request_path = flow.issue(job["job_id"])
    request = json.loads(request_path.read_text(encoding="utf-8"))
    image = request_path.parent / "source-pages" / request["source_pages"][0]["image"]
    image.write_bytes(image.read_bytes() + b"changed")
    with pytest.raises(ValidationError, match="page image changed"):
        flow.submit(job["job_id"], request["attempt_id"], result_for(request))
    assert flow.core.get_job(job["job_id"])["state"] == "reading"


def test_model_cannot_embed_images_outside_job(tmp_path):
    from evidence_loop.html_review import _local_image_data_uri
    image = tmp_path / "outside.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    output = tmp_path / "output"
    output.mkdir()
    assert _local_image_data_uri(str(image), output) is None
