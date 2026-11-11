# Optional AI verification

Default behavior remains `human_required`. To allow AI verification with optional later human review, set this in the workspace's `review-project.json`:

```json
{"review_policy": {"mode": "ai_review_with_optional_human"}}
```

Authorization metadata may coexist in `review_policy`; the core reads `mode`. This policy does not automatically verify a reading or manufacture human approval. `submit` still produces the reading materials and opens review.

After substantive review of one completed reading, use:

```sh
evidence-loop --workspace workspace ai-review-input JOB_ID
evidence-loop --workspace workspace verify-ai JOB_ID ai-review.json
evidence-loop --workspace workspace knowledge --approved-only
```

`ai-review-input` returns identity, current PDF/note hashes, the complete `artifact_sha256` mapping, and `human_review_sha256` (null if no prior human archive). Copy these bindings into a report alongside the following fields. The input command does not grant permission or perform substantive review.

| Field | Required content |
| --- | --- |
| `schema_version` | `"1.0"` |
| `reviewer`, `reviewed_at` | Verifier identity and ISO timestamp with timezone |
| `human_review_response` | How the verification respects prior human corrections; required nonempty when a human archive is bound, otherwise may be empty |
| `sections` | Eight objects, unique `section_id` 1–8, `verdict` `verified` or `qualified`, substantive `notes` and nonempty string list `source_anchors` |
| `verified_claims` | At least one object with unique `claim_id`, scoped `text`, nonempty string list `source_anchors`, and nonempty `limitations` |

Source anchors identify actual PDF pages, figure/table panels or sections. Qualified findings must carry their limitations into each relevant claim. Only corrected, usable claims belong in `verified_claims`; unresolved findings require revision or exclusion. Structural validation cannot replace substantive source verification. A report contains no human answers, `approved_claims`, or human approval flag. Unknown report fields are rejected.

The command accepts `ai_note_ready` or `human_reviewing` jobs. It verifies all bindings, archives the report, writes separate `ai_citation_permission`, and enters `ai_verified`. It keeps the original reading products and genuine human records intact. Legacy jobs need a source PDF, note, `review-sections.json` and `paper-review.html`; native jobs also bind every reading receipt artifact. The report's verified claims, rather than unverified candidates, enter `knowledge`.

In this optional mode, `knowledge --approved-only` means claims permitted under the active policy: AI claims carry `verification_origin: "ai"`, `ai_verified: true`, `citation_allowed: true`, and **`human_approved: false`**. Existing human-confirmed exports and their claim approval rules remain unchanged. Consumers must retain the origin and claim limitations. Prior human corrections are exported as historical context and remain bound to the report.

An effective `ai_verified` job releases its batch. A PDF, note, protected product, prior human archive or AI report change invalidates permission; later knowledge export and batch dispatch check actual hashes. Switching back to the default policy also excludes AI permission from knowledge and next-batch eligibility. Completed batch records remain historical; unresolved AI invalidation blocks the next batch.

The original review page remains usable. Its served claims tab shows the current verified claim text and limitations, without modifying the hash-protected HTML file. A later human save supersedes AI permission, including denial, exclusion or a request for clarification; human approval is recorded only through the human review entry point. A human decision on AI-verified claims exports `verification_origin: "human"` and `content_origin: "ai_verification"`, preserving the exact verified text rather than reviving older candidates. That content remains hash-protected. Optional mode also allows a later human denial or clarification to replace an earlier human confirmation.

Human decisions on AI-verified content must additionally carry `ai_review_sha256`, equal to the current AI archive hash. The served form supplies it; an external `import-review` producer must include it. Default human reviews still use the original fields. Old pages and drafts cannot approve a new AI report with the same claim IDs and PDF/note hashes; the draft key and restore check include this additional binding.

`revoke` withdraws AI permission. `reopen` preserves the job snapshot and genuine review archives, clears the active reading receipt, and requires a new reading/review. In optional mode it also accepts `human_reviewing` and `ai_verified`; an existing active batch is retained. No command migrates or verifies other papers automatically.
