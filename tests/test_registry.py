import json
from pathlib import Path

from review_workflow.core import ReviewOrchestrator
from review_workflow.registry import write_review_registry


def test_registry_centralizes_paths_without_copying_pdf(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"%PDF-1.4\nregistry")
    workflow = ReviewOrchestrator(kb / "review-jobs")
    job = workflow.prepare_paper("doi:10.1000/registry", pdf, zotero_key="REGISTRY")
    sections = Path(job["agent_output_dir"]) / "review-sections.json"
    sections.write_text(
        json.dumps({"title": "Central registry paper"}), encoding="utf-8"
    )

    json_path, markdown_path = write_review_registry(kb)
    registry = json.loads(json_path.read_text(encoding="utf-8"))
    paper = registry["papers"][0]

    assert registry["managed_paper_count"] == 1
    assert paper["title"] == "Central registry paper"
    assert paper["pdf"]["ownership"] == "zotero"
    assert paper["pdf"]["path"] == str(pdf.resolve())
    assert not (kb / "review-app" / pdf.name).exists()
    assert paper["files"]["latest_human_review"].endswith(
        "human_reviews\\REGISTRY.human-review.json"
    )
    assert "Central registry paper" in markdown_path.read_text(encoding="utf-8")


def test_registry_includes_unread_cards_without_jobs(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    card = kb / "papers" / "UNREAD01" / "paper.json"
    card.parent.mkdir(parents=True)
    card.write_text(
        json.dumps(
            {
                "zotero_key": "UNREAD01",
                "state": "legacy_unconfirmed",
                "identity": {"title": "Unread Zotero paper", "doi": "10.1000/unread"},
            }
        ),
        encoding="utf-8",
    )

    json_path, markdown_path = write_review_registry(kb)
    registry = json.loads(json_path.read_text(encoding="utf-8"))

    assert registry["managed_paper_count"] == 1
    assert registry["reviewed_count"] == 0
    assert registry["unreviewed_count"] == 1
    assert registry["papers"][0]["review_group"] == "unreviewed"
    ledger = markdown_path.read_text(encoding="utf-8")
    assert "Unread Zotero paper" in ledger
    assert "| UNREAD01 | 未读 |" in ledger
