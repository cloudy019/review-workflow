# Release scope and verification

This repository was initialized separately from its originating private research project. A fixed allowlist exported only the state core, HTML renderer, derived index, local review server, human schema, synthetic tests and MIT-licensed KaTeX assets. No private project's Git history was inherited. A baseline commit preserves this code before public refactoring.

The release adds a unified reader protocol, portable source-page extraction, fenced host handoff, OpenAI-compatible API adapter, shared CLI, original reading instructions, packaging and synthetic verification. Optional skills from the private environment are not public prerequisites and are not bundled.

`scripts/audit_release.py` checks tracked files and every locally available commit for prohibited runtime artifacts, personal filesystem paths and recognizable credential patterns. It also reports wheel content when supplied with `--wheel`. Findings report only names and categories, never secret values. The audit does not claim to detect every possible secret encoding.

The local `code-sanitize` skill's entrypoint and reference were read. Its named scan/validation scripts were absent from the installed skill directory, so this repository supplies a bounded equivalent audit instead of reporting nonexistent commands as executed. Tests and package inspection supplement the scan.

Verification uses generated synthetic PDFs and a loopback HTTP API fixture. In addition, Codex authored a reading result and a real WorkBuddy model invocation read the same synthetic fixture with all host tools disabled, returning its result via stdout. Both were registered by the shared core as `human_reviewing`, with permission still false. WorkBuddy's JSON array envelope was discovered in this smoke test and added to adapter tests. These are functional reading/transport tests, not real-paper scientific quality validation. Real DeepSeek API calls remain unverified without a user's credentials. CI runs tests/builds on Windows and Linux with Python 3.10 and 3.12.

Ignored and excluded: PDFs, databases, runtime workspaces, review decisions, extraction logs, credentials, `.env` files, private host configuration and personal skill archives. The only bundled third-party asset is KaTeX with its MIT notice. Optional PyMuPDF is installed separately under its own license.
