---
name: paper-review
description: Read one paper and generate an evidence-grounded human review page through the shared Evidence Loop framework. Works in Codex, WorkBuddy and other hosts without a subagent requirement.
---

Use the installed `evidence_loop` package as the only workflow authority. Never copy its state machine into a host-specific script. Python >=3.10 and `pip install ".[pdf]"` from the repository are required. Run `python -m evidence_loop --help` to verify the actual runtime.

1. Read project instructions and `python -m evidence_loop --workspace <workspace> status`. Existing project knowledge bases can be used directly as the workspace. Select only the current batch's reading task. Each task contains one paper and at most one PDF. Do not open another batch while human review is pending.
2. Run `python -m evidence_loop --workspace <workspace> issue <job-id> --reader <host-name>`. Read the returned request JSON, its instructions, every source page and original page images needed to assess figures. A host's main model can perform the reading; subagents are optional. If an attempt already exists, stop its previous reader before explicitly issuing `--handoff`. Old attempts cannot publish.
3. Write an actual model-authored result JSON matching `output_example`, replacing every placeholder. Preserve page anchors, source statements versus AI inferences, and one include/omit decision per detected figure. Do not write `job.json`, human judgments, citation permission, shared indices or manuscripts. Save your result inside the issued attempt directory.
4. Run `python -m evidence_loop --workspace <workspace> submit <job-id> <attempt-id> <result.json>`. The core validates, renders, registers and moves to human review in one operation. Open the returned job's `agent-output/paper-review.html`, or use `serve` for save-and-advance in the same review UI. The AI never fills in human answers.

If the host cannot execute commands or write its result, report the specific denied capability. Do not claim another software's subagents are required, bypass permission checks, or mark the task complete. A configured API reader can be run by the same framework outside the host. API text reading does not constitute visual inspection. Read all source pages; when evidence is unavailable, label the missing evidence rather than invent it.

Only `knowledge --approved-only` supplies human-approved claims for downstream writing. Paper-level permission alone supplies candidates for synthesis, not approved manuscript claims. Source or reading-artifact changes invalidate permission. Moving a workspace preserves tasks; rebind an external PDF with `relocate-source` only when its hash is identical.
