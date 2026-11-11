from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


PLUGIN_GLOB = "plugins/cache/openai-curated-remote/zotero/*/skills/zotero/scripts/zotero.py"


class ZoteroPluginError(RuntimeError):
    pass


def locate_plugin_helper(codex_home: Path | None = None) -> Path:
    root = codex_home or Path.home() / ".codex"
    matches = sorted(root.glob(PLUGIN_GLOB))
    if not matches:
        raise ZoteroPluginError("Codex built-in Zotero plugin helper was not found")
    return matches[-1]


def run_plugin(
    command: str,
    *args: str,
    helper: Path | None = None,
    timeout: int = 60,
) -> str:
    """Run a supported Zotero plugin command.

    Library writes are intentionally limited to the plugin's import commands.
    Existing-item collection membership remains a human Zotero action.
    """

    allowed = {
        "status", "enable", "restart", "probe", "inventory", "collections", "tags", "search",
        "export-bibtex", "sync-bib", "citations", "children", "fulltext",
        "file-url", "cite", "selected-target", "import-bibtex", "import-ris",
    }
    if command not in allowed:
        raise ZoteroPluginError(f"unsupported plugin command: {command}")
    path = helper or locate_plugin_helper()
    completed = subprocess.run(
        [sys.executable, str(path), command, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ZoteroPluginError(detail or f"Zotero plugin failed: {command}")
    return completed.stdout


def plugin_json(command: str, *args: str, helper: Path | None = None) -> Any:
    call_args = list(args)
    if "--json" not in call_args:
        call_args.append("--json")
    return json.loads(run_plugin(command, *call_args, helper=helper))


def attachment_file_path(
    attachment_key: str, *, helper: Path | None = None
) -> Path:
    """Resolve one attachment through the built-in plugin's ``file-url`` route."""

    if not attachment_key or any(separator in attachment_key for separator in " ,;|"):
        raise ValueError("attachment_key must identify exactly one attachment")
    raw = run_plugin("file-url", attachment_key, helper=helper).strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "file":
        raise ZoteroPluginError(f"Zotero did not return a local file URL: {raw}")
    local = Path(url2pathname(unquote(parsed.path)))
    if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
        local = Path(f"//{parsed.netloc}{url2pathname(unquote(parsed.path))}")
    if not local.is_file():
        raise ZoteroPluginError(f"Zotero attachment file is unavailable: {local}")
    return local.resolve()


def _load_plugin_module(helper: Path | None = None) -> ModuleType:
    path = helper or locate_plugin_helper()
    spec = importlib.util.spec_from_file_location("codex_zotero_plugin_helper", path)
    if spec is None or spec.loader is None:
        raise ZoteroPluginError("Could not load Zotero plugin helper")
    module = importlib.util.module_from_spec(spec)
    # The helper defines dataclasses, which inspect sys.modules while loading.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def verify_collection_membership(
    collection_key: str,
    item_key: str,
    *,
    helper: Path | None = None,
) -> bool:
    """Read collection members through the built-in plugin's API layer.

    This is read-only. Moving an existing item into a collection is deliberately
    left to the user in Zotero Desktop because plugin v0.1.2 has no such command.
    """

    if not collection_key or not item_key:
        raise ValueError("collection_key and item_key are required")
    module = _load_plugin_module(helper)
    start = 0
    while True:
        rows = module.api_get(
            f"{module.LOCAL_USER}/collections/{collection_key}/items/top?limit=100&start={start}"
        )
        if not isinstance(rows, list):
            raise ZoteroPluginError("Unexpected collection response from plugin")
        if any(row.get("key") == item_key for row in rows):
            return True
        if len(rows) < 100:
            return False
        start += len(rows)
