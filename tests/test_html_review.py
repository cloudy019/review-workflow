import json
import re
import base64
from pathlib import Path

import pytest

from review_workflow.html_review import (
    EIGHT_SECTIONS,
    ReviewRecordError,
    normalize_paper_record,
    render_review_html,
    write_review_html,
)
from review_workflow.core import ReviewOrchestrator


@pytest.fixture
def paper_record():
    return {
        "job_id": "paper-0123456789abcdef",
        "paper_id": "doi:10.1000/example",
        "zotero_key": "ABCD1234",
        "pdf_sha256": "a" * 64,
        "ai_note_sha256": "b" * 64,
        "title": "A <single> paper",
        "citation": "Author et al. (2026)",
        "sections": {
            "relevance": {"ai_content": "核心相关", "source_anchors": "p. 1"},
            "results_evidence": "结果支持主张",
        },
    }


def _embedded_record(html):
    match = re.search(r'<script type="application/json" id="paper-data">(.*?)</script>', html, re.S)
    assert match
    return json.loads(match.group(1))


def test_normalize_requires_exactly_one_identity(paper_record):
    for field in ("job_id", "paper_id", "zotero_key", "pdf_sha256", "ai_note_sha256"):
        broken = dict(paper_record)
        broken[field] = ["one", "two"]
        with pytest.raises(ReviewRecordError):
            normalize_paper_record(broken)


def test_render_contains_eight_sections_and_offline_review_behaviour(paper_record):
    html = render_review_html(paper_record)
    embedded = _embedded_record(html)

    assert len(embedded["sections"]) == 8
    assert [item["id"] for item in embedded["sections"]] == [item["id"] for item in EIGHT_SECTIONS]
    for label in ("正确", "部分正确", "错误", "无法判断", "跳过", "分类", "心得 / 修正 / 补充"):
        assert label in html
    assert "localStorage.setItem" in html
    assert "citation_permission" in html
    assert 'allowed: hasValue && outcome === "human_confirmed"' in html
    assert "review_outcome: outcome" in html
    assert '"partially_correct"' in html
    assert '"unable_to_judge"' in html
    # The vendored KaTeX parser has an internal method named fetch(); the page
    # must still contain no network target or external resource reference.
    assert 'fetch("http' not in html and "fetch('http" not in html
    assert not re.search(r'<(?:script|img|link)[^>]+(?:src|href)=["\']https?://', html)


def test_export_contract_fields_are_present(paper_record):
    html = render_review_html(paper_record)
    for field in (
        "schema_version",
        "job_id",
        "paper_id",
        "zotero_key",
        "pdf_sha256",
        "ai_note_sha256",
        "review_outcome",
        "sections",
        "citation_permission",
        "exported_at",
    ):
        assert re.search(rf"\b{field}\b", html)


def test_review_export_uses_loopback_save_and_keeps_backup_download(paper_record):
    paper_record["human_review_dir"] = "E:/project/paper/human_reviews"
    rendered = render_review_html(paper_record)
    assert 'id="bind-folder"' not in rendered
    assert "window.showDirectoryPicker" not in rendered
    assert 'id="export">保存评审</button>' in rendered
    assert 'id="download">下载备用 JSON</button>' in rendered
    assert 'fetch(endpoint' in rendered
    assert 'location.hostname === "127.0.0.1"' in rendered
    assert "downloadReview(text)" in rendered
    assert "E:/project/paper/human_reviews" in rendered


def test_embedded_json_cannot_close_script_tag(paper_record):
    paper_record["title"] = "</script><script>alert(1)</script>"
    html = render_review_html(paper_record)
    assert html.count('</script><script>alert(1)</script>') == 0
    assert _embedded_record(html)["title"] == "</script><script>alert(1)</script>"


def test_write_review_html(tmp_path, paper_record):
    output = write_review_html(paper_record, tmp_path / "paper.html")
    assert output == (tmp_path / "paper.html").resolve()
    assert output.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_unknown_section_is_rejected(paper_record):
    paper_record["sections"]["ninth"] = "not allowed"
    with pytest.raises(ReviewRecordError, match="unknown section"):
        render_review_html(paper_record)


