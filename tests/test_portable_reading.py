import json
from pathlib import Path
import shutil
import subprocess
import sys

import pymupdf
import pytest

from review_workflow.core import ReviewOrchestrator, ValidationError
from review_workflow.html_review import EIGHT_SECTIONS
from review_workflow.reading import (
    accept_reading_output, extract_reading_source, prepare_reading_task, render_reading_output,
)
from review_workflow.reading_kit import SKILLS, build_reading_kit, install_reading_kit, verify_kit


def write_json(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


@pytest.fixture
def kit(tmp_path):
    skills = tmp_path / "original skills"
    for name in SKILLS:
        directory = skills / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Fixture\n---\n", encoding="utf-8")
    result = build_reading_kit(tmp_path / "export" / "paper-review", skills)
    return Path(result["kit"])


@pytest.fixture
def job(tmp_path):
    kb = tmp_path / "project" / "knowledge"
    pdf = tmp_path / "fixture.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Synthetic fixture. Design rule: x > 2. Tested only for x in [1, 3].")
    document.save(pdf)
    document.close()
    workflow = ReviewOrchestrator(kb / "review-jobs")
    record = workflow.prepare_paper("doi:fixture", pdf, zotero_key="FIXTURE1")
    workflow.dispatch_reading_batch([record["job_id"]], {record["job_id"]: ["nature-reader", "deeppapernote"]})
    return kb, record["job_id"]


def outputs(task):
    directory = Path(task["exclusive_output_dir"])
    (directory / "reader").mkdir(exist_ok=True)
    (directory / "reader/paper.md").write_text("Synthetic source anchor: page 1.", encoding="utf-8")
    write_json(directory / "reader/source_map.json", {"page": 1})
    (directory / "deep-paper-note.md").write_text("# Fixture\n## 速览\nA synthetic fixture. $x > 2$.\n", encoding="utf-8")
    write_json(directory / "review-sections.json", {"title": "Synthetic fixture", "sections": {section["id"]: {"ai_content": "Synthetic test content", "source_anchors": "p.1"} for section in EIGHT_SECTIONS}})
    write_json(directory / "figure-completeness.json", {"items": []})


@pytest.mark.parametrize("engine", ["codex", "workbuddy"])
def test_engine_neutral_single_paper_pipeline_and_human_gate(kit, job, engine):
    kb, job_id = job
    task = prepare_reading_task(kb, job_id, engine, kit)
    source = extract_reading_source(task["task_path"])
    assert "Design rule" in Path(source["pages_jsonl"]).read_text(encoding="utf-8")
    assert extract_reading_source(task["task_path"])["cached"]
    outputs(task)
    result = render_reading_output(task["task_path"])
    workflow = ReviewOrchestrator(kb / "review-jobs")
    assert workflow.get_job(job_id)["state"] == "reading"  # worker never mutates shared state
    assert not (kb / "review-app/library.json").exists()
    accepted = accept_reading_output(kb, job_id)
    assert accepted["state"] == "human_reviewing"
    assert (kb / "review-app/library.json").is_file()
    assert result["citation_permission"] == {"allowed": False, "valid": False}
    assert workflow.get_job(job_id)["citation_permission"]["allowed"] is False
    assert accept_reading_output(kb, job_id)["state"] == "human_reviewing"  # retry is idempotent
    assert "katex" in Path(accepted["review_html"]).read_text(encoding="utf-8")


def test_handoff_requires_explicit_engine_transfer(kit, job):
    kb, job_id = job
    task = prepare_reading_task(kb, job_id, "codex", kit)
    outputs(task)
    with pytest.raises(ValidationError, match="另一个执行端"):
        prepare_reading_task(kb, job_id, "workbuddy", kit)
    moved = prepare_reading_task(kb, job_id, "workbuddy", kit, handoff=True)
    assert moved["engine"] == "workbuddy"
    assert (Path(moved["exclusive_output_dir"]) / "deep-paper-note.md").is_file()


def test_incomplete_sections_do_not_generate_finished_page(kit, job):
    task = prepare_reading_task(*job, "workbuddy", kit)
    outputs(task)
    record_path = Path(task["exclusive_output_dir"]) / "review-sections.json"
    record = json.loads(record_path.read_text())
    record["sections"].pop("results_evidence")
    write_json(record_path, record)
    with pytest.raises(ValidationError, match="完整八节"):
        render_reading_output(task["task_path"])
    assert not (record_path.parent / "paper-review.html").exists()


