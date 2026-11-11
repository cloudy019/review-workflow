import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from review_workflow.core import ReviewOrchestrator
from review_workflow.review_server import ReviewApplication, ReviewServerError, make_server


def _ready_job(kb: Path):
    pdf = kb.parent / "one.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    workflow = ReviewOrchestrator(kb / "review-jobs")
    job = workflow.prepare_paper("doi:10.1000/one", pdf, zotero_key="ABCD1234")
    workflow.dispatch_reading_batch([job["job_id"]])
    note = Path(job["agent_output_dir"]) / "deep-paper-note.md"
    note.write_text("AI note", encoding="utf-8")
    job = workflow.attach_ai_note(job["job_id"], note)
    workflow.begin_human_review(job["job_id"])
    page = Path(job["agent_output_dir"]) / "paper-review.html"
    page.write_text("<!doctype html><title>review</title>", encoding="utf-8")
    return workflow.get_job(job["job_id"])


def _review(job, *, outcome="human_confirmed"):
    return {
        "schema_version": "1.0",
        "job_id": job["job_id"],
        "paper_id": job["paper_id"],
        "zotero_key": job["zotero_key"],
        "pdf_sha256": job["pdf_sha256"],
        "ai_note_sha256": job["ai_note_sha256"],
        "review_outcome": outcome,
        "sections": [
            {"section_id": number, "verdict": "correct", "notes": "", "answers": {}}
            for number in range(1, 9)
        ],
        "citation_permission": {
            "allowed": outcome == "human_confirmed",
            "approved_claims": ["具体主张"] if outcome == "human_confirmed" else [],
            "notes": "",
        },
        "exported_at": "2026-08-15T12:00:00+08:00",
    }


def test_application_saves_only_to_own_human_reviews(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    job = _ready_job(kb)
    app = ReviewApplication(kb, token="test-token")
    target = app.save_review(job["job_id"], _review(job))

    assert target == (
        Path(job["job_dir"]) / "human_reviews" / "ABCD1234.human-review.json"
    ).resolve()
    assert json.loads(target.read_text(encoding="utf-8"))["job_id"] == job["job_id"]

    wrong = _review(job)
    wrong["zotero_key"] = "WRONGKEY"
    with pytest.raises(ReviewServerError, match="zotero_key"):
        app.save_review(job["job_id"], wrong)


def test_application_rejects_stale_artifacts(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    job = _ready_job(kb)
    Path(job["ai_note_path"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ReviewServerError, match="已变化"):
        ReviewApplication(kb).save_review(job["job_id"], _review(job))


def test_loopback_http_requires_token_and_origin_then_saves(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    job = _ready_job(kb)
    server = make_server(kb, port=0, token="fixed-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        with urlopen(f"{base}/?token=fixed-token") as response:
            assert response.status == 200
            assert job["paper_id"].encode("utf-8") in response.read()

        body = json.dumps(_review(job)).encode("utf-8")
        request = Request(
            f"{base}/api/reviews/{job['job_id']}?token=fixed-token",
            data=body,
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urlopen(request) as response:
            result = json.loads(response.read())
        assert result["ok"] is True
        assert (Path(job["job_dir"]) / "human_reviews" / result["file_name"]).is_file()

        with urlopen(
            f"{base}/api/reviews/{job['job_id']}?token=fixed-token"
        ) as response:
            restored = json.loads(response.read())
        assert restored["ok"] is True
        assert restored["review"]["zotero_key"] == "ABCD1234"

        bad_origin = Request(
            f"{base}/api/reviews/{job['job_id']}?token=fixed-token",
            data=body,
            headers={"Content-Type": "application/json", "Origin": "https://evil.invalid"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(bad_origin)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_index_splits_reviewed_and_unreviewed_from_project_ledger(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    unread = kb / "papers" / "UNREAD01" / "paper.json"
    unread.parent.mkdir(parents=True)
    unread.write_text(
        json.dumps(
            {
                "zotero_key": "UNREAD01",
                "state": "legacy_unconfirmed",
                "identity": {"title": "Unread paper"},
            }
        ),
        encoding="utf-8",
    )
    job = _ready_job(kb)
    workflow = ReviewOrchestrator(kb / "review-jobs")
    review_file = tmp_path / "review.json"
    review_file.write_text(json.dumps(_review(job)), encoding="utf-8")
    workflow.import_human_review(review_file)
    workflow.mark_indexed(job["job_id"])
    workflow.mark_synthesis_eligible(job["job_id"])

    page = ReviewApplication(kb, token="test-token").index_page().decode("utf-8")

    assert "未评审 1" in page
    assert "已评审 1" in page
    assert "Unread paper" in page
    assert 'data-group="unreviewed"' in page
