import json
import os
from pathlib import Path

from review_workflow import queue


def _record(key, title, abstract, role, attachment):
    return {
        "zotero_key": key,
        "title": title,
        "abstract": abstract,
        "paper_role": role,
        "attachment_details": {"attachments": [{"key": attachment}]},
        "signals": {"rule": True, "validation": True},
        "fulltext_priority": 1,
    }


def test_pool_is_scored_one_record_at_a_time_and_reserved_automatically(tmp_path: Path, monkeypatch):
    source = tmp_path / "candidates.json"
    source.write_text(
        json.dumps(
            {
                "records": [
                    _record("METHOD01", "SHAP rule extraction", "Explainable method and rule.", "computer_science_method", "ATTACH01"),
                    _record("MATERI01", "Materials descriptor", "Materials design descriptor.", "materials_application", "ATTACH02"),
                    _record("BATTERY1", "Battery mechanism", "Lithium-ion battery mechanism validation.", "battery_application", "ATTACH03"),
                ]
            }
        ),
        encoding="utf-8",
    )
    pool = queue.build_candidate_pool(source)
    pdfs = {}
    for number, key in enumerate(("ATTACH01", "ATTACH02", "ATTACH03"), 1):
        path = tmp_path / f"{number}.pdf"
        path.write_bytes(b"%PDF-1.4\n" + key.encode())
        pdfs[key] = path
    monkeypatch.setattr(
        "review_workflow.candidate_sources.attachment_file_path",
        lambda key, helper=None: pdfs[key],
    )
    verified = queue.verify_pool_sources(pool, max_attempts=3)
    reservation_path = tmp_path / "next-batch.json"
    reservation = queue.reserve_next_batch(
        verified, reservation_path=reservation_path
    )
    assert len(reservation["selected"]) == 3
    assert {item["zotero_key"] for item in reservation["selected"]} == {
        "METHOD01",
        "MATERI01",
        "BATTERY1",
    }
    assert all(item["source_verified"] for item in reservation["selected"])
    assert queue.reserve_next_batch(verified, reservation_path=reservation_path) == reservation


def test_queue_lock_rejects_concurrent_selection(tmp_path: Path):
    with queue.queue_lock(tmp_path):
        try:
            with queue.queue_lock(tmp_path):
                raise AssertionError("lock should fail")
        except queue.QueueError:
            pass


def test_queue_lock_reclaims_lock_whose_owner_exited(tmp_path: Path):
    """持有者已消亡的残留锁应被回收，而不是永远拦住后续运行。"""

    path = tmp_path / ".selection.lock"
    # 找一个几乎不可能存在的 PID：现有 PID 上界加一。
    dead_pid = 0
    for candidate in range(1, 1 << 22):
        if not queue._pid_alive(candidate):
            dead_pid = candidate
            break
    assert dead_pid, "应能找到一个不存活的 PID"
    path.write_text(str(dead_pid), encoding="ascii")

    with queue.queue_lock(tmp_path):
        assert queue._read_lock_pid(path) == os.getpid()


def test_queue_lock_rejects_unreadable_pid(tmp_path: Path):
    """读不出持有者 PID 时保守视为仍被占用，不擅自回收。"""

    path = tmp_path / ".selection.lock"
    path.write_text("not-a-pid", encoding="ascii")
    try:
        with queue.queue_lock(tmp_path):
            raise AssertionError("lock should fail")
    except queue.QueueError:
        pass


def test_queue_lock_tolerates_unlink_failure(tmp_path: Path, monkeypatch):
    """清理锁失败不应把已成功的业务报成失败（沙箱回收站策略场景）。"""

    path = tmp_path / ".selection.lock"

    def exploding_unlink(*args, **kwargs):
        raise OSError("[safe-delete][SAFE_DELETE_FAIL_CLOSED]")

    monkeypatch.setattr(type(path), "unlink", exploding_unlink)
    with queue.queue_lock(tmp_path):
        pass
    assert path.exists()


def test_reclaim_lock_falls_back_to_rename_when_unlink_blocked(tmp_path: Path, monkeypatch):
    """unlink 被沙箱策略拦截时应降级为 rename 归档，而不是直接失败。"""

    root = tmp_path / "review-jobs"
    root.mkdir()
    path = root / ".selection.lock"
    path.write_text("4242", encoding="ascii")
    monkeypatch.setattr(
        type(path), "unlink", lambda *a, **k: (_ for _ in ()).throw(OSError("blocked"))
    )
    queue._reclaim_lock(path, root)
    assert not path.exists()
    archived = list((root / "stale-locks").glob("selection.lock.stale-*"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="ascii") == "4242"


def test_reclaim_lock_reports_when_both_delete_and_move_blocked(tmp_path: Path, monkeypatch):
    """删除与移动都被拦截时给出明确报错，而不是假装拿到锁。"""

    root = tmp_path / "review-jobs"
    root.mkdir()
    path = root / ".selection.lock"
    path.write_text("4242", encoding="ascii")

    def blocked(*args, **kwargs):
        raise OSError("blocked")

    monkeypatch.setattr(type(path), "unlink", blocked)
    monkeypatch.setattr(queue.os, "replace", blocked)
    try:
        queue._reclaim_lock(path, root)
        raise AssertionError("should fail")
    except queue.QueueError as exc:
        assert "手动删除" in str(exc)


def test_pid_alive_recognises_self_and_dead_pid():
    assert queue._pid_alive(os.getpid()) is True
    assert queue._pid_alive(0) is False
    assert queue._pid_alive(-1) is False
    dead = next(c for c in range(1, 1 << 22) if not queue._pid_alive(c))
    assert queue._pid_alive(dead) is False
