import pytest

from review_workflow.screening import (
    RUBRIC_VERSION,
    ScreeningError,
    score_paper,
    select_next_batch,
)


def test_score_paper_strictly_accepts_one_mapping():
    with pytest.raises(ScreeningError, match="一次只接受一个"):
        score_paper([{"paper_id": "A"}, {"paper_id": "B"}])
    with pytest.raises(ScreeningError, match="逐篇"):
        score_paper({"records": [{"paper_id": "A"}]})
    with pytest.raises(ScreeningError, match="title"):
        score_paper({"paper_id": "A", "title": ["one", "two"]})


def test_strong_single_paper_matches_central_thesis_and_three_axes():
    result = score_paper(
        {
            "paper_id": "10.1000/strong",
            "zotero_key": "ABCD1234",
            "title": "Explainable machine learning for lithium-ion battery materials design",
            "abstract": (
                "We develop SHAP rule extraction for cathode materials and electrolyte design. "
                "The resulting design rule and scientific descriptor are tested by independent "
                "experimental validation and transferability across battery chemistries; code "
                "and a quantitative dataset are available."
            ),
            "paper_type": "primary_research",
        }
    )
    assert result["score"] == 10
    assert result["status"] == "highly_relevant"
    assert result["rubric_version"] == RUBRIC_VERSION
    assert result["axes"] == ["method", "materials", "battery"]
    assert sum(result["breakdown"].values()) == result["score"]


def test_irrelevant_result_is_low_and_deterministic():
    paper = {
        "paper_id": "OFFTOPIC",
        "title": "Urban traffic demand forecasting",
        "abstract": "A general forecasting model for road traffic is presented.",
    }
    first = score_paper(paper)
    second = score_paper(dict(reversed(list(paper.items()))))
    assert first == second
    assert first["score"] < 5
    assert first["status"] == "not_relevant"
    assert first["eligible_for_reading"] is False


def _result(paper_id, score, axes, **extra):
    return {
        "paper_id": paper_id,
        "zotero_key": extra.pop("zotero_key", None),
        "title": paper_id,
        "score": score,
        "status": "highly_relevant" if score >= 9 else "relevant",
        "rubric_version": RUBRIC_VERSION,
        "axes": axes,
        **extra,
    }


def test_next_batch_prefers_top_score_then_fills_three_axes():
    decision = select_next_batch(
        [
            _result("METHOD-TOP", 10, ["method"]),
            _result("METHOD-SECOND", 9, ["method"]),
            _result("MATERIALS", 8, ["materials"]),
            _result("BATTERY", 7, ["battery"]),
        ]
    )
    assert [item["paper_id"] for item in decision["selected"]] == [
        "METHOD-TOP",
        "MATERIALS",
        "BATTERY",
    ]
    assert decision["covered_axes"] == ["method", "materials", "battery"]
    assert all(item["selection_reason"] for item in decision["selected"])


def test_next_batch_uses_distinct_frozen_roles_even_if_first_paper_has_multiple_axes():
    decision = select_next_batch(
        [
            _result("BATTERY-XAI", 10, ["method", "battery"], paper_role="battery_application"),
            _result("BATTERY-SECOND", 9, ["method", "battery"], paper_role="battery_application"),
            _result("MATERIALS", 9, ["method", "materials"], paper_role="materials_application"),
            _result("METHOD", 8, ["method"], paper_role="computer_science_method"),
        ]
    )
    assert [item["paper_id"] for item in decision["selected"]] == [
        "BATTERY-XAI",
        "MATERIALS",
        "METHOD",
    ]


def test_existing_nonlegacy_and_terminal_are_excluded_but_legacy_is_eligible():
    results = [
        _result("ACTIVE", 10, ["method"], zotero_key="ACTIVE01"),
        _result("LEGACY", 9, ["materials"], zotero_key="LEGACY01"),
        _result("TERMINAL", 9, ["battery"], workflow_state="human_confirmed"),
        _result("NEW", 8, ["battery"]),
    ]
    decision = select_next_batch(
        results,
        existing_tasks=[
            {"paper_id": "OTHER-ID", "zotero_key": "ACTIVE01", "state": "reading"},
            {"paper_id": "LEGACY", "zotero_key": "LEGACY01", "state": "legacy_unconfirmed"},
        ],
    )
    assert [item["paper_id"] for item in decision["selected"]] == ["LEGACY", "NEW"]
    excluded = {item["paper_id"]: item["reason"] for item in decision["excluded"]}
    assert "非legacy" in excluded["ACTIVE"]
    assert "终态" in excluded["TERMINAL"]


def test_selection_rejects_more_than_three_and_duplicate_results():
    with pytest.raises(ScreeningError, match="1至3"):
        select_next_batch([], max_papers=4)
    duplicate = [_result("A", 8, ["method"]), _result("A", 9, ["battery"])]
    with pytest.raises(ScreeningError, match="重复论文"):
        select_next_batch(duplicate)
    duplicate_zotero = [
        _result("DOI-A", 8, ["method"], zotero_key="SAMEKEY1"),
        _result("DOI-B", 9, ["battery"], zotero_key="SAMEKEY1"),
    ]
    with pytest.raises(ScreeningError, match="重复论文"):
        select_next_batch(duplicate_zotero)


def test_low_score_is_recorded_as_excluded_not_selected():
    decision = select_next_batch([_result("LOW", 6, ["materials"])])
    assert decision["selected"] == []
    assert decision["excluded"] == [
        {"paper_id": "LOW", "reason": "未达到全文阅读阈值"}
    ]


def test_legacy_fields_map_axes_and_pass_through_without_verifying_source():
    result = score_paper(
        {
            "zotero_key": "QW5SFFZV",
            "title": "Local effects analysis",
            "abstract": "",
            "doi": "10.1177/example",
            "year": "2021",
            "paper_role": "battery_application",
            "evidence_tier": "support",
            "fulltext_priority": 3,
            "full_text_state": "full_text_indexed_unread",
            "signals": {"battery": True, "materials": True, "explanation": True, "rule": True},
            "knowledge_products": ["R"],
            "attachment_details": {
                "attachments": [{"key": "ATTACH01", "is_pdf": True}],
                "status": "full_text_indexed_unread",
            },
        }
    )
    # Frozen routing role prevents incidental legacy domain flags from making
    # one paper satisfy two application slots in an automatic batch.
    assert result["axes"] == ["method", "battery"]
    assert result["doi"] == "10.1177/example"
    assert result["year"] == "2021"
    assert result["paper_role"] == "battery_application"
    assert result["evidence_tier"] == "support"
    assert result["fulltext_priority"] == 3
    assert result["attachment_keys"] == ["ATTACH01"]
    assert result["source_hint"] == "zotero_attachment:ATTACH01"
    assert result["source_verified"] is False
    assert result["legacy_screening"] == "provisional"
    assert result["legacy_full_text_state"] == "full_text_indexed_unread"
    assert result["citation_allowed"] is False


def test_formal_batch_can_require_verified_source():
    provisional = _result("PROVISIONAL", 10, ["method"], source_verified=False)
    verified = _result("VERIFIED", 8, ["battery"], source_verified=True)
    decision = select_next_batch(
        [provisional, verified], require_verified_source=True
    )
    assert [item["paper_id"] for item in decision["selected"]] == ["VERIFIED"]
    assert decision["require_verified_source"] is True
    assert decision["excluded"] == [
        {"paper_id": "PROVISIONAL", "reason": "来源尚未经当前流程核验"}
    ]
