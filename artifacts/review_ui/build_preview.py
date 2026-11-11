"""Render review pages into artifacts/review_ui/preview/ without touching a job.

A job's ``paper-review.html`` hash is part of the human-review receipt, so
layout experiments must be judged on throw-away copies first.  This script
reuses ``rerender.build_record`` (byte-for-byte the record ``Workflow.submit``
builds) and only writes files under ``preview/``.

Usage:
    python artifacts/review_ui/build_preview.py                # all three jobs
    python artifacts/review_ui/build_preview.py <job_id> [...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evidence-loop" / "src"))
sys.path.insert(0, str(ROOT / "artifacts" / "review_ui"))

from evidence_loop.html_review import render_review_html  # noqa: E402
from rerender import JOBS, build_record  # noqa: E402

DEFAULT_JOBS = [
    "paper-45d514c98a379d42",
    "paper-7844aca4d0f7364f",
    "paper-af794951649f8519",
]
PREVIEW = Path(__file__).resolve().parent / "preview"


def build(job_id: str) -> dict:
    job = json.loads((JOBS / job_id / "job.json").read_text(encoding="utf-8"))
    record = build_record(job)
    html = render_review_html(record)
    target = PREVIEW / f"{job_id}.html"
    target.write_text(html, encoding="utf-8")
    return {
        "job_id": job_id,
        "mb": round(len(html.encode("utf-8")) / 1e6, 1),
        "figures": html.count('class="fig"'),
        "path": str(target),
    }


def main(argv: list[str]) -> int:
    PREVIEW.mkdir(parents=True, exist_ok=True)
    for job_id in argv or DEFAULT_JOBS:
        print(json.dumps(build(job_id), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
