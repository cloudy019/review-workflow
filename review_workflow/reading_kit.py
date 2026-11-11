"""Build and install a self-contained skill without project data or credentials."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from .core import ValidationError, sha256_file
from .registry import _atomic_json


KIT_NAME = "paper-review"
SKILLS = (
    "pdf", "nature-reader", "nature-shared", "deeppapernote", "ml-paper-writing",
    "scientific-writing", "literature-review", "nature-statistics",
    "statistical-analysis", "pymatgen",
)
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "evals", "tests", "templates", "node_modules")


def default_kit(project: Path, engine: str) -> Path:
    if engine == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills" / KIT_NAME
    if engine == "workbuddy":
        return project / ".workbuddy" / "skills" / KIT_NAME
    raise ValidationError(f"未知执行端：{engine}")


def locate_skill(skills_root: Path, name: str) -> Path:
    candidates = [skills_root / name, skills_root / "scientific-toolkit-skill" / "references" / "scientific-skills" / name]
    for path in candidates:
        if (path / "SKILL.md").is_file():
            return path
    raise ValidationError(f"未找到需要打包的技能：{name}")


def _manifest(root: Path) -> dict[str, Any]:
    files = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "kit-manifest.json" and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"schema_version": "review_workflow.reading_kit.v1", "name": KIT_NAME, "engines": ["codex", "workbuddy"], "skills": list(SKILLS), "files": files, "digest": digest}


def verify_kit(kit: str | Path) -> dict[str, Any]:
    root = Path(kit).resolve()
    recorded = json.loads((root / "kit-manifest.json").read_text(encoding="utf-8"))
    current = _manifest(root)
    if current["digest"] != recorded.get("digest") or current["files"] != recorded.get("files"):
        raise ValidationError("阅读能力包文件已变化，请重新构建后安装")
    return recorded


def build_reading_kit(destination: str | Path, skills_root: str | Path | None = None) -> dict[str, Any]:
    """Export only reusable resources; never walk the project/library/home tree."""
    root = Path(destination).expanduser().resolve()
    if root.exists() and any(root.iterdir()):
        raise ValidationError("导出目录必须为空，避免混入旧文件或私人资料")
    skills = Path(skills_root or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills")
    sources = {name: locate_skill(skills, name) for name in SKILLS}
    module = Path(__file__).parent
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(module / "reading_skill", root, dirs_exist_ok=True)
    for name, source in sources.items():
        shutil.copytree(source, root / "references" / "skills" / name, ignore=IGNORE)
    runtime = root / "references" / "runtime" / "review_workflow"
    runtime.mkdir(parents=True)
    # Compatibility exports carry the canonical package, never a second copy
    # of the old state machine. No private project files are traversed.
    import evidence_loop
    framework = Path(evidence_loop.__file__).parent
    shutil.copytree(framework, runtime.parent / "evidence_loop", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # A fixed allowlist prevents exporting local Zotero integration/configuration.
    for name in ("__init__.py", "skill_router.py", "core.py", "html_review.py", "registry.py", "reading.py", "reading_kit.py"):
        shutil.copy2(module / name, runtime / name)
    shutil.copytree(module / "templates", runtime / "templates")
    katex = module / "vendor" / "katex" / "node_modules" / "katex"
    for name in ("dist/katex.min.js", "LICENSE"):
        target = runtime / "vendor" / "katex" / "node_modules" / "katex" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(katex / name, target)
    lock = module / "dependencies.lock.json"
    shutil.copy2(lock, root / "references" / "source-versions.json")
    manifest = _manifest(root)
    _atomic_json(root / "kit-manifest.json", manifest)
    return {"kit": str(root), "digest": manifest["digest"], "files": len(manifest["files"]), "skills": list(SKILLS)}


def install_reading_kit(kit: str | Path, target: str | Path) -> dict[str, Any]:
    source, destination = Path(kit).resolve(), Path(target).expanduser().resolve()
    manifest = verify_kit(source)
    if source == destination:
        return {"installed": str(destination), "digest": manifest["digest"]}
    # Preflight all collisions before any writes. Managed, unchanged files may update.
    old_path = destination / "kit-manifest.json"
    old = json.loads(old_path.read_text(encoding="utf-8")) if old_path.is_file() else {}
    for name, digest in manifest["files"].items():
        path = destination / name
        if path.exists() and (not path.is_file() or sha256_file(path) not in {digest, old.get("files", {}).get(name)}):
            raise ValidationError(f"目标文件有本地修改，保留原文件：{path}")
    if old.get("files") and set(old["files"]) - set(manifest["files"]):
        raise ValidationError("新版删除了旧版文件，请使用新的安装目录")
    for name in manifest["files"]:
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, path)
    _atomic_json(destination / "kit-manifest.json", manifest)
    verify_kit(destination)
    return {"installed": str(destination), "digest": manifest["digest"]}


def reading_capabilities(kit: str | Path) -> dict[str, Any]:
    root = Path(kit).resolve()
    manifest = verify_kit(root)
    missing = [name for name in manifest["skills"] if not (root / "references" / "skills" / name / "SKILL.md").is_file()]
    pdf_available = importlib.util.find_spec("pymupdf") is not None
    return {
        "kit": str(root), "digest": manifest["digest"], "skills": manifest["skills"],
        "python": sys.executable, "python_version": sys.version.split()[0],
        "missing_skills": missing, "pymupdf_available": pdf_available,
        "ready": not missing and pdf_available and sys.version_info >= (3, 10),
        "ai_reader": "current_host_model", "zotero_required_for_verified_local_pdf": False,
    }
