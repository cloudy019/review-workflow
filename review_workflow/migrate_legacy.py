"""CLI for safe migration of the historical literature pool."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

try:  # Supports both ``python -m`` and direct script execution.
    from .library import plan_legacy_migration, write_legacy_skeletons
except ImportError:  # pragma: no cover - direct invocation compatibility
    from library import plan_legacy_migration, write_legacy_skeletons


DEFAULT_SCREENED = Path(
    "workspace/candidates/papers-screened.json"
)
DEFAULT_IMPORTS = Path("文献知识库/imports")
DEFAULT_PAPERS = Path("文献知识库/papers")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Plan legacy paper skeletons. Dry-run is the default; use --apply "
            "to write new paper.json files without overwriting existing cards."
        )
    )
    parser.add_argument("--screened", type=Path, default=DEFAULT_SCREENED)
    parser.add_argument("--imports-dir", type=Path, default=DEFAULT_IMPORTS)
    parser.add_argument("--papers-dir", type=Path, default=DEFAULT_PAPERS)
    parser.add_argument("--apply", action="store_true", help="Write planned new skeletons")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return a non-zero exit code for invalid records or import count mismatches",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = plan_legacy_migration(args.screened, args.imports_dir, args.papers_dir)
    result = write_legacy_skeletons(plan, args.papers_dir, dry_run=not args.apply)
    report = {"mode": "apply" if args.apply else "dry-run", "stats": plan["stats"], "result": result}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and (
        plan["stats"]["invalid_screened_record_count"]
        or plan["stats"]["batch_count_mismatches"]
    ):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

