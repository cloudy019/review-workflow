"""File-backed handoff from the review app to Tencent WorkBuddy."""

from __future__ import annotations

import json
import os
import ctypes
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from .registry import build_review_registry, write_review_registry
from .attachment_index import refresh_index
from evidence_loop.locking import store_lock


SCHEMA_VERSION = "review_workflow.workbuddy_request.v1"


class WorkBuddyBridgeError(RuntimeError):
    pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bridge_dir(kb: Path) -> Path:
    return kb / "review-app" / "workbuddy"


def _pid_is_running(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid < 1:
        return False
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        return False
    return True


def request_queue_refresh(kb: str | Path) -> tuple[Path, dict[str, Any]]:
    """Write one durable refresh request derived from the project ledger."""

    root = Path(kb).expanduser().resolve()
    started = time.perf_counter()
    write_review_registry(root)
    registry = build_review_registry(root)
    source = root / "candidates" / "papers-screened.json"
    _, attachment_stats = refresh_index(root, source) if source.is_file() else ({}, {"total_papers": registry["managed_paper_count"], "cache_hit": 0, "missing_pdf_key": 0, "zotero_queries": 0})
    attachment_stats["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    request_path = _bridge_dir(root) / "inbox" / "queue-refresh.request.json"
    current = _read_json(request_path)
    if current.get("status") == "processing":
        return request_path.resolve(), current
    if current.get("status") == "pending":
        current["attachment_stats"] = attachment_stats
        current["counts"] = {
            "total": registry["managed_paper_count"],
            "reviewed": registry["reviewed_count"],
            "unreviewed": registry["unreviewed_count"],
            "active_jobs": registry["active_job_count"],
        }
        _atomic_json(request_path, current)
        return request_path.resolve(), current

    request = {
        "schema_version": SCHEMA_VERSION,
        "request_id": f"wb-{uuid4().hex[:16]}",
        "status": "pending",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": "review_app",
        "project_root": str(root.parent),
        "knowledge_base": str(root),
        "library_json": str((root / "review-app" / "library.json").resolve()),
        "reading_ledger": str((root / "review-app" / "论文文件总索引.md").resolve()),
        "instructions": str(
            (root.parent / "review_workflow" / "WORKBUDDY_QUEUE.md").resolve()
        ),
        "source_pool": str(
            (
                root
                / "candidates"
                / "papers-screened.json"
            ).resolve()
        ),
        "max_batch_size": 3,
        "paper_limit_per_agent": 1,
        "counts": {
            "total": registry["managed_paper_count"],
            "reviewed": registry["reviewed_count"],
            "unreviewed": registry["unreviewed_count"],
            "active_jobs": registry["active_job_count"],
        },
        "attachment_stats": attachment_stats,
        "attachment_index": str((root / "screening" / "zotero-attachment-index.json").resolve()),
    }
    _atomic_json(request_path, request)
    return request_path.resolve(), request


def _default_config(kb: Path) -> dict[str, Any]:
    home = Path.home()
    node_candidates = sorted(
        (home / ".workbuddy" / "binaries" / "node" / "versions").glob("*/node.exe"),
        reverse=True,
    )
    cli_candidates = []
    configured = os.environ.get("WORKBUDDY_CLI")
    if configured:
        cli_candidates.append(Path(configured))
    command = shutil.which("codebuddy")
    if command:
        cli_candidates.append(Path(command))
    cli_candidates.extend(
        [
            Path("E:/workbuddy/resources/app.asar.unpacked/cli/bin/codebuddy"),
            Path("C:/Program Files/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy"),
        ]
    )
    node = next((path for path in node_candidates if path.is_file()), None)
    cli = next((path for path in cli_candidates if path.is_file()), None)
    return {
        "schema_version": "review_workflow.workbuddy_bridge.v1",
        "node_path": str(node) if node else None,
        "cli_path": str(cli) if cli else None,
        "model_id": None,
        "allowed_tools": [],
        "api_credentials": "由WorkBuddy模型设置管理；本项目不保存密钥",
    }


def ensure_bridge_config(kb: str | Path) -> tuple[Path, dict[str, Any]]:
    root = Path(kb).expanduser().resolve()
    path = _bridge_dir(root) / "workbuddy-bridge.json"
    config = _read_json(path)
    if not config:
        config = _default_config(root)
        _atomic_json(path, config)
    return path.resolve(), config


def launch_workbuddy(kb: str | Path, request_path: str | Path) -> dict[str, Any]:
    with store_lock(_bridge_dir(Path(kb).expanduser().resolve())):
        return _launch_workbuddy(kb, request_path)


def _launch_workbuddy(kb: str | Path, request_path: str | Path) -> dict[str, Any]:
    """Start one non-interactive WorkBuddy orchestrator; it delegates PDFs singly."""

    root = Path(kb).expanduser().resolve()
    request_file = Path(request_path).expanduser().resolve()
    config_path, config = ensure_bridge_config(root)
    node = Path(str(config.get("node_path") or ""))
    cli = Path(str(config.get("cli_path") or ""))
    if not node.is_file() or not cli.is_file():
        raise WorkBuddyBridgeError(
            f"找不到WorkBuddy命令行组件，请检查{config_path}中的路径"
        )

    prompt = (
        "这是论文评审队列维护任务。先读取且严格遵守 "
        f"{root.parent / 'AGENTS.md'}、{root.parent / 'review_workflow' / 'WORKBUDDY_QUEUE.md'} "
        f"和请求文件 {request_file}。单篇AI阅读使用本项目 "
        f"{root.parent / '.workbuddy' / 'skills' / 'evidence-loop' / 'SKILL.md'}，"
        "运行同一个Evidence Loop框架，不依赖Codex子代理，也不得另建状态机。"
        "必须以项目台账判断已读/未读；一次只给一个代理一篇论文和一个PDF。"
        "没有子代理功能时由当前模型逐篇执行；主任务串行登记产物。"
    )
    arguments = [
        str(node),
        str(cli),
        "--print",
        "--output-format",
        "stream-json",
    ]
    model_id = str(config.get("model_id") or "").strip()
    if model_id:
        arguments.extend(["--model", model_id])
    allowed_tools = config.get("allowed_tools") or []
    if not isinstance(allowed_tools, list) or any(not isinstance(name, str) or not name.strip() for name in allowed_tools):
        raise WorkBuddyBridgeError("allowed_tools需要用户明确配置的工具权限字符串列表")
    if allowed_tools:
        arguments.extend(["--allowedTools", ",".join(allowed_tools)])
    # Variadic CLI options must not consume the prompt as another option value.
    arguments.append("--")
    arguments.append(prompt)
    if not allowed_tools:
        # Default execution does not ask a noninteractive host to perform file
        # I/O. The shared core runs locally; the host model returns JSON only.
        arguments = [sys.executable, "-m", "review_workflow.framework_worker", "--kb", str(root), "--request", str(request_file)]

    runtime = _bridge_dir(root) / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    request = _read_json(request_file)
    log_path = runtime / f"{request.get('request_id', 'workbuddy')}.log"
    log_handle = log_path.open("ab")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            arguments,
            cwd=str(root.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        log_handle.close()
    request.update(
        {
            "workbuddy_pid": process.pid,
            "workbuddy_log": str(log_path.resolve()),
            "launched_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    _atomic_json(request_file, request)
    return {
        "request": str(request_file),
        "status": request.get("status"),
        "workbuddy_pid": process.pid,
        "workbuddy_log": str(log_path.resolve()),
    }


__all__ = [
    "SCHEMA_VERSION",
    "WorkBuddyBridgeError",
    "ensure_bridge_config",
    "launch_workbuddy",
    "request_queue_refresh",
    "_pid_is_running",
]
