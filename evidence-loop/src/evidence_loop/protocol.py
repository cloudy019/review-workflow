"""The one result contract used by agent hosts and direct model APIs."""
from __future__ import annotations

from collections.abc import Mapping
import json
from .core import ValidationError
from .html_review import EIGHT_SECTIONS

PROTOCOL_VERSION = "1.0"
SECTION_IDS = tuple(section["id"] for section in EIGHT_SECTIONS)
RESULT_FIELDS = {"schema_version", "paper_id", "pdf_sha256", "title", "summary_markdown", "sections", "claims", "figure_decisions"}


def validate_result(result: Mapping, request: Mapping) -> None:
    if not isinstance(result, Mapping) or set(result) != RESULT_FIELDS:
        raise ValidationError("AI result must match the reading contract; human decisions are not AI outputs")
    if result["schema_version"] != PROTOCOL_VERSION:
        raise ValidationError("unsupported reading protocol version")
    for name in ("paper_id", "pdf_sha256"):
        if result[name] != request[name]:
            raise ValidationError(f"result belongs to a different source: {name}")
    for name in ("title", "summary_markdown"):
        if not isinstance(result[name], str) or not result[name].strip():
            raise ValidationError(f"missing model-authored {name}")
    unfinished = {"<source title>", "<model-authored evidence-grounded Chinese reading note>", "<model analysis>", "<candidate claim>", "<evidence function>"}
    if any(marker in json.dumps(result, ensure_ascii=False) for marker in unfinished):
        raise ValidationError("output example placeholders are not a completed AI reading")
    sections = result["sections"]
    if not isinstance(sections, dict) or set(sections) != set(SECTION_IDS):
        raise ValidationError("all eight reading sections are required")
    pages = {page["page"] for page in request["source_pages"]}
    def check_pages(anchors):
        if not isinstance(anchors, list) or not anchors or any(type(page) is not int or page not in pages for page in anchors):
            raise ValidationError("claims and sections must cite existing source pages")
    for section in sections.values():
        if not isinstance(section, dict) or set(section) != {"ai_content", "source_anchors", "source_pages"}:
            raise ValidationError("section requires AI content and traceable source anchors")
        if any(not isinstance(section[field], str) or not section[field].strip() for field in ("ai_content", "source_anchors")):
            raise ValidationError("empty reading section")
        check_pages(section["source_pages"])
    claims = result["claims"]
    if not isinstance(claims, list):
        raise ValidationError("claims must be a list")
    ids = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"claim_id", "text", "source_pages", "kind"}:
            raise ValidationError("invalid candidate claim")
        if not isinstance(claim["claim_id"], str) or not claim["claim_id"] or claim["claim_id"] in ids:
            raise ValidationError("candidate claim IDs must be unique")
        ids.add(claim["claim_id"])
        if not isinstance(claim["text"], str) or not claim["text"].strip() or claim["kind"] not in {"source_statement", "ai_inference"}:
            raise ValidationError("separate source statements from AI inferences")
        check_pages(claim["source_pages"])
    inventory = {item["id"] for item in request["figure_inventory"]}
    decisions = result["figure_decisions"]
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise ValidationError("figure decisions must be a list")
    if len(decisions) != len(inventory) or {item.get("id") for item in decisions} != inventory:
        raise ValidationError("every detected figure/table needs one explicit decision")
    for item in decisions:
        if set(item) != {"id", "decision", "reason"} or item["decision"] not in {"include", "omit"} or not isinstance(item["reason"], str) or not item["reason"].strip():
            raise ValidationError("figure decision needs include/omit and a concrete reason")


def example_result(request: Mapping) -> dict:
    """Shape only. Placeholder examples never advance a task."""
    page = request["source_pages"][0]["page"]
    return {
        "schema_version": PROTOCOL_VERSION, "paper_id": request["paper_id"],
        "pdf_sha256": request["pdf_sha256"], "title": "<source title>",
        "summary_markdown": "<model-authored evidence-grounded Chinese reading note>",
        "sections": {name: {"ai_content": "<model analysis>", "source_anchors": f"p.{page}, section/figure", "source_pages": [page]} for name in SECTION_IDS},
        "claims": [{"claim_id": "C1", "text": "<candidate claim>", "source_pages": [page], "kind": "source_statement"}],
        "figure_decisions": [{"id": item["id"], "decision": "include", "reason": "<evidence function>"} for item in request["figure_inventory"]],
    }
