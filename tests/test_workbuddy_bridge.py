import json
from pathlib import Path
from unittest.mock import patch

from review_workflow.workbuddy_bridge import ensure_bridge_config, launch_workbuddy, request_queue_refresh
from review_workflow.attachment_index import backfill_missing, index_path


def test_workbuddy_request_is_derived_from_persistent_ledger(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    card = kb / "papers" / "UNREAD01" / "paper.json"
    card.parent.mkdir(parents=True)
    card.write_text(
        json.dumps(
            {
                "zotero_key": "UNREAD01",
                "state": "legacy_unconfirmed",
                "identity": {"title": "Unread paper"},
            }
        ),
        encoding="utf-8",
    )

    path, request = request_queue_refresh(kb)

    assert path.name == "queue-refresh.request.json"
    assert request["status"] == "pending"
    assert request["paper_limit_per_agent"] == 1
    assert request["counts"] == {
        "total": 1,
        "reviewed": 0,
        "unreviewed": 1,
        "active_jobs": 0,
    }
    assert Path(request["library_json"]).is_file()
    assert Path(request["reading_ledger"]).is_file()


def test_bridge_config_never_contains_api_key(tmp_path: Path):
    path, config = ensure_bridge_config(tmp_path / "文献知识库")

    assert path.is_file()
    assert "api_key" not in json.dumps(config).lower()
    assert "不保存密钥" in config["api_credentials"]


def test_bridge_transmits_only_explicit_permission_grants(tmp_path: Path):
    kb = tmp_path / "knowledge"
    config_path, config = ensure_bridge_config(kb)
    executable = tmp_path / "executable"
    executable.write_text("fixture")
    config.update({"node_path": str(executable), "cli_path": str(executable), "allowed_tools": ["Read", "Bash(python:*)", "Write"]})
    config_path.write_text(json.dumps(config), encoding="utf-8")
    request_path, _ = request_queue_refresh(kb)
    with patch("review_workflow.workbuddy_bridge.subprocess.Popen") as launch:
        launch.return_value.pid = 42
        launch_workbuddy(kb, request_path)
    arguments = launch.call_args.args[0]
    assert arguments[arguments.index("--allowedTools") + 1] == "Read,Bash(python:*),Write"
    assert arguments[-2] == "--"  # prompt cannot be swallowed by a variadic option
    assert "--dangerously-skip-permissions" not in arguments
    assert "bypassPermissions" not in arguments


def test_bridge_default_does_not_grant_tools(tmp_path: Path):
    kb = tmp_path / "knowledge"
    config_path, config = ensure_bridge_config(kb)
    assert config["allowed_tools"] == []
    executable = tmp_path / "executable"
    executable.write_text("fixture")
    config.update({"node_path": str(executable), "cli_path": str(executable)})
    config_path.write_text(json.dumps(config), encoding="utf-8")
    request_path, _ = request_queue_refresh(kb)
    with patch("review_workflow.workbuddy_bridge.subprocess.Popen") as launch:
        launch.return_value.pid = 42
        launch_workbuddy(kb, request_path)
    assert "--allowedTools" not in launch.call_args.args[0]
    assert "review_workflow.framework_worker" in launch.call_args.args[0]


def test_queue_refresh_continues_when_a_paper_has_no_pdf_key(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    source = kb / "candidates" / "papers-screened.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"records": [
        {"zotero_key": "WITHPDF1", "attachment_details": {"attachments": [{"key": "ATTACH01", "content_type": "application/pdf"}]}},
        {"zotero_key": "NOPDF001", "attachment_details": {"attachments": []}},
    ]}), encoding="utf-8")
    for key in ("WITHPDF1", "NOPDF001"):
        card = kb / "papers" / key / "paper.json"
        card.parent.mkdir(parents=True)
        card.write_text(json.dumps({"zotero_key": key, "state": "legacy_unconfirmed"}), encoding="utf-8")

    with patch("review_workflow.attachment_index._load_plugin_module", side_effect=AssertionError("refresh contacted Zotero")):
        path, request = request_queue_refresh(kb)
        _, repeat = request_queue_refresh(kb)

    assert path.is_file()
    assert request["attachment_stats"]["total_papers"] == 2
    assert request["attachment_stats"]["missing_pdf_key"] == 1
    assert request["attachment_stats"]["zotero_queries"] == 0
    assert repeat["attachment_stats"]["cache_hit"] == 1
    entries = json.loads(index_path(kb).read_text(encoding="utf-8"))["entries"]
    assert entries["WITHPDF1"]["pdf_attachment_key"] == "ATTACH01"
    assert entries["NOPDF001"]["status"] == "missing_pdf_key"


def test_pdf_key_backfill_only_queries_missing_items(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    path = index_path(kb)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"entries": {
        "WITHPDF1": {"pdf_attachment_key": "ATTACH01", "status": "available"},
        "NOPDF001": {"pdf_attachment_key": None, "status": "missing_pdf_key"},
    }}), encoding="utf-8")

    class Plugin:
        LOCAL_USER = "/users/0"
        calls = []

        @classmethod
        def api_get(cls, endpoint):
            cls.calls.append(endpoint)
            return [{"key": "ATTACH02", "data": {"contentType": "application/pdf"}}]

    with patch("review_workflow.attachment_index._load_plugin_module", return_value=Plugin):
        result = backfill_missing(kb)
    assert Plugin.calls == ["/users/0/items/NOPDF001/children"]
    assert result["zotero_queries"] == 1
    assert result["filled"] == 1
    assert result["missing_pdf_key"] == 0


def test_no_pdf_is_checked_once_until_explicit_retry(tmp_path: Path):
    kb = tmp_path / "文献知识库"
    path = index_path(kb)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"entries": {"NOPDF001": {"pdf_attachment_key": None}}}), encoding="utf-8")

    class Plugin:
        LOCAL_USER = "/users/0"
        calls = 0

        @classmethod
        def api_get(cls, endpoint):
            cls.calls += 1
            return []

    with patch("review_workflow.attachment_index._load_plugin_module", return_value=Plugin):
        first = backfill_missing(kb)
        second = backfill_missing(kb)
    assert first["missing_pdf_key"] == 1
    assert second["zotero_queries"] == 0
    assert Plugin.calls == 1
