from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORKFLOW_LEAK_PATTERNS = {
    "evidence-pool-count": re.compile(
        r"(?:(?:证据池|evidence pool).{0,20}\d+|\d+.{0,20}(?:篇)?(?:证据池|evidence pool))",
        re.I,
    ),
    "coding-process": re.compile(r"(?:编码者|重复编码|coder|recoding|Cohen.?s\s*[κk]|kappa)", re.I),
    "agent-log": re.compile(r"(?:代理日志|agent log|任务运行|检索批次已归档)", re.I),
    "screening-ledger": re.compile(r"(?:第一轮|second coding|test.retest).{0,30}(?:一致|agreement)", re.I),
}

TRANSPARENCY_SECTIONS = {"methods", "methodology", "transparency", "supplement", "limitations"}


@dataclass(frozen=True)
class AuditIssue:
    code: str
    message: str


def load_confirmed_cards(papers_root: Path) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for path in sorted(papers_root.glob("*/paper.json")):
        card = json.loads(path.read_text(encoding="utf-8"))
        permission = card.get("citation_permission", False)
        allowed = permission is True or (isinstance(permission, dict) and permission.get("valid") is True)
        if card.get("state") == "human_confirmed" and allowed and not card.get("rescinded_at"):
            cards.append(card)
    return cards


def build_argument_package(
    section_id: str,
    cards: Iterable[dict[str, Any]],
    *,
    section_goal: str,
) -> dict[str, Any]:
    if not section_id.strip() or not section_goal.strip():
        raise ValueError("section_id and section_goal are required")
    evidence: list[dict[str, Any]] = []
    for card in cards:
        placements = card.get("writing_positions", [])
        if placements and section_id not in placements:
            continue
        for claim in card.get("confirmed_claims", []):
            evidence.append(
                {
                    "paper_id": card.get("paper_id") or card.get("zotero_key"),
                    "claim_id": claim.get("claim_id"),
                    "claim": claim.get("text"),
                    "locator": claim.get("locator"),
                    "source_kind": claim.get("kind"),
                    "source_pages": claim.get("source_pages", []),
                    "pdf_sha256": card.get("pdf_hash"),
                    "ai_note_sha256": card.get("note_hash"),
                    "stance": claim.get("stance", "support"),
                    "boundary": claim.get("boundary") or card.get("applicability_boundary"),
                }
            )
        for claim in card.get("candidate_claims", []):
            if isinstance(claim, dict):
                evidence.append(
                    {
                        "paper_id": card.get("paper_id") or card.get("zotero_key"),
                        "claim_id": claim.get("candidate_id") or claim.get("claim_id"),
                        "claim": claim.get("text") or claim.get("claim"),
                        "locator": claim.get("locator"),
                        "stance": claim.get("stance", "candidate"),
                        "boundary": claim.get("boundary") or card.get("applicability_boundary"),
                        "status": "pending_human_approval",
                    }
                )
    return {
        "schema_version": "1.0",
        "section_id": section_id,
        "section_goal": section_goal,
        "status": "pending_human_approval",
        "evidence": evidence,
        "approved_claim_ids": [],
        "paragraph_order": [],
    }


def audit_draft(
    text: str,
    *,
    section_kind: str,
    cited_paper_ids: Iterable[str],
    allowed_paper_ids: Iterable[str],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    allowed = set(allowed_paper_ids)
    disallowed = sorted(set(cited_paper_ids) - allowed)
    if disallowed:
        issues.append(AuditIssue("unconfirmed-citation", f"未获人工许可的引文：{', '.join(disallowed)}"))
    if section_kind.strip().lower() not in TRANSPARENCY_SECTIONS:
        for code, pattern in WORKFLOW_LEAK_PATTERNS.items():
            if pattern.search(text):
                issues.append(AuditIssue(code, "内部检索、编码或代理流程出现在不合适的正文位置"))
    return issues


def assert_draft_admissible(*args: Any, **kwargs: Any) -> None:
    issues = audit_draft(*args, **kwargs)
    if issues:
        raise ValueError("; ".join(f"{issue.code}: {issue.message}" for issue in issues))