def test_legacy_hash_names_are_input_only(paper_record):
    paper_record["pdf_hash"] = paper_record.pop("pdf_sha256")
    paper_record["note_hash"] = paper_record.pop("ai_note_sha256")
    embedded = _embedded_record(render_review_html(paper_record))
    assert embedded["pdf_sha256"] == "a" * 64
    assert embedded["ai_note_sha256"] == "b" * 64
    assert "pdf_hash" not in embedded and "note_hash" not in embedded


def test_complete_export_fixture_matches_core_contract(paper_record):
    # Mirrors the object produced in JavaScript after all eight sections are
    # explicitly decided and at least one concrete claim is approved.
    exported = {
        "schema_version": "1.0",
        "job_id": paper_record["job_id"],
        "paper_id": paper_record["paper_id"],
        "zotero_key": paper_record["zotero_key"],
        "pdf_sha256": paper_record["pdf_sha256"],
        "ai_note_sha256": paper_record["ai_note_sha256"],
        "review_outcome": "human_confirmed",
        "sections": [
            {"section_id": number, "verdict": "correct", "notes": "", "answers": {"categories": []}}
            for number in range(1, 9)
        ],
        "citation_permission": {"allowed": True, "approved_claims": ["具体主张"], "notes": ""},
        "exported_at": "2026-08-11T00:00:00.000Z",
    }
    # The core validator deliberately ignores provenance fields it does not
    # interpret, while checking hashes, verdicts and permission structure.
    ReviewOrchestrator.validate_human_review(exported)


def test_paper_level_synthesis_permission_needs_no_handwritten_claims(paper_record):
    rendered = render_review_html(paper_record)
    assert 'id="approved-claims"' in rendered
    assert "已批准的具体主张" in rendered
    assert 'id="citation-consent"' in rendered
    assert 'id="citation-request"' not in rendered
    assert "具体主张仍需在写作前逐条批准" in rendered
    review = {
        "schema_version": "1.0",
        "job_id": paper_record["job_id"],
        "paper_id": paper_record["paper_id"],
        "zotero_key": paper_record["zotero_key"],
        "pdf_sha256": paper_record["pdf_sha256"],
        "ai_note_sha256": paper_record["ai_note_sha256"],
        "review_outcome": "human_confirmed",
        "sections": [
            {
                "section_id": number,
                "verdict": "skipped" if number == 3 else "correct",
                "notes": "",
                "answers": {},
            }
            for number in range(1, 9)
        ],
        "citation_permission": {"allowed": True, "approved_claims": [], "notes": ""},
        "exported_at": "2026-08-16T00:00:00+08:00",
    }
    ReviewOrchestrator.validate_human_review(review)


def _write_tiny_png(path):
    path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
    )


def test_markdown_reader_is_safe_and_embeds_local_images(tmp_path, paper_record):
    image = tmp_path / "figure.png"
    _write_tiny_png(image)
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = """---
title: hidden metadata
---
# 全文阅读
<a id="S001"></a>
**Original:** evidence

**中文:** 证据

Inline $R^2=0.88$.

$$X_{i,j}=\\frac{x_{i,j}-\\mu_j}{\\sigma_j}. \\tag{1}$$

![结果图](figure.png)

<script>alert('bad')</script>
[外部链接](https://evil.invalid/tracker)
"""
    rendered = render_review_html(paper_record)

    assert 'class="note" id="paper-reader"' in rendered
    assert "<h1>全文阅读</h1>" not in rendered, "H1 由页头标题承载，不在速读区重复"
    assert '<span class="source-anchor" id="S001"></span>' in rendered
    assert "data:image/png;base64," in rendered
    assert 'src="figure.png"' not in rendered
    assert "<script>alert('bad')</script>" not in rendered
    assert "&lt;script&gt;alert" in rendered
    assert "https://evil.invalid" not in rendered
    assert '<div class="source-label">关键英文信息 · AI整理</div>' in rendered
    assert '<div class="ai-annotation">' in rendered
    assert "AI理解与阅读提示" in rendered
    assert '<span class="reader-math">R^2=0.88</span>' in rendered
    assert 'class="reader-math reader-math-block"' in rendered
    assert "katex.render(tex, element" in rendered
    assert 'output: "mathml"' in rendered


