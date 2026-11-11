"""Read-only audit of already-registered AI verifications against the current gate.

This never changes a job.  It answers: of the jobs that were allowed to become
``ai_verified``, how many would the *current* visual-evidence gate have stopped,
and on which objectively checkable grounds?

Usage:
    python artifacts/review_ui/audit_ai_gate.py [--out tmp/ai_gate_audit.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evidence-loop" / "src"))

from evidence_loop.core import ReviewOrchestrator  # noqa: E402
from rerender import JOBS  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="tmp/ai_gate_audit.json")
    args = parser.parse_args()
    rows = []
    for path in sorted(JOBS.glob("*/job.json")):
        job = json.loads(path.read_text(encoding="utf-8"))
        permission = job.get("ai_citation_permission") or {}
        if not permission.get("valid"):
            continue
        report = ReviewOrchestrator.visual_evidence_report(job)
        rows.append(
            {
                "job_id": job["job_id"],
                "state": job["state"],
                "waived": ReviewOrchestrator.has_visual_qa_waiver(job),
                **report,
            }
        )
    blocking = [row for row in rows if row["problems"] and not row["waived"]]
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "scope": "jobs with ai_citation_permission.valid=true",
                "registered": len(rows),
                "would_be_blocked_now": len(blocking),
                "with_waiver": sum(1 for row in rows if row["waived"]),
                "rows": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"已登记 AI 核验 {len(rows)} 篇；按现行门禁会被拦下 {len(blocking)} 篇 → {out}")
    for row in rows:
        print(
            f"  {row['job_id']}  state={row['state']:<16} "
            f"crops={row['available_crops']}/{row['expected_crops']} "
            f"visual_review={row['visual_review']} qa={row['html_visual_qa']!s:<28} "
            f"problems={len(row['problems'])}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
