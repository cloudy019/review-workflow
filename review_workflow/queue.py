"""Candidate-pool screening, source verification, and automatic reservations."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from .candidate_sources import verify_candidate_source
from .screening import RUBRIC_VERSION, score_candidate, select_next_batch


class QueueError(RuntimeError):
    pass


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QueueError(f"无法读取JSON：{path}") from exc


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def source_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_candidate_pool(source: Path) -> dict[str, Any]:
    """Score a candidate file by invoking the strict scorer once per record."""

    payload = _load_json(source)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("records"), list):
        raise QueueError("候选来源必须包含records数组")
    results: list[dict[str, Any]] = []
    for record in payload["records"]:
        if not isinstance(record, Mapping):
            raise QueueError("候选records只能包含单篇对象")
        result = score_candidate(record)
        result["candidate_record_sha256"] = hashlib.sha256(
            json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        results.append(result)
    keys = [item.get("zotero_key") for item in results]
    if any(not isinstance(key, str) or not key for key in keys):
        raise QueueError("正式候选池中的每篇论文必须有Zotero Key")
    if len(set(keys)) != len(keys):
        raise QueueError("候选池包含重复Zotero Key")
    return {
        "schema_version": "review_workflow.candidate_pool.v1",
        "rubric_version": RUBRIC_VERSION,
        "source": str(source.resolve()),
        "source_sha256": source_hash(source),
        "screening_status": "provisional",
        "record_count": len(results),
        "results": results,
    }


def _priority(result: Mapping[str, Any]) -> tuple[Any, ...]:
    priority = result.get("fulltext_priority")
    safe_priority = priority if isinstance(priority, int) else 99
    return (-float(result.get("score") or 0), safe_priority, str(result["paper_id"]).casefold())


def verify_pool_sources(
    pool: Mapping[str, Any],
    *,
    max_attempts: int = 24,
    helper: Path | None = None,
) -> dict[str, Any]:
    """Mechanically verify ranked Zotero attachments, one paper per call."""

    if not isinstance(pool, Mapping) or not isinstance(pool.get("results"), list):
        raise QueueError("candidate pool格式无效")
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise QueueError("max_attempts必须是正整数")
    updated = deepcopy(dict(pool))
    results = [dict(item) for item in updated["results"]]
    index = {item["paper_id"]: number for number, item in enumerate(results)}
    attempts = 0
    for candidate in sorted(results, key=_priority):
        if attempts >= max_attempts:
            break
        if candidate.get("score", 0) < 7 or not candidate.get("attachment_keys"):
            continue
        if candidate.get("source_verified") is True:
            continue
        attempts += 1
        verified = verify_candidate_source(candidate, helper=helper)
        results[index[candidate["paper_id"]]] = verified
        decision = select_next_batch(
            results, max_papers=3, require_verified_source=True
        )
        if decision["selected_count"] == 3 and set(decision["covered_routes"]) == {
            "method",
            "materials",
            "battery",
        }:
            break
    updated["results"] = results
    updated["source_verification"] = {
        "attempted_count": attempts,
        "verified_count": sum(item.get("source_verified") is True for item in results),
        "failed_count": sum(
            item.get("source_verification_error") not in (None, "") for item in results
        ),
        "via": "codex-built-in-zotero-plugin",
    }
    return updated


_LOCK_ACQUIRE_ATTEMPTS = 3


def _pid_alive(pid: int) -> bool:
    """判断进程是否存活；无法判定时保守返回 True（视为仍占用锁）。"""

    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION 足以探测进程是否存在。
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            # ERROR_ACCESS_DENIED(5)：进程存在但当前用户无权查询。
            return kernel32.GetLastError() == 5
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _read_lock_pid(path: Path) -> int | None:
    """读出锁文件记录的持有者 PID；读不出返回 None。"""

    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    if not text.isdigit():
        return None
    return int(text)


def _stale_lock_dir(root: Path) -> Path:
    """残留锁的归档目录：项目级 .workbuddy/trash/queue-locks。

    只认 root 所在的项目，不回头看当前工作目录，避免把别处的锁归档到
    无关项目里。
    """

    project = root.parent.parent
    if (project / ".workbuddy").is_dir():
        return project / ".workbuddy" / "trash" / "queue-locks"
    return root / "stale-locks"


def _reclaim_lock(path: Path, root: Path) -> None:
    """移除一个已确认无人持有的残留锁。

    `unlink` 在部分环境（如 WorkBuddy 沙箱的回收站策略）会被 fail-closed
    拦截，但 `rename` 通常仍然放行，因此这里逐级降级：先删，删不掉就移到
    项目 trash，再不行就在原地改名，最后才报错让人工处理。
    """

    try:
        path.unlink()
        return
    except OSError:
        pass
    archive = _stale_lock_dir(root)
    try:
        archive.mkdir(parents=True, exist_ok=True)
        os.replace(path, archive / f"selection.lock.stale-{_read_lock_pid(path) or 'unknown'}")
        return
    except OSError:
        pass
    try:
        os.replace(path, path.with_name(f".selection.lock.stale-{_read_lock_pid(path) or 'unknown'}"))
        return
    except OSError as rename_exc:
        raise QueueError(
            "选批锁残留且无法清理（删除与移动都被环境策略拦截），"
            f"请手动删除 {path} 后重试"
        ) from rename_exc


def _acquire_lock(path: Path, root: Path) -> int:
    """获取互斥锁；持有进程已消亡的残留锁回收后重试。

    正常路径只有一种失败原因：另一个活进程正持有锁。但锁清理在部分环境
    （如 WorkBuddy 沙箱的回收站策略）会抛 OSError，导致锁残留、随后每次
    运行都被 FileExistsError 拦住，而报错信息里的"正在进行"是假的。此处
    只在能证明持有者已退出时才回收锁，真正的并发仍然立刻失败。
    """

    error: OSError | None = None
    for _ in range(_LOCK_ACQUIRE_ATTEMPTS):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            error = exc
            owner = _read_lock_pid(path)
            if owner is None or _pid_alive(owner):
                raise QueueError("已有自动选批或启动操作正在进行") from exc
            _reclaim_lock(path, root)
    raise QueueError(f"无法获取选批锁：{path}") from error


@contextmanager
def queue_lock(root: Path):
    """Fail fast if another live process is selecting or starting a batch."""

    root.mkdir(parents=True, exist_ok=True)
    path = root / ".selection.lock"
    descriptor = _acquire_lock(path, root)
    try:
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
        finally:
            os.close(descriptor)
        yield
    finally:
        # 清理失败不应把已成功的选批/启动报成失败：受沙箱回收站策略等影响，
        # unlink 可能抛 OSError；此时业务已执行完成，残留锁由下次运行按
        # 持有者 PID 是否存活自动回收。
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def reserve_next_batch(
    pool: Mapping[str, Any],
    *,
    existing_tasks: Iterable[Mapping[str, Any]] = (),
    reservation_path: Path,
    max_papers: int = 3,
) -> dict[str, Any]:
    """Automatically reserve a source-verified portfolio; no paper IDs accepted."""

    if reservation_path.is_file():
        existing = _load_json(reservation_path)
        if isinstance(existing, Mapping) and existing.get("status") == "reserved":
            return dict(existing)
    decision = select_next_batch(
        pool.get("results", []),
        existing_tasks=list(existing_tasks),
        max_papers=max_papers,
        require_verified_source=True,
    )
    if not decision["selected"]:
        raise QueueError("没有已通过来源核验且达到阈值的候选论文")
    selected_keys = [item["zotero_key"] for item in decision["selected"]]
    if len(set(selected_keys)) != len(selected_keys):
        raise QueueError("自动选批结果包含重复Zotero Key")
    pool_digest = str(pool.get("source_sha256") or "")
    identity = hashlib.sha256(
        (pool_digest + "|" + "|".join(selected_keys)).encode("utf-8")
    ).hexdigest()[:16]
    reservation = {
        "schema_version": "review_workflow.selection.v1",
        "selection_id": f"selection-{identity}",
        "status": "reserved",
        "pool_sha256": pool_digest,
        "rubric_version": decision["rubric_version"],
        "selected": decision["selected"],
        "covered_axes": decision["covered_axes"],
        "covered_routes": decision["covered_routes"],
        "batch_id": None,
    }
    atomic_json(reservation_path, reservation)
    return reservation


def selection_markdown(reservation: Mapping[str, Any]) -> str:
    lines = [
        "# AI自动选择的下一阅读批次",
        "",
        "> 论文由冻结筛选标准自动排序；用户不需要逐篇挑选。旧筛选信息只作初筛先验。",
        "",
    ]
    for number, item in enumerate(reservation.get("selected", []), 1):
        lines.extend(
            [
                f"## {number}. {item.get('title') or item.get('paper_id')}",
                "",
                f"- Zotero Key：`{item.get('zotero_key')}`",
                f"- 初筛分数：{item.get('score')}/10",
                f"- 方向：{'、'.join(item.get('axes') or []) or '待分类'}",
                f"- 入选理由：{item.get('selection_reason')}",
                f"- PDF：`{item.get('verified_pdf_path')}`",
                "- 引用许可：无；须完成八节人工确认后单独授予。",
                "",
            ]
        )
    return "\n".join(lines)


__all__ = [
    "QueueError",
    "atomic_json",
    "build_candidate_pool",
    "queue_lock",
    "reserve_next_batch",
    "selection_markdown",
    "source_hash",
    "verify_pool_sources",
]
