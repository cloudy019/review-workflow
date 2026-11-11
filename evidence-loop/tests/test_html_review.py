import json
import re
import base64
from pathlib import Path

import pytest

from evidence_loop.html_review import (
    EIGHT_SECTIONS,
    ReviewRecordError,
    normalize_paper_record,
    render_review_html,
    write_review_html,
)
from evidence_loop.core import ReviewOrchestrator


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
    for label in ("正确", "部分正确", "错误", "无法判断", "跳过（不评审本节）", "分类选择", "你的心得"):
        assert label in html
    assert "localStorage.setItem" in html
    assert "citation_permission" in html
    assert 'allowed: paperHasValue && reviewOutcome === "human_confirmed"' in html
    assert "reviewOutcome" in html
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
    assert 'id="download">下载备用JSON</button>' in rendered
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
    assert "具体主张（可留空" in rendered
    assert 'id="citation-consent"' in rendered
    assert 'id="citation-request"' not in rendered
    assert "具体主张由AI提取" in rendered
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

    assert '<article class="reader-panel"' in rendered
    assert "<h1>全文阅读</h1>" in rendered
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
    assert 'id="image-lightbox"' in rendered
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
    assert "AI论文速读器" in rendered
    assert "concise key English information" in rendered
    assert "这段材料用于支持边界判断。" in rendered


def test_copy_title_button_has_clipboard_and_fallback(paper_record):
    rendered = render_review_html(paper_record)
    assert 'id="copy-title"' in rendered
    assert "navigator.clipboard.writeText(paper.title)" in rendered
    assert 'document.execCommand("copy")' in rendered
    assert 'button.textContent = "已复制"' in rendered


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
    assert 'button.dataset.summaryFilter' in rendered


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
    paper_record["reader_markdown"] = "# Markdown正文"
    paper_record["reader_html"] = "<h1>HTML正文</h1>"
    rendered = render_review_html(paper_record)
    assert "<h1>Markdown正文</h1>" in rendered
    assert "HTML正文" not in rendered


def test_draft_restore_reads_nested_export_categories(paper_record):
    rendered = render_review_html(paper_record)
    assert "answer.answers && answer.answers.categories" in rendered
    assert "answer.answers.categories) || answer.categories" in rendered
    assert "已恢复上次保存的人工评审" in rendered
    assert "const response = await fetch(endpoint)" in rendered


def test_invalid_reader_base_dir_is_rejected(paper_record, tmp_path):
    paper_record["reader_markdown"] = "![图](missing.png)"
    paper_record["reader_base_dir"] = str(tmp_path / "missing")
    with pytest.raises(ReviewRecordError, match="reader_base_dir"):
        render_review_html(paper_record)
