import argparse
import hashlib
import json
from pathlib import Path

from review_workflow.cli import (
    _cmd_dispatch,
    _cmd_import,
    _cmd_rebuild,
    _cmd_screen_pool,
    _cmd_start_next,
    _orchestrator,
)
from review_workflow.core import sha256_file


def _review(job):
    return {
        "schema_version": "1.0",
        "job_id": job["job_id"],
        "paper_id": job["paper_id"],
        "zotero_key": job["zotero_key"],
        "pdf_sha256": job["pdf_sha256"],
        "ai_note_sha256": job["ai_note_sha256"],
        "review_outcome": "human_confirmed",
        "sections": [
            {
                "section_id": number,
                "verdict": "correct",
                "notes": f"人工判断{number}",
                "answers": {},
            }
            for number in range(1, 9)
        ],
        "citation_permission": {
            "allowed": True,
            "approved_claims": ["XAI结果只允许支持这一条具体主张"],
            "notes": "人工许可",
        },
        "exported_at": "2026-08-11T12:00:00+08:00",
    }


def test_cli_confirmed_review_materializes_and_rebuilds(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfixture")
    workflow = _orchestrator(kb)
    job = workflow.prepare_paper(
        "doi:10.1000/one", pdf, zotero_key="ABCD1234"
    )

    dispatch = _cmd_dispatch(
        argparse.Namespace(
            kb=kb,
            job_ids=[job["job_id"]],
            paper_type="ml",
            statistics=True,
            structure_data=False,
        )
    )
    manifest = json.loads(
        Path(dispatch["agent_tasks"][0]["manifest_path"]).read_text(encoding="utf-8")
    )
    assert manifest["paper_id"] == "doi:10.1000/one"
    assert manifest["read_only_sources"] == [str(pdf.resolve())]
    assert "nature-reader" in manifest["allowed_skills"]
    assert "deeppapernote" in manifest["allowed_skills"]

    note = Path(job["agent_output_dir"]) / "deep-paper-note.md"
    note.write_text("AI层笔记", encoding="utf-8")
    ready = workflow.attach_ai_note(job["job_id"], note)
    review_path = tmp_path / "review.json"
    review_path.write_text(
        json.dumps(_review(ready), ensure_ascii=False), encoding="utf-8"
    )
    imported = _cmd_import(argparse.Namespace(kb=kb, review=review_path))
    assert imported["state"] == "synthesis_eligible"

    card_path = kb / "papers" / "ABCD1234" / "paper.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    assert card["state"] == "human_confirmed"
    assert card["citation_permission"]["valid"] is True
    assert card["confirmed_claims"][0]["text"].startswith("XAI结果")
    assert card["pdf_hash"] == hashlib.sha256(pdf.read_bytes()).hexdigest()

    rebuilt = _cmd_rebuild(argparse.Namespace(kb=kb))
    assert rebuilt["eligible_card_count"] == 1
    library = json.loads(
        (kb / "generated" / "confirmed-library.json").read_text(encoding="utf-8")
    )
    assert library["eligible_zotero_keys"] == ["ABCD1234"]
    assert len(library["writing_candidates"]) == 1


def test_permission_hash_change_fails_closed(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    paper_dir = kb / "papers" / "ABCD1234"
    paper_dir.mkdir(parents=True)
    card = {
        "zotero_key": "ABCD1234",
        "state": "human_confirmed",
        "pdf_hash": "a" * 64,
        "note_hash": "b" * 64,
        "approved_claims": ["H1"],
        "confirmed_claims": [{"claim_id": "H1", "text": "可用主张"}],
        "citation_permission": {
            "allowed": True,
            "valid": True,
            "approved_claims": ["H1"],
            "pdf_hash": "c" * 64,
            "note_hash": "b" * 64,
        },
    }
    (paper_dir / "paper.json").write_text(json.dumps(card), encoding="utf-8")
    rebuilt = _cmd_rebuild(argparse.Namespace(kb=kb))
    assert rebuilt["eligible_card_count"] == 0


def test_automatic_screening_and_heterogeneous_batch_start(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    candidate_source = tmp_path / "candidates.json"
    candidate_source.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "zotero_key": "METHOD01",
                        "title": "Explainable rule extraction",
                        "abstract": "Explainable method extracts a transferable design rule.",
                        "paper_role": "computer_science_method",
                        "signals": {"rule": True},
                    },
                    {
                        "zotero_key": "MATERI01",
                        "title": "Validated materials descriptor",
                        "abstract": "A materials descriptor receives independent validation.",
                        "paper_role": "materials_application",
                        "signals": {"concept": True, "validation": True},
                    },
                    {
                        "zotero_key": "BATTERY1",
                        "title": "Battery mechanism",
                        "abstract": "A lithium-ion battery mechanism receives experimental validation.",
                        "paper_role": "battery_application",
                        "signals": {"mechanism": True, "validation": True},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    screened = _cmd_screen_pool(
        argparse.Namespace(kb=kb, source=candidate_source, output=None)
    )
    assert screened["paper_count"] == 3

    selected = []
    for number, (key, axis) in enumerate(
        (("METHOD01", "method"), ("MATERI01", "materials"), ("BATTERY1", "battery")),
        1,
    ):
        pdf = tmp_path / f"{number}.pdf"
        pdf.write_bytes(b"%PDF-1.4\n" + key.encode())
        selected.append(
            {
                "paper_id": key,
                "zotero_key": key,
                "doi": None,
                "title": key,
                "score": 9,
                "axes": [axis],
                "verified_pdf_path": str(pdf),
                "verified_pdf_sha256": sha256_file(pdf),
                "source_verified": True,
                "selection_reason": "自动测试",
            }
        )
    selection_path = kb / "screening" / "next-batch.json"
    selection_path.parent.mkdir(parents=True, exist_ok=True)
    selection_path.write_text(
        json.dumps(
            {
                "schema_version": "review_workflow.selection.v1",
                "selection_id": "selection-1234567890abcdef",
                "status": "reserved",
                "selected": selected,
                "batch_id": None,
            }
        ),
        encoding="utf-8",
    )
    started = _cmd_start_next(argparse.Namespace(kb=kb, selection=selection_path))
    assert started["status"] == "started"
    assert len(started["agent_tasks"]) == 3
    tasks = {task["paper_id"]: task for task in started["agent_tasks"]}
    assert "ml-paper-writing" in tasks["zotero:METHOD01"]["allowed_skills"]
    assert "ml-paper-writing" not in tasks["zotero:MATERI01"]["allowed_skills"]
    assert all(task["paper_limit"] == 1 for task in tasks.values())