def test_image_lightbox_is_available_and_keyboard_closable(paper_record):
    rendered = render_review_html(paper_record)
    assert 'class="lb" id="lb"' in rendered
    assert 'id="lb-img"' in rendered and 'id="lb-close"' in rendered
    assert 'cursor:zoom-in' in rendered
    assert 'event.target.closest("img")' in rendered
    assert 'event.key === "Escape"' in rendered


def test_reader_uses_chunked_key_information_not_full_source_layer(paper_record):
    paper_record["reader_markdown"] = (
        "# Guided reading\n\n**Original:** concise key English information\n\n"
        "**中文:** 这段材料用于支持边界判断。"
    )
    rendered = render_review_html(paper_record)
    assert "英文原文层" not in rendered
    assert "论文速读" in rendered
    assert "concise key English information" in rendered
    assert "这段材料用于支持边界判断。" in rendered


def test_copy_title_button_has_clipboard_and_fallback(paper_record):
    rendered = render_review_html(paper_record)
    assert 'id="copy-title"' in rendered
    assert "navigator.clipboard.writeText(paper.title)" in rendered
    assert 'document.execCommand("copy")' in rendered
    assert 'flash("已复制论文标题。", "ok")' in rendered
    assert "复制失败，请手动选中标题。" in rendered


def test_paper_summary_mode_has_faceted_speed_reading_and_omits_full_abstract(paper_record):
    paper_record["reader_mode"] = "paper_summary"
    paper_record["reader_markdown"] = """# Paper title
## 原文摘要翻译
不应在速读页面重复整段摘要。
## 一句话总结
一句话结论。
## 方法主线
### 机制流程
输入到输出。
## 关键结果
关键数字。
## 局限
外推有限。
## 我的笔记
可写入综述的位置。
## 引用
参考文献不应显示。
"""
    rendered = render_review_html(paper_record)
    assert "论文速读" in rendered
    for label in ("全部", "速览", "方法", "结果", "局限", "综述价值"):
        assert f">{label}</button>" in rendered
    assert 'data-summary-topic="overview"' in rendered
    assert 'data-summary-topic="method"' in rendered
    assert 'data-summary-topic="result"' in rendered
    assert 'data-summary-topic="limitations"' in rendered
    assert 'data-summary-topic="review"' in rendered
    assert "不应在速读页面重复整段摘要" not in rendered
    assert "参考文献不应显示" not in rendered
    assert 'const selected = btn.dataset.summaryFilter' in rendered


def test_reader_html_is_allowlist_sanitized_and_offline(tmp_path, paper_record):
    image = tmp_path / "figure.png"
    _write_tiny_png(image)
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_html"] = (
        '<section><h2 onclick="steal()">结果</h2>'
        '<script>alert(1)</script>'
        '<img src="https://evil.invalid/pixel.png" onerror="steal()" alt="remote">'
        '<img src="figure.png" onerror="steal()" alt="local">'
        '<a href="javascript:steal()">危险链接</a></section>'
    )
    rendered = render_review_html(paper_record)

    assert "<h2>结果</h2>" in rendered
    assert "alert(1)" not in rendered
    assert '<h2 onclick=' not in rendered and re.search(r'<img[^>]+onerror=', rendered) is None
    assert "javascript:" not in rendered
    assert "https://evil.invalid" not in rendered
    assert rendered.count("data:image/png;base64,") == 1
    assert "[图片未嵌入：remote]" in rendered


