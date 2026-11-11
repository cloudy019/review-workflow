# Protocol 1.0

This document describes the default `human_required` flow. The reading request/result protocol is unchanged in the optional `ai_review_with_optional_human` mode. Its separate verification report, `ai_verified` branch, knowledge provenance and human content-hash binding are documented in [ai-review.md](ai-review.md).

Every integration consumes one issued request and submits one result. `request.json` supplies `job_id`, `paper_id`, `pdf_sha256`, `attempt_id`, research context, every source page, original PNG pages, caption-detected figure inventory, instructions and an output example. Model adapters must use these source identities unchanged.

The result has exactly these fields:

| Field | Contract |
|---|---|
| schema_version | `1.0` |
| paper_id / pdf_sha256 | Same source as issued request |
| title / summary_markdown | Actual model-authored reading; placeholders rejected |
| sections | Eight named sections: `relevance`, `question_contribution`, `methods_data`, `results_evidence`, `figures_equations`, `limitations`, `knowledge_product`, `synthesis_permission` |
| claims | Candidate claims with unique `claim_id`, `text`, `source_pages`, `kind` (`source_statement` or `ai_inference`) |
| figure_decisions | Exactly one `{id, decision, reason}` per inventory item; decision `include` or `omit` |

Each section contains `ai_content`, `source_anchors`, `source_pages`. All anchors must reference existing physical PDF pages, numbered from 1. Caption detection is not a complete figure detector; full page images are retained independently. An omit decision does not remove the original page from human inspection.

Readers cannot submit human verdicts, permission or state transitions in the result. The CLI validates exact fields, identities and attempt ownership, writes a hash receipt, renders the review page and commits `human_reviewing`. Unchanged legacy jobs are accepted through the compatibility state core; old host scripts must not publish into a fenced native/API attempt.

State progression: `candidate → identity_verified → source_ready → reading → ai_note_ready → human_reviewing → human_confirmed → indexed → synthesis_eligible`. The public `Workflow.submit` commits the two middle publication transitions together. Human exclusion, source blocking and clarification use explicit branches. A batch has at most 3 papers and stays open until every paper reaches a human terminal decision or source-blocked/rescinded stop.

Human records follow [human_review.schema.json](../src/evidence_loop/schemas/human_review.schema.json), bind paper/PDF/note identities and require all eight judgments for confirmation. Paper permission and claim approval are separate. In the section-eight claims box, enter an exact claim ID (e.g. `C1`) or the exact approved claim text, one per line. Unapproved candidates never appear as approved writing claims. Permission can be revoked explicitly. Changed source/result/HTML/page assets revoke permission; a modified note cannot reuse an old approval.

Handoff: stop the prior reader, issue `--handoff`, then supply the new attempt ID when submitting. Prior attempt files remain auditable but their token no longer publishes. Workspace-local paths are rebased on load. External PDF relocation must preserve SHA-256. API credentials are environment-only and never stored in request, result or human review.

Direct API integration implements the `Reader` protocol (`name`, `generate(request) -> dict`). Host labels are provenance, not exclusive permissions. Add an API/provider adapter by producing this result; it must not create a second state store. OpenAI-compatible text adapters reject incomplete responses and never silently drop document pages. Empty-image PDFs need OCR before the current text extraction route; visual-reader expansion can retain the same result protocol.

The local server is a human review UI on loopback with token and Origin checks. It assumes a trusted local user; it is not a multi-user identity or authorization server. Model output is untrusted content, and local review pages sanitize HTML and images. Host command permissions remain the host's configuration, separate from model capability.
