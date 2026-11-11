import json

import pytest

from review_workflow.skill_router import route_skills
from review_workflow.style_memory import record_edit_pair, style_training_pairs
from review_workflow.writing import audit_draft, build_argument_package


def test_read_route_is_specialized_but_single_paper_oriented():
    route = route_skills("read", paper_type="materials", has_statistics=True, has_structure_data=True)
    assert route.skills[:2] == ("nature-reader", "deeppapernote")
    assert "pymatgen" in route.skills
    assert "nature-statistics" in route.skills


def test_workflow_leak_and_unconfirmed_citation_are_blocked():
    issues = audit_draft(
        "基于去重的150篇证据池，并进行Cohen's kappa编码。",
        section_kind="introduction",
        cited_paper_ids=["A", "B"],
        allowed_paper_ids=["A"],
    )
    codes = {item.code for item in issues}
    assert "unconfirmed-citation" in codes
    assert "evidence-pool-count" in codes
    assert "coding-process" in codes


def test_workflow_metadata_can_live_in_transparency_section():
    issues = audit_draft(
        "证据池包含150篇。",
        section_kind="transparency",
        cited_paper_ids=["A"],
        allowed_paper_ids=["A"],
    )
    assert issues == []


def test_argument_package_uses_only_given_confirmed_cards():
    package = build_argument_package(
        "section-2",
        [
            {
                "paper_id": "A",
                "writing_positions": ["section-2"],
                "confirmed_claims": [
                    {"claim_id": "A-C1", "text": "Claim", "locator": "p. 4"}
                ],
            }
        ],
        section_goal="比较规则与机制",
    )
    assert package["status"] == "pending_human_approval"
    assert package["evidence"][0]["paper_id"] == "A"


def test_argument_package_can_offer_ai_candidates_for_later_human_choice():
    package = build_argument_package(
        "section-2",
        [
            {
                "paper_id": "A",
                "candidate_claims": [
                    {"candidate_id": "AI-1", "text": "AI summarized claim"}
                ],
            }
        ],
        section_goal="选择候选论点",
    )
    assert package["approved_claim_ids"] == []
    assert package["evidence"][0]["status"] == "pending_human_approval"


def test_style_pairs_require_explicit_consent(tmp_path):
    store = tmp_path / "style-pairs.json"
    record_edit_pair(
        store,
        original="原文。",
        revised="修改。",
        language="zh",
        edit_type="fact",
        include_in_style_profile=False,
    )
    record_edit_pair(
        store,
        original="旧表达。",
        revised="我的表达。",
        language="zh",
        edit_type="style",
        include_in_style_profile=True,
    )
    pairs = style_training_pairs(store, language="zh")
    assert len(pairs) == 1
    assert pairs[0]["revised"] == "我的表达。"
