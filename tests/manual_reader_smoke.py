"""Prepare/inspect an isolated synthetic source for real-host AI reader checks."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import pymupdf
from review_workflow.core import ReviewOrchestrator
from review_workflow.reading import prepare_reading_task
from review_workflow.reading_kit import default_kit, install_reading_kit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--engine", choices=("codex", "workbuddy"), required=True)
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    kb = root / "knowledge"
    pdf = root / "synthetic-source.pdf"
    if not pdf.exists():
        text = (
            "Synthetic Reader Acceptance Fixture (not a scientific publication)\n\n"
            "Question: Can a reader preserve the evidence boundary of a design rule?\n"
            "Method: Three independent synthetic samples were evaluated at T=250, 300,\n"
            "and 350 K. The measured toy conductivity was 0.2, 0.6, and 0.4 mS/cm,\n"
            "respectively. These are fictional test data, not electrolyte experiments.\n"
            "Result: The largest tested value was at 300 K. We propose selecting 300 K\n"
            "among these three tested settings to maximize this toy metric.\n"
            "Author-stated limitation: Each setting has only one sample; uncertainty\n"
            "was not quantified and no setting outside this range was measured.\n"
            "Knowledge product: A local empirical design rule, not a mechanism or\n"
            "a universal law. No held-out validation or causal experiment was done.\n"
            "There are no figures, tables, references, or supplementary PDFs."
        )
        document = pymupdf.open()
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(60, 60, 545, 770), text, fontsize=11)
        document.save(pdf)
        document.close()
    project_config = kb / "review-project.json"
    project_config.parent.mkdir(parents=True, exist_ok=True)
    project_config.write_text(json.dumps({"central_thesis": "解释的科学价值取决于可复用、可迁移、可检验、可修正的知识产物", "synthetic_test_only": True}, ensure_ascii=False), encoding="utf-8")
    workflow = ReviewOrchestrator(kb / "review-jobs")
    job = workflow.prepare_paper("fixture:portable-reader", pdf, zotero_key="FIXTURE1")
    if job["state"] == "source_ready":
        workflow.dispatch_reading_batch([job["job_id"]], {job["job_id"]: ["nature-reader", "deeppapernote"]})
    source_kit = default_kit(PROJECT, args.engine)
    kit = root / ".workbuddy" / "skills" / "paper-review" if args.engine == "workbuddy" else source_kit
    if kit != source_kit:
        install_reading_kit(source_kit, kit)
    task = prepare_reading_task(kb, job["job_id"], args.engine, kit)
    (root / "task-location.json").write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.launch:
        print(json.dumps({"task": task["task_path"], "kit": str(kit)}, ensure_ascii=False))
        return
    bridge = json.loads((PROJECT / "文献知识库/review-app/workbuddy/workbuddy-bridge.json").read_text(encoding="utf-8"))
    prompt = (
        f"这是隔离的单篇阅读能力验收，不是真实论文入库。使用 {kit / 'SKILL.md'} "
        f"和 {kit / 'references/single-paper.md'}，任务已准备于 {task['task_path']}。"
        f"你就是本篇阅读模型，无需调用Codex或子代理。只读这一份PDF，使用Python {sys.executable}。"
        "实际抽取并阅读正文，生成中文精读笔记、后台来源映射、八节评审内容和图表完整性清单；"
        "再运行包内render生成评审页。本例没有图表，不需要额外查询、全文翻译、外部文献或完整DeepPaperNote脚本流水线；"
        "但必须按技能的证据阅读、理解和写作标准由你自己完成内容。"
        "所有生成物只写task规定的exclusive_output_dir，主任务accept由测试程序随后执行。"
        "若任何工具权限被拒绝，报告拒绝原因并停止，不绕过。最终只简短报告完成或阻塞。"
    )
    command = [bridge["node_path"], bridge["cli_path"], "--print", "--output-format", "json", "--max-turns", "12", "--add-dir", str(kit), "--", prompt]
    with (root / "workbuddy-smoke.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(command, cwd=root, stdout=log, stderr=subprocess.STDOUT, timeout=240)
    print(json.dumps({"exit_code": completed.returncode, "result_exists": (Path(task["exclusive_output_dir"]) / "reading-result.json").is_file(), "log": str(root / "workbuddy-smoke.log")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