def test_markdown_reader_takes_precedence_over_reader_html(paper_record):
    paper_record["reader_markdown"] = "# 标题\n\n这是 Markdown 阅读区的一句正文。"
    paper_record["reader_html"] = "<h1>HTML正文</h1><p>HTML 阅读区的正文。</p>"
    rendered = render_review_html(paper_record)
    assert "这是 Markdown 阅读区的一句正文。" in rendered
    assert "HTML 阅读区的正文。" not in rendered


def test_draft_restore_reads_nested_export_categories(paper_record):
    rendered = render_review_html(paper_record)
    assert "answer.answers && answer.answers.categories" in rendered
    assert "answer.answers.categories) || answer.categories" in rendered
    assert "已恢复上次保存的评审" in rendered
    assert "const response = await fetch(endpoint)" in rendered


def test_invalid_reader_base_dir_is_rejected(paper_record, tmp_path):
    paper_record["reader_markdown"] = "![图](missing.png)"
    paper_record["reader_base_dir"] = str(tmp_path / "missing")
    with pytest.raises(ReviewRecordError, match="reader_base_dir"):
        render_review_html(paper_record)


def test_false_positive_omit_reason_does_not_claim_a_real_crop(tmp_path, paper_record):
    """A dropped inventory row mentioning Table 1 must not own the Table 1 crop."""

    assets = tmp_path / "reader" / "assets"
    assets.mkdir(parents=True)
    _write_tiny_png(assets / "p002_Table_1.png")
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = "# 全文阅读\n\n## 方法与数据\n\n见 Table 1。\n"
    paper_record["sections"]["figures_equations"] = {"ai_content": "Table 1 给出数据库清单。"}
    paper_record["figure_decisions"] = [
        {"id": "p2-f2", "decision": "omit", "reason": "正文对Table1的指引误检，不是独立图表。"},
        {"id": "p2-f1", "decision": "include", "reason": "Table 1 列出数据库来源，承担证据功能。"},
    ]
    rendered = render_review_html(paper_record)

    assert 'class="fig"' in rendered, "真实裁图仍应显示"
    assert "纳入证据" in rendered
    assert "不作正文图" not in rendered, "误检行不应给真实裁图打上 omit 徽标"


def test_page_with_no_crops_states_the_reason_and_guards_the_fig_filter(tmp_path, paper_record):
    """A text-only page must say why, and "只看图" must never blank the page."""

    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = "# 全文阅读\n\n## 方法与数据\n\n这里讨论 Fig. 1。\n"
    paper_record["figure_decisions"] = [
        {"id": "p3-f1", "decision": "include", "reason": "Fig. 1 给出闭环流程。"},
        {"id": "p4-f1", "decision": "include", "reason": "Fig. 2 给出文本挖掘流程。"},
    ]
    rendered = render_review_html(paper_record)

    assert 'class="fig-missing"' in rendered
    assert "图表缺失" in rendered
    assert "reader/assets/" in rendered
    assert "2 条「纳入证据」" in rendered
    assert "不臆造、不重绘" in rendered
    assert "runs/" not in rendered, "不能在页面里留下未内嵌的图片相对路径"
    assert "没有通过核验的图表裁图，已保留全部速读内容" in rendered
    assert 'btn.textContent = "图表缺失"' in rendered


def test_page_with_all_crops_has_no_gap_notice(tmp_path, paper_record):
    assets = tmp_path / "reader" / "assets"
    assets.mkdir(parents=True)
    _write_tiny_png(assets / "p003_Fig_1.png")
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = "# 全文阅读\n\n## 方法与数据\n\n这里讨论 Fig. 1。\n"
    paper_record["figure_decisions"] = [
        {"id": "p3-f1", "decision": "include", "reason": "Fig. 1 给出闭环流程。"},
    ]
    rendered = render_review_html(paper_record)

    assert 'class="fig"' in rendered
    assert 'class="fig-missing"' not in rendered


