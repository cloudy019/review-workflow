"""A single CLI shared by human review, agent hosts and API execution."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import ReviewWorkflowError
from .providers import CommandReader, OpenAICompatibleReader
from .review_server import make_server
from .workflow import Workflow


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="evidence-loop")
    parser.add_argument("--workspace", default="workspace", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    add = sub.add_parser("add-paper")
    add.add_argument("paper_id")
    add.add_argument("--pdf", type=Path)
    add.add_argument("--context", default="")
    add.add_argument("--source-key")
    batch = sub.add_parser("start-batch")
    batch.add_argument("job_ids", nargs="+")
    issue = sub.add_parser("issue")
    issue.add_argument("job_id")
    issue.add_argument("--reader", default="external-agent")
    issue.add_argument("--handoff", action="store_true")
    submit = sub.add_parser("submit")
    submit.add_argument("job_id")
    submit.add_argument("attempt_id")
    submit.add_argument("result", type=Path)
    api = sub.add_parser("read-api")
    api.add_argument("job_id")
    api.add_argument("--base-url", default="https://api.deepseek.com")
    api.add_argument("--model", required=True)
    api.add_argument("--key-env", default="DEEPSEEK_API_KEY")
    api.add_argument("--chunk-chars", default=18000, type=int)
    api.add_argument("--max-tokens", default=12000, type=int)
    api.add_argument("--handoff", action="store_true")
    command = sub.add_parser("read-command")
    command.add_argument("job_id")
    command.add_argument("--config", required=True, type=Path)
    command.add_argument("--handoff", action="store_true")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", default=0, type=int)
    sub.add_parser("status")
    install = sub.add_parser("install-agent")
    install.add_argument("--target", type=Path, required=True)
    review = sub.add_parser("import-review")
    review.add_argument("review", type=Path)
    ai_review = sub.add_parser("verify-ai")
    ai_review.add_argument("job_id")
    ai_review.add_argument("review", type=Path)
    ai_inputs = sub.add_parser("ai-review-input")
    ai_inputs.add_argument("job_id")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("job_id")
    reopen = sub.add_parser("reopen")
    reopen.add_argument("job_id")
    reopen.add_argument("--pdf", type=Path)
    knowledge = sub.add_parser("knowledge")
    knowledge.add_argument("--approved-only", action="store_true")
    relocate = sub.add_parser("relocate-source")
    relocate.add_argument("job_id")
    relocate.add_argument("pdf", type=Path)
    blocked = sub.add_parser("block-source")
    blocked.add_argument("job_id")
    blocked.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    flow = Workflow(args.workspace)
    try:
        if args.command == "init":
            result = {"workspace": str(flow.workspace), "protocol": "1.0"}
        elif args.command == "add-paper":
            result = flow.add(args.paper_id, args.pdf, context=args.context, source_key=args.source_key)
        elif args.command == "start-batch":
            result = flow.start_batch(args.job_ids)
        elif args.command == "issue":
            result = {"request": str(flow.issue(args.job_id, reader=args.reader, handoff=args.handoff))}
        elif args.command == "submit":
            result = flow.submit(args.job_id, args.attempt_id, json.loads(args.result.read_text(encoding="utf-8")))
        elif args.command == "read-api":
            reader = OpenAICompatibleReader(base_url=args.base_url, model=args.model, api_key_env=args.key_env,
                                            chunk_chars=args.chunk_chars, max_tokens=args.max_tokens,
                                            progress=lambda msg: print(msg, file=sys.stderr, flush=True))
            path = flow.issue(args.job_id, reader=reader.name, handoff=args.handoff)
            request = json.loads(path.read_text(encoding="utf-8"))
            result = flow.submit(args.job_id, request["attempt_id"], reader.generate(request))
        elif args.command == "read-command":
            config = json.loads(args.config.read_text(encoding="utf-8"))
            reader = CommandReader(config["command"], name=config.get("name", "command-reader"),
                                   timeout=config.get("timeout", 300), cwd=config.get("cwd"))
            path = flow.issue(args.job_id, reader=reader.name, handoff=args.handoff)
            request = json.loads(path.read_text(encoding="utf-8"))
            result = flow.submit(args.job_id, request["attempt_id"], reader.generate(request))
        elif args.command == "serve":
            server = make_server(flow.workspace, port=args.port)
            print(f"http://127.0.0.1:{server.server_address[1]}/?token={server.app.token}", flush=True)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        elif args.command == "status":
            result = flow.core.list_jobs()
        elif args.command == "install-agent":
            source = Path(__file__).parent / "skills" / "evidence-loop" / "SKILL.md"
            target = args.target.expanduser().resolve() / "evidence-loop" / "SKILL.md"
            if target.exists() and target.read_bytes() != source.read_bytes():
                raise ValueError("Existing skill differs; preserve it before updating")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            result = {"skill": str(target), "runtime": "evidence_loop"}
        elif args.command == "import-review":
            result = flow.core.import_human_review(args.review)
        elif args.command == "verify-ai":
            result = flow.core.import_ai_review(args.job_id, args.review)
            from .registry import write_review_registry
            write_review_registry(flow.workspace)
        elif args.command == "ai-review-input":
            result = flow.core.ai_review_bindings(args.job_id)
        elif args.command == "revoke":
            result = flow.core.transition(args.job_id, "rescinded", "human permission revoked")
        elif args.command == "reopen":
            result = flow.reopen(args.job_id, args.pdf)
        elif args.command == "knowledge":
            result = flow.knowledge(approved_only=args.approved_only)
        elif args.command == "relocate-source":
            result = flow.relocate_source(args.job_id, args.pdf)
        elif args.command == "block-source":
            result = flow.core.transition(args.job_id, "blocked_source", args.reason)
        else:
            parser.error("unknown command")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ReviewWorkflowError, ValueError, OSError, TimeoutError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
