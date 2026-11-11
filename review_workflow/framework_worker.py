"""Project queue adapter: Python owns I/O, WorkBuddy only returns reading JSON."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from evidence_loop.locking import store_lock
from evidence_loop.providers import CommandReader
from evidence_loop.registry import write_review_registry
from evidence_loop.workflow import Workflow

from .workbuddy_bridge import _atomic_json, _read_json, ensure_bridge_config


def run(kb: Path, request_path: Path):
    from .cli import _cmd_auto_start, _cmd_import
    root = kb.resolve()
    flow = Workflow(root)
    with store_lock(root / "review-app" / "workbuddy"):
        request = _read_json(request_path)
        request.update(status="processing", started_at=datetime.now(timezone.utc).isoformat())
        _atomic_json(request_path, request)
        try:
            # Human browser saves are already applied by the core. Materialize
            # project-specific cards through the existing serial adapter.
            for job in flow.core.list_jobs():
                review = Path(job["job_dir"]) / "human_reviews" / f"{job['zotero_key']}.human-review.json"
                if review.is_file() and job["state"] in {"human_reviewing", "needs_clarification", "human_confirmed", "indexed", "synthesis_eligible"}:
                    _cmd_import(argparse.Namespace(kb=root, review=review))
            open_batches = [flow.core.get_batch(path.stem) for path in flow.core.batches_dir.glob("batch-*.json")]
            open_batches = [batch for batch in open_batches if batch["status"] == "open"]
            if not open_batches:
                selected = _cmd_auto_start(argparse.Namespace(kb=root, source=Path(request["source_pool"]), max_attempts=20, max_papers=3))
                batch_id = selected["reading_batch"]["batch_id"]
                open_batches = [flow.core.get_batch(batch_id)]
            request["batch_ids"] = [batch["batch_id"] for batch in open_batches]
            jobs = [flow.core.get_job(task["job_id"]) for batch in open_batches for task in batch["agent_tasks"]]
            _, config = ensure_bridge_config(root)
            command = [config["node_path"], config["cli_path"], "--print", "--output-format", "json", "--tools", "", "--no-session-persistence", "--max-turns", "2",
                       "--system-prompt", "You are a scientific reading assistant. Fill the exact JSON result contract from the supplied source. Never call tools or delegate. Return only JSON."]
            if config.get("model_id"):
                command.extend(["--model", config["model_id"]])
            reader = CommandReader(command, name="workbuddy", timeout=600, cwd=str(root.parent))
            completed = []
            for job in jobs:
                if job["state"] != "reading":
                    continue
                path = flow.issue(job["job_id"], reader=reader.name, handoff=bool(job.get("reader_attempt")))
                reading_request = json.loads(path.read_text(encoding="utf-8"))
                result = reader.generate(reading_request)
                finished = flow.submit(job["job_id"], reading_request["attempt_id"], result)
                completed.append({"job_id": job["job_id"], "state": finished["state"], "review_html": str(Path(finished["agent_output_dir"]) / "paper-review.html")})
                request["completed_reading"] = completed
                _atomic_json(request_path, request)
            write_review_registry(root)
            request.update(status="waiting_human_review", completed_reading=completed,
                           completed_at=datetime.now(timezone.utc).isoformat(),
                           execution_mode="evidence-loop-stdio", model_tools_enabled=False)
        except Exception as exc:
            request.update(status="blocked", error=str(exc), completed_at=datetime.now(timezone.utc).isoformat())
        _atomic_json(request_path, request)
        return request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.kb, args.request)
    print(json.dumps({"status": result["status"], "execution_mode": result.get("execution_mode"), "error": result.get("error")}, ensure_ascii=False))
    return int(result["status"] == "blocked")


if __name__ == "__main__":
    raise SystemExit(main())