def test_changed_output_cannot_be_registered(kit, job):
    task = prepare_reading_task(*job, "codex", kit)
    outputs(task)
    render_reading_output(task["task_path"])
    (Path(task["exclusive_output_dir"]) / "deep-paper-note.md").write_text("Changed note", encoding="utf-8")
    with pytest.raises(ValidationError, match="已变化"):
        accept_reading_output(*job)


def test_declared_figure_must_be_included_and_inside_job(kit, job):
    task = prepare_reading_task(*job, "codex", kit)
    outputs(task)
    write_json(Path(task["exclusive_output_dir"]) / "figure-completeness.json", {"items": [{"id": "Fig. 1", "source_page": 1, "decision": "insert", "asset": "../../outside.png"}]})
    with pytest.raises(ValidationError, match="隔离目录"):
        render_reading_output(task["task_path"])


def test_handoff_cannot_register_another_engines_old_receipt(kit, job):
    task = prepare_reading_task(*job, "codex", kit)
    outputs(task)
    render_reading_output(task["task_path"])
    prepare_reading_task(*job, "workbuddy", kit, handoff=True)
    with pytest.raises(ValidationError, match="执行端已交接"):
        accept_reading_output(*job)


def test_bundle_manifest_cannot_substitute_external_files(kit):
    manifest = kit / "kit-manifest.json"
    record = json.loads(manifest.read_text())
    record["files"]["../../external"] = "0" * 64
    write_json(manifest, record)
    with pytest.raises(ValidationError, match="能力包文件已变化"):
        verify_kit(kit)


def test_relocated_kit_runs_without_original_host_or_checkout(kit, job, tmp_path):
    moved = tmp_path / "another machine" / "paper-review"
    install_reading_kit(kit, moved)
    shutil.rmtree(kit)  # prove resources resolve from the installed copy
    process = subprocess.run([sys.executable, str(moved / "scripts/review.py"), "doctor"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["ready"] is True
    kb, job_id = job
    prepared = subprocess.run([sys.executable, str(moved / "scripts/review.py"), "task", "--kb", str(kb), "--job-id", job_id, "--engine", "workbuddy"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert prepared.returncode == 0, prepared.stderr
    task = json.loads(prepared.stdout)
    assert all(str(moved) in path for path in task["skill_paths"].values())
    outputs(task)
    rendered = subprocess.run([sys.executable, str(moved / "scripts/review.py"), "render", task["task_path"]], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert rendered.returncode == 0, rendered.stderr
    assert json.loads(rendered.stdout)["status"] == "ready_for_registration"


def test_standalone_kit_prepares_new_single_paper_project(kit, job, tmp_path):
    original_kb, job_id = job
    original = ReviewOrchestrator(original_kb / "review-jobs").get_job(job_id)
    kb = tmp_path / "new standalone project"
    completed = subprocess.run([
        sys.executable, str(kit / "scripts/review.py"), "prepare", "--kb", str(kb),
        "--paper-id", "fixture:new-project", "--zotero-key", "NEWPAPER", "--pdf", original["pdf_path"],
        "--engine", "codex",
    ], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    task = json.loads(completed.stdout)
    assert task["pdf_limit"] == 1
    assert task["paper_id"] == "fixture:new-project"
    assert ReviewOrchestrator(kb / "review-jobs").get_job(task["job_id"])["state"] == "reading"


def test_install_preserves_local_changes_and_bundle_has_no_project_data(kit, tmp_path):
    destination = tmp_path / "installed" / "paper-review"
    install_reading_kit(kit, destination)
    skill_file = destination / "SKILL.md"
    skill_file.write_text("User modification", encoding="utf-8")
    with pytest.raises(ValidationError, match="本地修改"):
        install_reading_kit(kit, destination)
    assert skill_file.read_text() == "User modification"
    names = verify_kit(kit)["files"]
    assert not any(name.endswith(".pdf") or "review-jobs" in name or "workbuddy-bridge" in name for name in names)
