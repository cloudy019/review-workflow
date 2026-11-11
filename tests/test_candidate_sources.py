from pathlib import Path

import pytest

from review_workflow import candidate_sources


def test_source_verification_is_single_paper_and_hash_bound(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\nsource")
    monkeypatch.setattr(candidate_sources, "attachment_file_path", lambda key, helper=None: pdf)
    verified = candidate_sources.verify_candidate_source(
        {"paper_id": "P1", "attachment_keys": ["ATTACH01"]}
    )
    assert verified["source_verified"] is True
    assert verified["verified_pdf_path"] == str(pdf)
    assert candidate_sources.verified_source_is_current(verified) is True
    pdf.write_bytes(b"changed")
    assert candidate_sources.verified_source_is_current(verified) is False


def test_source_verification_rejects_collections_and_fails_closed(monkeypatch):
    with pytest.raises(candidate_sources.CandidateSourceError, match="集合"):
        candidate_sources.verify_candidate_source({"results": []})
    monkeypatch.setattr(
        candidate_sources,
        "attachment_file_path",
        lambda key, helper=None: (_ for _ in ()).throw(
            candidate_sources.ZoteroPluginError("offline")
        ),
    )
    result = candidate_sources.verify_candidate_source(
        {"paper_id": "P1", "attachment_keys": ["ATTACH01"]}
    )
    assert result["source_verified"] is False
    assert "offline" in result["source_verification_error"]
