"""Bounded tracked-file/history/package audit; never print matched values."""
import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "credential_pattern": re.compile(rb"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_]{18,}\b"),
    "personal_windows_path": re.compile(rb"[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9_-]+"),
    "personal_posix_path": re.compile(rb"/(?:home|Users)/[A-Za-z0-9_-]+/"),
}


def prohibited(name):
    parts = Path(name).parts
    return (any(part in {"workspace", "workspaces", "private", ".codex", ".workbuddy", "logs"} for part in parts)
            or Path(name).suffix.lower() in {".pdf", ".sqlite", ".db", ".pem", ".key"}
            or Path(name).name.startswith(".env") and Path(name).name != ".env.example")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    findings, checked = [], 0
    def check(scope, name, data):
        nonlocal checked
        checked += 1
        if prohibited(name):
            findings.append({"scope": scope, "file": name, "category": "runtime_or_secret_file"})
        for category, pattern in PATTERNS.items():
            if pattern.search(data):
                findings.append({"scope": scope, "file": name, "category": category})
    for raw in git("ls-files", "-z").split(b"\0"):
        if raw:
            name = raw.decode("utf-8")
            check("working", name, (ROOT / name).read_bytes())
            check("staged", name, git("show", ":" + name))
    commits = git("rev-list", "--all").decode().splitlines()
    for commit in commits:
        for raw in git("ls-tree", "-r", "--name-only", "-z", commit).split(b"\0"):
            if raw:
                name = raw.decode("utf-8")
                check("history:" + commit[:8], name, git("show", f"{commit}:{name}"))
    if args.wheel:
        with zipfile.ZipFile(args.wheel) as package:
            for name in package.namelist():
                check("wheel", name, package.read(name))
    print(json.dumps({"checked_records": checked, "commits": len(commits), "findings": findings}, indent=2))
    return bool(findings)


if __name__ == "__main__":
    raise SystemExit(main())
