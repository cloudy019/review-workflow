#!/usr/bin/env python3
"""Portable entry point; resolve runtime from this skill, never the host checkout."""
from pathlib import Path
import argparse
import json
import sys

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT / "references" / "runtime"))

from review_workflow.reading import accept_reading_output, extract_reading_source, prepare_reading_task, render_reading_output
from review_workflow.reading_kit import default_kit, install_reading_kit, reading_capabilities
from review_workflow.core import ReviewOrchestrator
from review_workflow.skill_router import route_skills


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    install = commands.add_parser("install")
    install.add_argument("--engine", choices=("codex", "workbuddy", "both"), default="both")
    install.add_argument("--project", type=Path, default=Path.cwd())
    install.add_argument("--target", type=Path)
    task = commands.add_parser("task")
    task.add_argument("--kb", type=Path, required=True)
    task.add_argument("--job-id", required=True)
    task.add_argument("--engine", choices=("codex", "workbuddy"), required=True)
    task.add_argument("--handoff", action="store_true")
    prepare = commands.add_parser("prepare", help="显式单篇来源/新项目调试入口；正式批次沿用项目自动选批")
    prepare.add_argument("--kb", type=Path, required=True)
    prepare.add_argument("--paper-id", required=True)
    prepare.add_argument("--zotero-key", required=True)
    prepare.add_argument("--pdf", type=Path, required=True)
    prepare.add_argument("--engine", choices=("codex", "workbuddy"), required=True)
    prepare.add_argument("--paper-type", default="general")
    render = commands.add_parser("render")
    render.add_argument("task", type=Path)
    extract = commands.add_parser("extract")
    extract.add_argument("task", type=Path)
    accept = commands.add_parser("accept")
    accept.add_argument("--kb", type=Path, required=True)
    accept.add_argument("--job-id", required=True)
    args = parser.parse_args()
    if args.command == "doctor":
        result = reading_capabilities(KIT)
    elif args.command == "install":
        engines = ("codex", "workbuddy") if args.engine == "both" else (args.engine,)
        if args.target and args.engine == "both":
            parser.error("--target需指定单一engine")
        result = [install_reading_kit(KIT, args.target or default_kit(args.project.resolve(), engine)) for engine in engines]
    elif args.command == "task":
        result = prepare_reading_task(args.kb, args.job_id, args.engine, KIT, handoff=args.handoff)
    elif args.command == "prepare":
        workflow = ReviewOrchestrator(args.kb.resolve() / "review-jobs")
        job = workflow.prepare_paper(args.paper_id, args.pdf, zotero_key=args.zotero_key)
        if job["state"] == "source_ready":
            workflow.dispatch_reading_batch([job["job_id"]], {job["job_id"]: route_skills("read", paper_type=args.paper_type).skills})
        result = prepare_reading_task(args.kb, job["job_id"], args.engine, KIT)
    elif args.command == "render":
        result = render_reading_output(args.task)
    elif args.command == "extract":
        result = extract_reading_source(args.task)
    else:
        result = accept_reading_output(args.kb, args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.command == "doctor" and not result["ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
