"""Structural checks for the redesigned review page (no browser needed)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evidence-loop" / "src"))
sys.path.insert(0, str(ROOT / "artifacts" / "review_ui"))

from evidence_loop.html_review import render_review_html  # noqa: E402
from rerender import build_record, JOBS  # noqa: E402

TARGETS = ["paper-45d514c98a379d42", "paper-7844aca4d0f7364f", "paper-af794951649f8519"]


def check(html: str, job: dict) -> list[str]:
    problems: list[str] = []
    for marker in ("__READER_CONTENT_HTML__", "__FIGURES_HTML__", "__CLAIMS_HTML__",
                   "__PAGES_HTML__", "__PAPER_RECORD_JSON__", "__KATEX_RUNTIME_JS__"):
        if marker in html:
            problems.append(f"未替换的标记: {marker}")
    # 原文页已撤掉：不应再有整页缩略图，也不该留跳页入口。
    # （图卡自己的 data-page 只是标注页码，不构成跳转，故只查按钮形态。）
    for gone, why in (
        ('class="page"', "仍有原文页缩略图"),
        ('data-pane="pages"', "仍有原文页页签"),
        ('<button type="button" data-page="', "仍有跳原文页的页码按钮"),
        ("pagechips", "仍有跳页页码条"),
        ('id="page-', "仍有原文页锚点"),
    ):
        if gone in html:
            problems.append(why)
    if html.count('type="application/json"') != 1:
        problems.append("JSON 数据块数量异常")
    record = json.loads(re.search(r'<script type="application/json" id="paper-data">(.*?)</script>', html, re.S).group(1))
    if len(record["sections"]) != 8:
        problems.append(f"节数 {len(record['sections'])} != 8")
    if not all("source_pages" in s for s in record["sections"]):
        problems.append("分节缺少 source_pages")
    expect = {
        "速读段": (html.count('class="summary-section"'), None),
        "图表卡": (html.count('class="fig"'), None),
        "主张卡": (html.count('class="claim"'), len(record.get("claims") or [])),
    }
    if 'class="card" data-sec=' not in html:
        problems.append("缺少评审卡模板")
    for name, (found, want) in expect.items():
        if want is not None and found != want:
            problems.append(f"{name} 数量 {found} != {want}")
        if found == 0:
            problems.append(f"{name} 缺失")
    if html.count('id="citation-consent"') != 1:
        problems.append("缺少综合许可勾选框")
    if html.count('id="approved-claims"') != 1:
        problems.append("缺少主张批准文本框")
    if "runs/" in re.sub(r'<script type="application/json".*?</script>', "", html, flags=re.S):
        problems.append("仍有未内嵌的图片相对路径")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    app = max(scripts, key=len) if scripts else ""
    tmp = ROOT / "artifacts" / "review_ui" / "_app.js"
    tmp.write_text(app, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(tmp)], capture_output=True, text=True)
    if result.returncode != 0:
        problems.append("JS 语法错误: " + result.stderr.strip().splitlines()[-1] if result.stderr else "JS 语法错误")
    return problems


def main() -> int:
    out_dir = ROOT / "artifacts" / "review_ui" / "preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for job_id in TARGETS:
        job = json.loads((JOBS / job_id / "job.json").read_text(encoding="utf-8"))
        html = render_review_html(build_record(job))
        (out_dir / f"{job_id}.html").write_text(html, encoding="utf-8")
        problems = check(html, job)
        print(f"{job_id}  {len(html.encode('utf-8')) / 1e6:.1f}MB  问题 {len(problems)}")
        for item in problems:
            print("   -", item)
        failures += len(problems)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