def test_every_verified_crop_gets_a_card_even_with_boilerplate_decisions(tmp_path, paper_record):
    """Decisions are annotations, not a whitelist: 4 crops must yield 4 cards.

    Regression for a task where 18 identical boilerplate
    decisions (all mentioning an unrelated "Fig9") let one crop claim every
    decision and left the page with a single figure card.
    """

    assets = tmp_path / "reader" / "assets"
    assets.mkdir(parents=True)
    rows = []
    for number in range(1, 5):
        name = f"p00{number}_Figure_{number}.png"
        _write_tiny_png(assets / name)
        rows.append({
            "label": f"Figure {number}",
            "page": number,
            "status": "verified",
            "caption": f"Figure {number}. Original caption from the PDF.",
            "asset": {"path": name},
        })
    (tmp_path / "reader" / "figure-crops.json").write_text(
        json.dumps({"captions": 4, "verified": 4, "missing": 0, "rows": rows}, ensure_ascii=False),
        encoding="utf-8",
    )
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = "# 全文阅读\n\n## 方法与数据\n\n这里概述方法与数据。\n"
    paper_record["figure_decisions"] = [
        {"id": "p1-f1", "decision": "include", "reason": "自动清单漏Fig9；本页图承担证据功能。"},
    ]
    rendered = render_review_html(paper_record)

    assert rendered.count('class="fig"') == 4, "每张核验通过的裁图都必须有自己的图卡"
    assert 'class="fig-missing"' not in rendered
    assert "Original caption from the PDF" in rendered, "无决策标注时回退到原 PDF 图题"


def test_manifest_missing_rows_produce_a_partial_gap_notice(tmp_path, paper_record):
    """A real missing crop is reported with its figure id and concrete reason."""

    assets = tmp_path / "reader" / "assets"
    assets.mkdir(parents=True)
    for number in (1, 2):
        _write_tiny_png(assets / f"p00{number}_Fig_{number}.png")
    (tmp_path / "reader" / "figure-crops.json").write_text(
        json.dumps({
            "captions": 3,
            "verified": 2,
            "missing": 1,
            "rows": [
                {"label": "Fig. 1", "page": 1, "status": "verified", "asset": {"path": "p001_Fig_1.png"}},
                {"label": "Fig. 2", "page": 2, "status": "verified", "asset": {"path": "p002_Fig_2.png"}},
                {"label": "Fig. 6", "page": 13, "status": "missing",
                 "missing_reason": "图表缺失：无法从当前 PDF 提取 —— no_figure_region_near_caption"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = "# 全文阅读\n\n## 方法与数据\n\n这里概述方法与数据。\n"
    rendered = render_review_html(paper_record)

    assert rendered.count('class="fig"') == 2
    assert "部分图表缺失：无法从当前 PDF 提取" in rendered
    assert "Fig. 6" in rendered
    assert "no_figure_region_near_caption" in rendered
    assert "从原 PDF 检出 3 条图题" in rendered
    assert "不臆造、不重绘" in rendered


def test_complete_manifest_suppresses_gap_notice_when_decisions_outnumber_crops(tmp_path, paper_record):
    """Decision count is a different ledger; it must not imply missing figures."""

    assets = tmp_path / "reader" / "assets"
    assets.mkdir(parents=True)
    for number in (1, 2):
        _write_tiny_png(assets / f"p00{number}_Fig_{number}.png")
    (tmp_path / "reader" / "figure-crops.json").write_text(
        json.dumps({
            "captions": 2,
            "verified": 2,
            "missing": 0,
            "rows": [
                {"label": f"Fig. {n}", "page": n, "status": "verified",
                 "asset": {"path": f"p00{n}_Fig_{n}.png"}}
                for n in (1, 2)
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    paper_record["reader_base_dir"] = str(tmp_path)
    paper_record["reader_markdown"] = "# 全文阅读\n\n## 方法与数据\n\n这里概述方法与数据。\n"
    paper_record["figure_decisions"] = [
        {"id": f"p{n}-f{n}", "decision": "include", "reason": "承担证据功能。"}
        for n in range(1, 13)
    ]
    rendered = render_review_html(paper_record)

    assert rendered.count('class="fig"') == 2
    assert 'class="fig-missing"' not in rendered, "清单齐全时不得按决策条数反推缺图"
