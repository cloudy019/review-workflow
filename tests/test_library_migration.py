import json
from pathlib import Path

from review_workflow.library import (
    LEGACY_STATE,
    build_confirmed_outputs,
    plan_legacy_migration,
    write_legacy_skeletons,
)
from review_workflow.migrate_legacy import main


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _legacy_sources(tmp_path: Path) -> tuple[Path, Path]:
    screened = tmp_path / "papers-screened.json"
    _write_json(
        screened,
        {
            "records": [
                {
                    "zotero_key": "PAPER001",
                    "title": "Screened title",
                    "doi": "10.1/example",
                    "full_text_state": "full_text_indexed",
                    "full_text_read_status": "claim_level_verified",
                    "evidence_tier": "core",
                },
                {"zotero_key": "PAPER001", "title": "Duplicate old record"},
                {"title": "Missing key"},
            ]
        },
    )
    imports = tmp_path / "imports"
    imports.mkdir()
    (imports / "batch.md").write_text(
        """---
batch_id: batch-1
paper_count: 2
---
### PAPER001 — Imported title
old unconfirmed prose
### PAPER002 — Import-only title
old unconfirmed prose
""",
        encoding="utf-8",
    )
    return screened, imports


def test_plan_deduplicates_and_never_promotes_legacy_fulltext(tmp_path: Path) -> None:
    screened, imports = _legacy_sources(tmp_path)
    plan = plan_legacy_migration(screened, imports, tmp_path / "papers")

    assert [item["zotero_key"] for item in plan["skeletons"]] == ["PAPER001", "PAPER002"]
    assert plan["stats"]["screened_record_count"] == 3
    assert plan["stats"]["duplicate_screened_key_count"] == 1
    assert plan["stats"]["invalid_screened_record_count"] == 1
    assert plan["stats"]["planned_human_confirmed_count"] == 0
    assert plan["stats"]["planned_valid_citation_permission_count"] == 0

    old_fulltext = plan["skeletons"][0]
    assert old_fulltext["legacy"]["full_text_read_status"] == "claim_level_verified"
    assert old_fulltext["state"] == LEGACY_STATE
    assert old_fulltext["citation_permission"]["valid"] is False
    assert old_fulltext["human_review"]["status"] == "not_started"


def test_dry_run_writes_nothing_and_apply_never_overwrites(tmp_path: Path) -> None:
    screened, imports = _legacy_sources(tmp_path)
    papers = tmp_path / "papers"
    plan = plan_legacy_migration(screened, imports, papers)

    dry = write_legacy_skeletons(plan, papers, dry_run=True)
    assert dry["planned_count"] == 2
    assert not papers.exists()

    existing = papers / "PAPER002" / "paper.json"
    _write_json(existing, {"zotero_key": "PAPER002", "state": "human_confirmed"})
    applied = write_legacy_skeletons(plan, papers, dry_run=False)

    assert applied["written"] == ["PAPER001"]
    assert applied["skipped_existing"] == ["PAPER002"]
    assert json.loads(existing.read_text(encoding="utf-8"))["state"] == "human_confirmed"
    migrated = json.loads((papers / "PAPER001" / "paper.json").read_text(encoding="utf-8"))
    assert migrated["state"] == LEGACY_STATE
    assert migrated["citation_permission"]["valid"] is False


def test_aggregation_uses_only_confirmed_cards_with_valid_permission() -> None:
    eligible_bool = {
        "zotero_key": "ALLOW001",
        "state": "human_confirmed",
        "citation_permission": True,
        "approved_claims": ["confirmed claim"],
        "identity": {"title": "Allowed boolean"},
        "confirmed_knowledge": {
            "literature_matrix": {"claim": "confirmed claim"},
            "knowledge_relations": {"source": "A", "relation": "supports", "target": "B"},
            "writing_candidates": [{"claim": "usable in section 2"}],
        },
    }
    eligible_object = {
        "zotero_key": "ALLOW002",
        "state": "human_confirmed",
        "pdf_hash": "pdf-v1",
        "note_hash": "note-v1",
        "citation_permission": {
            "allowed": True,
            "approved_claims": ["second approved claim"],
            "pdf_hash": "pdf-v1",
            "note_hash": "note-v1",
        },
        "confirmed_knowledge": {
            "research_question": "RQ",
            "method": "method",
            "approved_claims": "second approved claim",
        },
    }
    rejected = [
        {
            "zotero_key": "LEGACY01",
            "state": "legacy_unconfirmed",
            "citation_permission": True,
            "approved_claims": ["must not leak"],
            "confirmed_knowledge": {"writing_candidates": "must not leak"},
        },
        {
            "zotero_key": "DENIED01",
            "state": "human_confirmed",
            "citation_permission": {"valid": False, "approved_claims": ["must not leak"]},
            "confirmed_knowledge": {"writing_candidates": "must not leak"},
        },
        {
            "zotero_key": "STALE001",
            "state": "human_confirmed",
            "pdf_hash": "new",
            "citation_permission": {
                "allowed": True,
                "approved_claims": ["must not leak"],
                "pdf_hash": "old",
            },
            "confirmed_knowledge": {"writing_candidates": "must not leak"},
        },
    ]

    output = build_confirmed_outputs([eligible_bool, eligible_object, *rejected])

    assert output["eligible_zotero_keys"] == ["ALLOW001", "ALLOW002"]
    assert len(output["literature_matrix"]) == 2
    assert len(output["knowledge_relations"]) == 1
    assert len(output["writing_candidates"]) == 2
    assert {item["zotero_key"] for item in output["writing_candidates"]} == {
        "ALLOW001",
        "ALLOW002",
    }
    assert {item["value"] for item in output["writing_candidates"]} == {
        "confirmed claim",
        "second approved claim",
    }
    serialized = json.dumps(output, ensure_ascii=False)
    assert "must not leak" not in serialized
    assert output["stats"]["rejection_reason_counts"] == {
        "pdf_hash_mismatch": 1,
        "permission_not_valid": 1,
        "state_not_human_confirmed": 1,
    }


def test_confirmed_without_specific_claims_enters_synthesis_not_writing() -> None:
    output = build_confirmed_outputs(
        [
            {
                "zotero_key": "NOCLAIM1",
                "state": "human_confirmed",
                "citation_permission": {"allowed": True},
                "confirmed_knowledge": {
                    "candidate_claims": [
                        {"candidate_id": "AI-1", "text": "AI proposal"}
                    ],
                    "writing_candidates": ["not actually approved"],
                },
            }
        ]
    )
    assert output["eligible_zotero_keys"] == ["NOCLAIM1"]
    assert output["writing_candidates"] == []
    assert output["candidate_claims"][0]["text"] == "AI proposal"


def test_cli_defaults_to_dry_run(tmp_path: Path, capsys) -> None:
    screened, imports = _legacy_sources(tmp_path)
    papers = tmp_path / "papers"

    exit_code = main(
        [
            "--screened",
            str(screened),
            "--imports-dir",
            str(imports),
            "--papers-dir",
            str(papers),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["mode"] == "dry-run"
    assert report["result"]["written_count"] == 0
    assert not papers.exists()
