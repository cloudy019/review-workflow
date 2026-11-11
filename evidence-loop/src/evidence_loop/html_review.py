"""Render a single-paper, offline human-review page.

The generated page has no network dependencies.  Human answers are kept in the
browser until the reviewer explicitly exports a JSON record.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from collections.abc import Mapping
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SCHEMA_VERSION = "1.0"

EIGHT_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "relevance",
        "title": "1. 相关性、阅读范围与证据角色",
        "critical": True,
        "categories": ["核心证据", "支持证据", "背景证据", "排除"],
    },
    {
        "id": "question_contribution",
        "title": "2. 研究问题、真实缺口与贡献",
        "critical": False,
        "categories": ["问题明确", "缺口成立", "贡献充分", "贡献被夸大"],
    },
    {
        "id": "methods_data",
        "title": "3. 方法、数据、材料与实验设计",
        "critical": True,
        "categories": ["方法适当", "数据充分", "设计可复现", "存在混杂或偏差"],
    },
    {
        "id": "results_evidence",
        "title": "4. 主要结果、关键数字与证据强度",
        "critical": True,
        "categories": ["强证据", "中等证据", "弱证据", "结果与结论不匹配"],
    },
    {
        "id": "figures_equations",
        "title": "5. 图表、公式、机制流程与原文位置",
        "critical": False,
        "categories": ["已定位原文", "图表支持", "公式支持", "需要页码级复核"],
    },
    {
        "id": "limitations",
        "title": "6. 作者局限、人工批判与未被证明的内容",
        "critical": True,
        "categories": ["作者明示局限", "人工新增批判", "存在负面证据", "结论外推过度"],
    },
    {
        "id": "knowledge_product",
        "title": "7. 知识产物、迁移性、可检验性与适用边界",
        "critical": False,
        "categories": ["经验或设计规则", "科学概念或描述符", "解析方程或定量规律", "机制或理论结构"],
    },
    {
        "id": "synthesis_permission",
        "title": "8. 论文关系、可写位置与综合许可",
        "critical": True,
        "categories": ["支持既有主张", "质疑既有主张", "形成新主张", "仅作背景"],
    },
)

_REQUIRED_IDENTITY_FIELDS = ("job_id", "paper_id", "zotero_key")
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "paper_review.html"
_KATEX_JS_PATH = (
    Path(__file__).with_name("vendor")
    / "katex"
    / "katex.min.js"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_IMAGE_MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
_MAX_EMBED_IMAGE_BYTES = 20 * 1024 * 1024
_READER_MARKER = "__READER_CONTENT_HTML__"
_PAGE_SECTION_RE = re.compile(r"(?m)^##\s+原文页图\s*$")
_NUMBERED_SECTION_RE = re.compile(r"(?m)^##\s+\d+\.\s+")
_CLAIMS_HEADING_RE = re.compile(r"(?m)^##\s*候选主张.*$")
_H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
_FIGURE_FILE_RE = re.compile(r"^p(\d+)_(.+?)\.(?:png|jpg|jpeg)$", re.IGNORECASE)
_FIGURE_ID_RE = re.compile(r"^p(\d+)-f(\d+)$")
_FIG_MENTION_RE = re.compile(r"(?:Fig(?:ure)?s?\.?\s*|图\s*)(\d{1,2})", re.IGNORECASE)
_TABLE_MENTION_RE = re.compile(r"Table\s*(\d{1,2})", re.IGNORECASE)
# A reason stating "this inventory row is not a real figure" only *mentions* a
# number (e.g. "正文对Table1的指引误检").  Reading that mention as a pairing
# target lets a false-positive row claim the genuine crop and stamp an "omit"
# badge on a core figure.
_NEGATIVE_FIGURE_REASON_RE = re.compile(
    r"误检|不是独立图表|非独立图表|非该页一幅图|非第二张独立表|非一幅图|"
    r"原页无新图|重复条目|文字引用|只是正文|非独立图",
    re.IGNORECASE,
)
# Reader-flow blocks: a figure is dropped right after the block that talks about
# it, so the picture and the words describing it stay on the same screen.
_BLOCK_RE = re.compile(r"<(p|li)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_SUMMARY_SECTION_RE = re.compile(
    r'<section class="summary-section" data-summary-topic="([a-zA-Z]+)"', re.IGNORECASE
)



class ReviewRecordError(ValueError):
    """Raised when a review page input is not one identifiable paper."""


def _reader_base_dir(record: Mapping[str, Any]) -> Path | None:
    value = record.get("reader_base_dir")
    if value in (None, ""):
        return None
    if not isinstance(value, (str, Path)):
        raise ReviewRecordError("reader_base_dir must be a single local path")
    path = Path(value).expanduser()
    if not path.is_dir():
        raise ReviewRecordError(f"reader_base_dir is not a directory: {path}")
    return path.resolve()


def _valid_image_bytes(data: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def _local_image_data_uri(source: str, base_dir: Path | None) -> str | None:
    """Return an inert raster data URI; reject remote, scriptable, or invalid input."""

    source = html.unescape(source).strip()
    if re.fullmatch(
        r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=\s]+", source
    ):
        return re.sub(r"\s+", "", source)
    parsed = urlparse(source)
    if parsed.scheme or parsed.netloc or source.startswith("//") or base_dir is None:
        return None
    raw_path = unquote(parsed.path).replace("/", str(Path("/")))
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    if not resolved.is_relative_to(base_dir.resolve()):
        return None
    suffix = resolved.suffix.lower()
    mime = _SAFE_IMAGE_MIMES.get(suffix)
    if mime is None or resolved.stat().st_size > _MAX_EMBED_IMAGE_BYTES:
        return None
    data = resolved.read_bytes()
    if not _valid_image_bytes(data, suffix):
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _safe_id(value: str) -> str | None:
    value = value.strip()
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", value) else None


def _safe_class(value: str) -> str | None:
    value = " ".join(value.split())
    return value if re.fullmatch(r"[A-Za-z0-9 _-]{1,160}", value) else None


class _ReaderHTMLSanitizer(HTMLParser):
    """Small allow-list sanitizer for already-rendered reader HTML."""

    _allowed = {
        "article", "section", "nav", "div", "span", "p", "h1", "h2", "h3",
        "h4", "h5", "h6", "ul", "ol", "li", "table", "thead", "tbody",
        "tr", "th", "td", "blockquote", "pre", "code", "strong", "em", "b",
        "i", "hr", "br", "figure", "figcaption", "a", "img", "sup", "sub",
        "details", "summary",
    }
    _void = {"hr", "br", "img"}
    _blocked_content = {"script", "style", "iframe", "object", "embed", "template"}

    def __init__(self, base_dir: Path | None) -> None:
        super().__init__(convert_charrefs=True)
        self.base_dir = base_dir
        self.output: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._blocked_content:
            self._blocked_depth += 1
            return
        if self._blocked_depth or tag not in self._allowed:
            return
        clean: list[tuple[str, str]] = []
        values = {name.lower(): value or "" for name, value in attrs}
        identifier = _safe_id(values.get("id", ""))
        class_name = _safe_class(values.get("class", ""))
        if identifier:
            clean.append(("id", identifier))
        if class_name:
            clean.append(("class", class_name))
        if tag == "a":
            href = values.get("href", "").strip()
            if href.startswith("#") and _safe_id(href[1:]):
                clean.append(("href", href))
            if values.get("title"):
                clean.append(("title", values["title"][:300]))
        elif tag == "img":
            src = _local_image_data_uri(values.get("src", ""), self.base_dir)
            if not src:
                self.output.append(
                    f'<span class="reader-image-missing">[图片未嵌入：{html.escape(values.get("alt") or "无效或非本地图片")}]</span>'
                )
                return
            clean.extend((('src', src), ('alt', values.get('alt', '')[:500]), ('loading', 'lazy')))
        elif tag in {"th", "td"}:
            for name in ("colspan", "rowspan"):
                if values.get(name, "").isdigit():
                    clean.append((name, values[name]))
        rendered_attrs = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in clean
        )
        self.output.append(f"<{tag}{rendered_attrs}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._blocked_content:
            if self._blocked_depth:
                self._blocked_depth -= 1
            return
        if not self._blocked_depth and tag in self._allowed and tag not in self._void:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.output.append(html.escape(data))

    def get_html(self) -> str:
        return "".join(self.output)


def _sanitize_reader_html(source: str, base_dir: Path | None) -> str:
    parser = _ReaderHTMLSanitizer(base_dir)
    parser.feed(source)
    parser.close()
    return parser.get_html()


def _render_inline_markdown(text: str) -> str:
    placeholders: list[str] = []

    def keep(fragment: str) -> str:
        placeholders.append(fragment)
        return f"\x00{len(placeholders) - 1}\x00"

    text = re.sub(
        r"`([^`\n]+)`",
        lambda match: keep(f"<code>{html.escape(match.group(1))}</code>"),
        text,
    )
    text = re.sub(
        r"\[([^\]\n]+)\]\(([^)\n]+)\)",
        lambda match: keep(
            f'<a href="#{html.escape(match.group(2)[1:], quote=True)}">{html.escape(match.group(1))}</a>'
            if match.group(2).startswith("#") and _safe_id(match.group(2)[1:])
            else f'<span class="reader-link-text">{html.escape(match.group(1))}</span>'
        ),
        text,
    )
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
    escaped = re.sub(r"(?<!\$)\$([^$\n]+)\$(?!\$)", r'<span class="reader-math">\1</span>', escaped)
    for index, fragment in enumerate(placeholders):
        escaped = escaped.replace(f"\x00{index}\x00", fragment)
    return escaped


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _markdown_to_safe_html(
    source: str,
    base_dir: Path | None,
    *,
    annotations_only: bool = False,
    summary_mode: bool = False,
) -> str:
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    source = re.sub(r"\A---\n.*?\n---\n?", "", source, count=1, flags=re.DOTALL)
    if summary_mode:
        # The speed-reading page does not repeat a full abstract translation or
        # the bibliography. Both remain available in the persistent note.
        source = re.sub(
            r"(?ms)^##\s+(?:原文摘要翻译|引用)\s*$.*?(?=^##\s+|\Z)",
            "",
            source,
        )
    lines = source.splitlines()
    output: list[str] = []
    index = 0
    in_code = False
    code_lines: list[str] = []
    in_math = False
    math_lines: list[str] = []
    summary_section_open = False

    def summary_topic(title: str) -> str:
        if title in {"速览", "核心信息", "创新点", "一句话总结", "研究问题"}:
            return "overview"
        if title in {"方法", "数据与任务定义", "方法主线"}:
            return "method"
        if title in {"结果", "关键结果", "结果与证据"}:
            return "result"
        if title in {"局限", "作者陈述的局限与 AI 判断"}:
            return "limitations"
        if title in {"综述价值", "深度分析", "我的笔记", "图表与公式"}:
            return "review"
        return "other"

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue
        one_line_math = re.fullmatch(r"\$\$(.+)\$\$", stripped)
        if one_line_math:
            output.append(
                '<div class="reader-math reader-math-block" data-display="true">'
                + html.escape(one_line_math.group(1).strip())
                + "</div>"
            )
            index += 1
            continue
        if stripped == "$$":
            if in_math:
                output.append(
                    '<div class="reader-math reader-math-block" data-display="true">'
                    + html.escape(chr(10).join(math_lines).strip())
                    + "</div>"
                )
                math_lines = []
                in_math = False
            else:
                in_math = True
            index += 1
            continue
        if in_math:
            math_lines.append(line)
            index += 1
            continue
        anchor = re.fullmatch(r'<a\s+id=["\']([^"\']+)["\']\s*></a>', stripped, re.IGNORECASE)
        if anchor:
            identifier = _safe_id(anchor.group(1))
            if identifier:
                output.append(f'<span class="source-anchor" id="{identifier}"></span>')
            index += 1
            continue
        image_match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", stripped)
        if image_match:
            alt, image_source = image_match.groups()
            data_uri = _local_image_data_uri(image_source, base_dir)
            if data_uri:
                output.append(
                    f'<figure class="reader-figure"><img src="{data_uri}" alt="{html.escape(alt, quote=True)}" loading="lazy"></figure>'
                )
            else:
                output.append(
                    f'<p class="reader-image-missing">[图片未嵌入：{html.escape(alt or image_source)}]</p>'
                )
            index += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip()
            if summary_mode and level == 1:
                index += 1
                continue
            if summary_mode and level == 2:
                if summary_section_open:
                    output.append("</section>")
                topic = summary_topic(re.sub(r"[*_`]", "", heading_text))
                output.append(
                    f'<section class="summary-section" data-summary-topic="{topic}">'
                    f'<h2>{_render_inline_markdown(heading_text)}</h2>'
                )
                summary_section_open = True
            else:
                output.append(f"<h{level}>{_render_inline_markdown(heading_text)}</h{level}>")
            index += 1
            continue
        if stripped.startswith("|") and index + 1 < len(lines) and re.match(
            r"^\s*\|?\s*:?-{3,}", lines[index + 1]
        ):
            headers = _split_table_row(line)
            index += 2
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                rows.append(_split_table_row(lines[index]))
                index += 1
            output.append("<div class=\"reader-table-wrap\"><table><thead><tr>" + "".join(
                f"<th>{_render_inline_markdown(cell)}</th>" for cell in headers
            ) + "</tr></thead><tbody>" + "".join(
                "<tr>" + "".join(f"<td>{_render_inline_markdown(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            ) + "</tbody></table></div>")
            continue
        list_match = re.match(r"^\s*(?:[-+*]|(\d+)\.)\s+(.+)$", line)
        if list_match:
            ordered = bool(list_match.group(1))
            tag = "ol" if ordered else "ul"
            items: list[str] = []
            while index < len(lines):
                current = re.match(r"^\s*(?:[-+*]|(\d+)\.)\s+(.+)$", lines[index])
                if not current or bool(current.group(1)) != ordered:
                    break
                items.append(current.group(2))
                index += 1
            output.append(f"<{tag}>" + "".join(f"<li>{_render_inline_markdown(item)}</li>" for item in items) + f"</{tag}>")
            continue
        if stripped.startswith(">"):
            output.append(f"<blockquote>{_render_inline_markdown(stripped.lstrip('>').strip())}</blockquote>")
            index += 1
            continue
        if re.fullmatch(r"-{3,}", stripped):
            output.append("<hr>")
            index += 1
            continue
        if not stripped:
            index += 1
            continue
        paragraph = [stripped]
        index += 1
        while index < len(lines) and lines[index].strip() and not re.match(
            r"^(?:#{1,6}\s|```|\$\$|\s*(?:[-+*]|\d+\.)\s+|\s*>|\s*\||!\[|<a\s+id=)",
            lines[index],
            flags=re.IGNORECASE,
        ):
            paragraph.append(lines[index].strip())
            index += 1
        paragraph_text = " ".join(paragraph)
        original_match = re.match(
            r"^\*\*(?:Original|English)(?: caption)?\s*:\*\*\s*(.*)$",
            paragraph_text,
            flags=re.IGNORECASE,
        )
        chinese_match = re.match(
            r"^\*\*(?:中文|中文图注|Chinese)(?: translation)?\s*:\*\*\s*(.*)$",
            paragraph_text,
            flags=re.IGNORECASE,
        )
        if original_match:
            output.append(
                '<div class="key-english"><div class="source-label">关键英文信息 · AI整理</div><p>'
                + _render_inline_markdown(original_match.group(1))
                + "</p></div>"
            )
        elif chinese_match:
            output.append(
                '<div class="ai-annotation"><div class="annotation-label">AI理解与阅读提示</div><p>'
                + _render_inline_markdown(chinese_match.group(1))
                + "</p></div>"
            )
        else:
            output.append(f"<p>{_render_inline_markdown(paragraph_text)}</p>")

    if in_code:
        output.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    if in_math:
        output.append(
            '<div class="reader-math reader-math-block" data-display="true">'
            + html.escape(chr(10).join(math_lines).strip())
            + "</div>"
        )
    if summary_section_open:
        output.append("</section>")
    return "\n".join(output)


def _split_reader(markdown: str) -> tuple[str, str, str]:
    """Split one bound note into (reading note, original-page block, claims block).

    The note written by the reading step repeats the eight review sections that
    the review column already shows as judgment cards.  Rendering both would
    double the reading load, so the repeated block is dropped here and only the
    overview part reaches the reading pane.
    """

    text = markdown
    page_block = ""
    match = _PAGE_SECTION_RE.search(text)
    if match:
        page_block = text[match.end():]
        text = text[:match.start()]
    claims_block = ""
    numbered = _NUMBERED_SECTION_RE.search(text)
    if numbered:
        tail = text[numbered.start():]
        text = text[:numbered.start()]
        claims_match = _CLAIMS_HEADING_RE.search(tail)
        if claims_match:
            claims_block = tail[claims_match.start():]
    return text.strip(), page_block, claims_block


def _label_kind_number(label: str) -> tuple[str, int | None]:
    kind = "table" if "table" in label.lower() else "figure"
    local = re.search(r"(\d+)", label)
    return kind, int(local.group(1)) if local else None


def _decision_target(decision: Mapping[str, Any]) -> tuple[str | None, int | None]:
    """Which figure/table a decision is actually about, read from its reason.

    The decision ``id`` is a position on the page (``p10-f5`` = fifth item the
    reading step looked at on page 10), not a figure number, so pairing crops by
    that index silently shifts the whole page — a real ``Fig. 5`` ends up
    wearing an ``omit`` badge meant for a passing "see Fig. S2" reference.
    """

    text = str(decision.get("reason") or "")
    table = _TABLE_MENTION_RE.search(text)
    if table is not None:
        return "table", int(table.group(1))
    figure = _FIG_MENTION_RE.search(text)
    if figure is not None:
        return "figure", int(figure.group(1))
    return None, None


def _is_false_positive_decision(decision: Mapping[str, Any]) -> bool:
    """A dropped inventory row describing a detection error owns no crop at all.

    Such a reason still *mentions* a figure number ("正文对Table1的指引误检"), so
    letting it reach the pairing loop lets it claim the genuine Table 1 crop and
    stamp an "omit" badge on a core figure.  Skipping the row entirely is the
    only correct behaviour: there is no image behind a false-positive row.
    """

    if str(decision.get("decision") or "").strip() != "omit":
        return False
    return _NEGATIVE_FIGURE_REASON_RE.search(str(decision.get("reason") or "")) is not None


def _manifest_captions(base_dir: Path) -> dict[str, str]:
    """Original PDF captions recorded by the verified-crop tool, keyed by file."""

    manifest = base_dir / "reader" / "figure-crops.json"
    if not manifest.is_file():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    captions: dict[str, str] = {}
    for row in data.get("rows") or []:
        if not isinstance(row, Mapping) or row.get("status") != "verified":
            continue
        asset = row.get("asset") or {}
        name = str(asset.get("path") or "") if isinstance(asset, Mapping) else ""
        if name:
            captions[Path(name).name] = str(row.get("caption") or "")[:300]
    return captions


def _collect_figures(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Show every verified crop; treat figure decisions as annotations, not a whitelist.

    Older reading results reused one boilerplate reason for all decisions (some
    mentioned an unrelated "Fig9").  Making decisions the *only* source of cards
    meant a single crop claimed them all and the rest of the page stayed blank.
    The crop files are themselves verified real PDF regions, so each one gets a
    card; a decision only attaches its verdict/reason when it names that exact
    figure on the same page.  Unannotated crops fall back to their original PDF
    caption instead of silently disappearing.
    """

    base_dir = _reader_base_dir(record)
    if base_dir is None:
        return []
    assets = base_dir / "reader" / "assets"
    if not assets.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(assets.iterdir()):
        match = _FIGURE_FILE_RE.fullmatch(path.name)
        if match is None:
            continue
        label = match.group(2).replace("_", " ").strip()
        kind, number = _label_kind_number(label)
        entries.append(
            {"path": path, "page": int(match.group(1)), "label": label, "kind": kind, "number": number}
        )
    captions = _manifest_captions(base_dir)
    claims: dict[int, Mapping[str, Any]] = {}
    decisions = [d for d in (record.get("figure_decisions") or []) if isinstance(d, Mapping)]
    decisions.sort(key=lambda item: 0 if _decision_target(item)[0] else 1)
    for decision in decisions:
        if _is_false_positive_decision(decision):
            continue
        kind, number = _decision_target(decision)
        if kind is None or number is None:
            continue
        parsed = _FIGURE_ID_RE.fullmatch(str(decision.get("id") or ""))
        page = int(parsed.group(1)) if parsed else None
        for index, entry in enumerate(entries):
            if index in claims or entry["kind"] != kind or entry["number"] != number:
                continue
            if page is not None and abs(entry["page"] - page) > 1:
                continue
            claims[index] = decision
            break

    figures: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        try:
            relative = entry["path"].relative_to(base_dir)
        except ValueError:
            relative = entry["path"]
        source = _local_image_data_uri(str(relative), base_dir)
        if not source:
            continue
        decision = claims.get(index)
        figures.append(
            {
                "label": entry["label"],
                "page": entry["page"],
                "decision": str(decision.get("decision") or "include") if decision else "include",
                "reason": str(decision.get("reason") or "").strip() if decision else "",
                "pdf_caption": captions.get(entry["path"].name, ""),
                "src": source,
            }
        )
    figures.sort(key=lambda item: (item["page"], item["label"]))
    return figures


def _figure_sentences(record: Mapping[str, Any]) -> dict[tuple[str, int], str]:
    """Split the note's figure commentary into one description per figure/table.

    Figures and tables share the numbering space (a paper has both ``Fig. 1``
    and ``Table 1``), so the key carries the kind.
    """

    sections = record.get("sections") or {}
    text = ""
    if isinstance(sections, Mapping):
        supplied = sections.get("figures_equations")
        if isinstance(supplied, Mapping):
            text = str(supplied.get("ai_content") or "")
        elif isinstance(supplied, str):
            text = supplied
    if not text.strip():
        return {}
    parts = re.split(r"[；;。\n]+", text)
    sentences: dict[tuple[str, int], str] = {}
    for part in parts:
        sentence = part.strip().strip("，, ")
        if not sentence:
            continue
        table = _TABLE_MENTION_RE.search(sentence)
        if table is not None:
            key = ("table", int(table.group(1)))
        else:
            figure = _FIG_MENTION_RE.search(sentence)
            if figure is None:
                continue
            key = ("figure", int(figure.group(1)))
        if key in sentences:
            sentences[key] += "；" + sentence
        else:
            sentences[key] = sentence
    return sentences


def _caption_text(sentence: str, reason: str, limit: int = 200) -> str:
    """Keep the caption to one sentence a reader can finish on the spot.

    The note's own sentence wins when it is short enough; when the notes pack
    several figures into one long clause, the decision's single-sentence reason
    is what actually talks about this figure.
    """

    for candidate in (sentence, reason):
        text = (candidate or "").strip()
        if text and len(text) <= limit:
            break
    else:
        text = (sentence or reason or "").strip()
    # Captions read better when they start at the figure id rather than at a
    # preamble like "六幅主图全部承担证据功能：".
    mention = _TABLE_MENTION_RE.search(text) or _FIG_MENTION_RE.search(text)
    if mention is not None and mention.start() > 0:
        text = text[mention.start() :]
    return text


def _figure_card(figure: Mapping[str, Any], sentence: str) -> str:
    included = figure["decision"] != "omit"
    badge = (
        '<span class="badge inc">纳入证据</span>'
        if included
        else '<span class="badge omit">不作正文图</span>'
    )
    caption = _caption_text(
        sentence, str(figure.get("reason") or figure.get("pdf_caption") or "")
    )
    return (
        f'<figure class="fig" data-label="{html.escape(str(figure["label"]), quote=True)}" '
        f'data-page="{figure["page"]}">'
        f'<img src="{figure["src"]}" '
        f'alt="{html.escape(str(figure["label"]) + " 原文第 " + str(figure["page"]) + " 页", quote=True)}" loading="lazy">'
        '<figcaption class="fig-c">'
        f'<div class="fig-t">{badge}<span>{html.escape(str(figure["label"]))}</span>'
        f'<span class="pg">原文第 {figure["page"]} 页</span></div>'
        f'<div class="fig-r">{html.escape(caption)}</div>'
        "</figcaption></figure>"
    )


def _figure_cards(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One self-contained card per cropped figure, carrying the note's own words."""

    figures = _collect_figures(record)
    if not figures:
        return []
    sentences = _figure_sentences(record)
    cards: list[dict[str, Any]] = []
    for figure in figures:
        label = str(figure["label"])
        kind = "table" if "table" in label.lower() else "figure"
        local = re.search(r"(\d+)", label)
        number = int(local.group(1)) if local else None
        caption = sentences.get((kind, number) if number is not None else ("", 0), "")
        pdf_caption = str(figure.get("pdf_caption") or "")
        cards.append(
            {
                "number": number,
                "kind": kind,
                "label": label,
                "page": int(figure["page"]),
                "caption": _caption_text(caption, str(figure.get("reason") or pdf_caption)),
                "description": f"{caption} {figure['reason']} {pdf_caption}".strip(),
                "included": figure["decision"] != "omit",
                "html": _figure_card(figure, caption),
            }
        )
    return cards


def _section_spans(note_html: str) -> list[dict[str, Any]]:
    """Every summary section as ``{topic, start, close}`` offsets."""

    spans: list[dict[str, Any]] = []
    for match in _SUMMARY_SECTION_RE.finditer(note_html):
        close = note_html.find("</section>", match.end())
        if close < 0:
            continue
        spans.append({"topic": match.group(1).lower(), "start": match.start(), "close": close})
    return spans


def _topic_pages(record: Mapping[str, Any]) -> dict[str, set[int]]:
    """The pages the reading step itself attached to the method/result sections."""

    supplied = record.get("sections") or {}
    pages: dict[str, set[int]] = {}
    if isinstance(supplied, Mapping):
        for key, topic in (("methods_data", "method"), ("results_evidence", "result")):
            payload = supplied.get(key)
            raw = payload.get("source_pages") if isinstance(payload, Mapping) else None
            pages[topic] = {int(page) for page in (raw or []) if str(page).isdigit()}
    return pages


# A figure whose page is annotated in neither section still has to land
# somewhere; these are the words the note itself uses to describe a figure's
# job, so the fallback stays auditable instead of guessing.
_METHOD_HINTS = ("流程", "工作流", "框架", "架构", "总纲", "示意", "超参", "特征集")
_RESULT_HINTS = ("散点", "混淆", "重要性", "曲线", "对比", "验证", "violin", "分布", "预测")


def _figure_topic(card: Mapping[str, Any], pages: Mapping[str, set[int]]) -> str | None:
    page = card["page"]
    method_pages = pages.get("method") or set()
    result_pages = pages.get("result") or set()
    if page in method_pages and page not in result_pages:
        return "method"
    if page in result_pages:
        return "result"
    if page in method_pages:
        return "method"
    text = str(card["description"])
    method_hits = sum(hint in text for hint in _METHOD_HINTS)
    result_hits = sum(hint.lower() in text.lower() for hint in _RESULT_HINTS)
    if method_hits > result_hits:
        return "method"
    if result_hits > method_hits:
        return "result"
    return None


def _sentence_end(body: str, start: int) -> int | None:
    """First depth-0 Chinese sentence end after ``start`` inside a block body.

    Only positions outside any inline element qualify, so splitting a paragraph
    there can never cut a KaTeX span or a link in half.
    """

    depth = 0
    index = start
    while index < len(body):
        char = body[index]
        if char == "<":
            close = body.find(">", index)
            if close < 0:
                return None
            tag = body[index : close + 1]
            if tag.startswith("</"):
                depth = max(0, depth - 1)
            elif not tag.endswith("/>"):
                depth += 1
            index = close + 1
            continue
        if depth == 0 and char in "。！？":
            return index + 1
        index += 1
    return None


def _inline_figures(
    note_html: str, cards: list[dict[str, Any]], record: Mapping[str, Any]
) -> tuple[str, list[dict[str, Any]]]:
    """Keep every picture next to the passage that uses it, inside one scroll.

    Placement is decided from the record, never from guesswork:

    * a figure the note itself cites by number (``Fig. 2`` / ``Table 1``) lands
      right after the sentence that cites it — words and picture share a screen;
    * otherwise the page annotations the reading step attached to
      ``methods_data`` / ``results_evidence`` decide whether the figure closes
      the method section or the result section;
    * figures whose page belongs to neither are collected at the end so nothing
      silently disappears.
    """

    if not cards:
        return note_html, []
    spans = _section_spans(note_html)
    if not spans:
        return note_html, list(cards)
    by_topic = {span["topic"]: span for span in spans}
    pages = _topic_pages(record)

    by_key = {
        (card["kind"], card["number"]): card for card in cards if card["number"] is not None
    }
    placed: set[tuple[str, int]] = set()
    # offset -> {"html": [...], "tag": 开标签原文 or None 表示直接插在块之后}
    slots: dict[int, dict[str, Any]] = {}

    def add(offset: int, card: dict[str, Any], tag: str | None = None) -> None:
        slot = slots.setdefault(offset, {"html": [], "tag": tag})
        slot["html"].append(card["html"])
        placed.add((card["kind"], card["number"]))

    # Pass 1 — an explicit "Table 1 / Fig. 3" inside the argument wins, and the
    # card goes after that very sentence rather than after the whole paragraph.
    for match in _BLOCK_RE.finditer(note_html):
        topic = next(
            (span["topic"] for span in spans if span["start"] <= match.start() < span["close"]),
            None,
        )
        if topic not in {"method", "result"}:
            continue
        raw = match.group(0)
        open_end = raw.find(">") + 1
        close_at = raw.rfind("</")
        if open_end <= 0 or close_at < open_end:
            continue
        body = raw[open_end:close_at]
        open_tag = raw[:open_end]
        texts = {"table": body, "figure": body}
        for kind, pattern in (("table", _TABLE_MENTION_RE), ("figure", _FIG_MENTION_RE)):
            for hit in pattern.finditer(texts[kind]):
                key = (kind, int(hit.group(1)))
                if key not in by_key or key in placed:
                    continue
                cut = _sentence_end(body, hit.end())
                if cut is None:
                    add(match.end(), by_key[key])
                else:
                    add(match.start() + open_end + cut, by_key[key], open_tag)

    # Pass 2 — page annotations decide which section the figure closes.
    for card in sorted(
        (c for c in cards if (c["kind"], c["number"]) not in placed),
        key=lambda c: (c["page"], c["number"] or 0),
    ):
        topic = _figure_topic(card, pages)
        span = by_topic.get(topic) if topic else None
        if span is None:
            continue
        add(span["close"], card)

    orphans = [card for card in cards if (card["kind"], card["number"]) not in placed]
    rendered = note_html
    for offset in sorted(slots, reverse=True):
        slot = slots[offset]
        block = "".join(slot["html"])
        if slot["tag"]:
            # Close the hosting paragraph, show the figure, reopen the same tag.
            tag = slot["tag"]
            name = re.match(r"<\s*([a-zA-Z0-9]+)", tag)
            closer = f"</{name.group(1)}>" if name else "</p>"
            rendered = rendered[:offset] + closer + block + tag + rendered[offset:]
        else:
            rendered = rendered[:offset] + block + rendered[offset:]
    return rendered, orphans


def _render_figure_flow(
    cards: list[dict[str, Any]], dropped: list[dict[str, Any]] | None = None
) -> str:
    """Two tails after the reading flow: figures it never cited, and dropped ones."""

    blocks: list[str] = []
    if cards:
        blocks.append(
            '<section class="summary-section" data-summary-topic="figs">'
            "<h2>未在正文点到的图</h2>"
            '<p class="fig-lead">这些图表正文没有逐张讨论，按原文顺序附在这里；点图可放大。</p>'
            + "".join(card["html"] for card in cards)
            + "</section>"
        )
    if dropped:
        blocks.append(
            '<section class="summary-section" data-summary-topic="figs">'
            "<h2>AI 判为不作正文图的图表</h2>"
            '<p class="fig-lead">这些图表被判为不承担正文证据，列出供你复核；'
            "若不同意，可在第 5 节改判后重新提交。</p>"
            + "".join(card["html"] for card in dropped)
            + "</section>"
        )
    return "".join(blocks)


def _inject_figure_flow(note_html: str, block: str) -> str:
    if not block:
        return note_html
    anchor = note_html.find('data-summary-topic="limitations"')
    if anchor > 0:
        cut = note_html.rfind("<section", 0, anchor)
        if cut > 0:
            return note_html[:cut] + block + "\n" + note_html[cut:]
    return note_html + "\n" + block


def _figure_gap_notice(record: Mapping[str, Any], card_count: int) -> str:
    """Say *why* a page has no (or too few) figures, with countable evidence.

    ``figure_decisions`` is only a list of intentions.  When the reading step
    never produced ``reader/assets/`` crops, silently returning an empty list
    leaves the reviewer staring at a text-only page with no way to tell a
    missing asset from a paper that genuinely has no figures.
    """

    decisions = [d for d in (record.get("figure_decisions") or []) if isinstance(d, Mapping)]
    expected = sum(
        1
        for d in decisions
        if str(d.get("decision") or "include") != "omit" and not _is_false_positive_decision(d)
    )
    base_dir = _reader_base_dir(record)
    assets = base_dir / "reader" / "assets" if base_dir else None
    crops = (
        [p for p in sorted(assets.iterdir()) if _FIGURE_FILE_RE.fullmatch(p.name)]
        if assets is not None and assets.is_dir()
        else []
    )
    manifest = assets.parent / "figure-crops.json" if assets is not None else None
    if manifest is not None and manifest.is_file() and crops:
        # 裁剪清单是 PDF 图题层面的真值；图决策条数是另一套口径，不能用它
        # 反推「缺了几张图」。只报告清单里逐条写明原因的真实缺失项。
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if not isinstance(data, Mapping):
            data = None
        missing_rows = [
            row for row in (data.get("rows") or [])
            if isinstance(row, Mapping) and row.get("status") == "missing"
        ] if data else []
        if not missing_rows:
            return ""
        details = "；".join(
            f"{row.get('label', '?')}（原文第 {row.get('page', '?')} 页："
            f"{str(row.get('missing_reason') or '原因未记录')}）"
            for row in missing_rows
        )
        lines = [
            f"从原 PDF 检出 {data.get('captions', len(missing_rows) + len(crops))} 条图题，"
            f"其中 {data.get('verified', len(crops))} 张通过身份核验并已内嵌；"
            f"以下 {len(missing_rows)} 张无法从当前 PDF 提取：{details}。",
            "原页整图保存在阅读产物的 source-pages 目录，是哈希锚定的整页渲染，"
            "不是单图裁图；按约定评审页不显示整页原文，不能拿来兜底。",
            "逐图原因见 reader/figure-crops.json；未提取的部分不臆造、不重绘。",
        ]
        return (
            '<div class="fig-missing" role="note" data-figure-gap="1">'
            '<div class="fig-missing-h">部分图表缺失：无法从当前 PDF 提取</div>'
            + "".join(f"<p>{html.escape(line)}</p>" for line in lines)
            + "</div>"
        )
    if expected == 0 or card_count >= expected:
        return ""
    if not crops:
        cause = (
            f"本任务没有生成单图裁图资产（reader/assets/ 不存在或为空）；阅读结果里 "
            f"{expected} 条「纳入证据」的图表决策因此全部落空。"
        )
    else:
        cause = (
            f"reader/assets/ 只有 {len(crops)} 张裁图，{expected} 条「纳入证据」的图表决策"
            f"仅 {card_count} 条完成配对；其余裁图缺失或身份核验未通过。"
        )
    lines = [
        cause,
        "原页整图保存在阅读产物的 source-pages 目录，是哈希锚定的整页渲染，不是单图裁图；"
        "按约定评审页不显示整页原文，不能拿来兜底。",
    ]
    manifest = assets.parent / "figure-crops.json" if assets is not None else None
    if manifest is not None and manifest.is_file():
        lines.append("逐图原因见 reader/figure-crops.json。")
    lines.append("图表缺失：无法从当前 PDF 提取的部分不臆造、不重绘；请从原 PDF 重新裁图并重渲染。")
    return (
        '<div class="fig-missing" role="note" data-figure-gap="1">'
        '<div class="fig-missing-h">图表缺失：本页未能显示原文图表</div>'
        + "".join(f"<p>{html.escape(line)}</p>" for line in lines)
        + "</div>"
    )




def _render_claims(record: Mapping[str, Any]) -> str:
    claims = record.get("claims") or []
    if not claims:
        return '<p class="reader-empty">这篇论文还没有抽取候选主张。</p>'
    kind_styles = {
        "source_statement": ("原文陈述", "src"),
        "ai_inference": ("AI 推断", "inf"),
        "ai_verified": ("AI 核验主张", "inf"),
    }
    cards = []
    for claim in claims:
        if not isinstance(claim, Mapping):
            continue
        kind = str(claim.get("kind") or "source_statement")
        label, tone = kind_styles.get(kind, ("原文陈述", "src"))
        pages = [
            str(page) for page in (claim.get("source_pages") or []) if str(page).isdigit()
        ]
        chips = (
            '<span class="claim-pg">原文 ' + "、".join(f"p.{page}" for page in pages) + "</span>"
            if pages
            else ""
        )
        claim_id = str(claim.get("claim_id") or "")
        checkbox_id = "claim-" + re.sub(r"[^A-Za-z0-9_-]", "", claim_id)
        cards.append(
            '<div class="claim">'
            f'<input type="checkbox" id="{html.escape(checkbox_id, quote=True)}" value="{html.escape(claim_id, quote=True)}">'
            "<div>"
            '<div class="claim-h">'
            f'<label for="{html.escape(checkbox_id, quote=True)}"><span class="claim-id">{html.escape(claim_id)}</span></label>'
            f'<span class="badge {tone}">{label}</span>'
            "</div>"
            f'<label class="claim-x" for="{html.escape(checkbox_id, quote=True)}">{html.escape(str(claim.get("text") or ""))}</label>'
            f'<div class="claim-f">{chips}'
            + ("<p>来源：" + html.escape("；".join(claim["source_anchors"])) + "</p>" if claim.get("source_anchors") else "")
            + ("<p>适用范围：" + html.escape(claim["limitations"]) + "</p>" if claim.get("limitations") else "")
            + "</div>"
            "</div></div>"
        )
    return (
        '<p class="claims-note">勾选你已核验、并允许写入综述的主张；勾选结果会自动填进第 8 节的'
        "「已批准的具体主张」。未勾选的主张只作为线索，不会被引用。</p>"
        f'<div class="claims">{"".join(cards)}</div>'
    )


def _render_reader(record: Mapping[str, Any]) -> tuple[str, str]:
    """Return (note html, claims html)."""

    markdown = record.get("reader_markdown")
    rendered_html = record.get("reader_html")
    if markdown is not None and not isinstance(markdown, str):
        raise ReviewRecordError("reader_markdown must be a string")
    if rendered_html is not None and not isinstance(rendered_html, str):
        raise ReviewRecordError("reader_html must be a string")
    base_dir = _reader_base_dir(record)
    if isinstance(markdown, str) and markdown.strip():
        note, _page_block, claims_block = _split_reader(markdown)
        claims_html = _render_claims(record)
        if claims_html.startswith('<p class="reader-empty"'):
            legacy_claims = _render_legacy_claims(claims_block, base_dir)
            if legacy_claims:
                claims_html = legacy_claims
        cards = _figure_cards(record)
        note_html, leftover = _inline_figures(
            _markdown_to_safe_html(note, base_dir, summary_mode=True),
            [card for card in cards if card["included"]],
            record,
        )
        dropped = [card for card in cards if not card["included"]]
        note_html = _inject_figure_flow(note_html, _render_figure_flow(leftover, dropped))
        notice = _figure_gap_notice(record, len(cards))
        if notice:
            note_html = notice + "\n" + note_html
        return note_html, claims_html
    if isinstance(rendered_html, str) and rendered_html.strip():
        return (
            _sanitize_reader_html(rendered_html, base_dir),
            _render_claims(record),
        )
    return (
        '<p class="reader-empty">尚未嵌入全文阅读材料。</p>',
        _render_claims(record),
    )


def _render_legacy_claims(block: str, base_dir: Path | None) -> str:
    """Render the note's own claim list when the structured record carries none."""

    if not block.strip():
        return ""
    body = re.sub(r"(?m)^##\s*候选主张.*$", "", block, count=1).strip()
    return (
        '<p class="claims-note">以下主张摘自阅读笔记，可在第 8 节手工填写批准清单。</p>'
        f'<div class="claims">{_markdown_to_safe_html(body, base_dir)}</div>'
    )



def _clean_identity(record: Mapping[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for field in _REQUIRED_IDENTITY_FIELDS:
        value = record.get(field)
        if isinstance(value, (list, tuple, set, dict)):
            raise ReviewRecordError(f"{field} must identify one paper, not a collection")
        if not isinstance(value, str) or not value.strip():
            raise ReviewRecordError(f"missing non-empty single-paper field: {field}")
        identity[field] = value.strip()
    if not re.fullmatch(r"paper-[0-9a-f]{16}", identity["job_id"]):
        raise ReviewRecordError("job_id must match paper-[0-9a-f]{16}")

    for output_name, primary_name, legacy_name in (
        ("pdf_sha256", "pdf_sha256", "pdf_hash"),
        ("ai_note_sha256", "ai_note_sha256", "note_hash"),
    ):
        value = record.get(primary_name, record.get(legacy_name))
        if isinstance(value, (list, tuple, set, dict)):
            raise ReviewRecordError(f"{primary_name} must identify one artifact")
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise ReviewRecordError(f"{primary_name} must be a 64-character lowercase SHA-256")
        identity[output_name] = value
    return identity


def normalize_paper_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the structured input for exactly one paper."""

    if not isinstance(record, Mapping):
        raise ReviewRecordError("paper record must be a mapping")
    normalized: dict[str, Any] = _clean_identity(record)
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["title"] = str(record.get("title") or normalized["paper_id"]).strip()
    normalized["citation"] = str(record.get("citation") or "").strip()
    normalized["human_review_dir"] = str(record.get("human_review_dir") or "").strip()
    markdown = record.get("reader_markdown")
    normalized["zh_title"] = _chinese_title(markdown, normalized["title"])
    normalized["reading_minutes"] = _reading_minutes(markdown)

    supplied_sections = record.get("sections") or {}
    if not isinstance(supplied_sections, Mapping):
        raise ReviewRecordError("sections must be an object keyed by section id")

    known_ids = {section["id"] for section in EIGHT_SECTIONS}
    unknown_ids = set(supplied_sections) - known_ids
    if unknown_ids:
        raise ReviewRecordError(f"unknown section ids: {', '.join(sorted(unknown_ids))}")

    sections: list[dict[str, Any]] = []
    for definition in EIGHT_SECTIONS:
        supplied = supplied_sections.get(definition["id"], {})
        if isinstance(supplied, str):
            supplied = {"ai_content": supplied}
        if not isinstance(supplied, Mapping):
            raise ReviewRecordError(f"section {definition['id']} must be text or an object")
        sections.append(
            {
                **definition,
                "ai_content": str(
                    supplied.get("ai_content")
                    or supplied.get("content")
                    or supplied.get("summary")
                    or "本节暂无AI分析，请结合原文作出判断。"
                ).strip(),
                "source_anchors": str(supplied.get("source_anchors") or "").strip(),
                "source_pages": _page_numbers(supplied.get("source_pages")),
            }
        )
    normalized["sections"] = sections
    normalized["claims"] = _clean_claims(record.get("claims"))
    normalized["figure_decisions"] = _clean_figures(record.get("figure_decisions"))
    return normalized


def _chinese_title(markdown: Any, fallback: str) -> str:
    if not isinstance(markdown, str):
        return ""
    headings = [value.strip() for value in _H1_RE.findall(markdown)]
    for heading in headings[1:]:
        if heading and heading != fallback and re.search(r"[\u4e00-\u9fff]", heading):
            return heading
    return ""


def _reading_minutes(markdown: Any) -> str:
    if not isinstance(markdown, str):
        return ""
    note, _, _ = _split_reader(markdown)
    chars = len(re.sub(r"\s+", "", note))
    if chars < 200:
        return ""
    minutes = max(4, round(chars / 300))
    return f"{max(3, minutes - 2)}–{minutes + 3}"


def _page_numbers(value: Any) -> list[int]:
    numbers: set[int] = set()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                numbers.add(int(item))
            elif isinstance(item, str) and item.strip().isdigit():
                numbers.add(int(item.strip()))
    return sorted(number for number in numbers if 0 < number < 1000)


def _clean_claims(value: Any) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    if not isinstance(value, (list, tuple)):
        return claims
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identifier = str(item.get("claim_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not identifier or not text:
            continue
        claims.append(
            {
                "claim_id": identifier,
                "text": text,
                "kind": str(item.get("kind") or "source_statement").strip(),
                "source_pages": _page_numbers(item.get("source_pages")),
            }
        )
    return claims


def _clean_figures(value: Any) -> list[dict[str, Any]]:
    figures: list[dict[str, Any]] = []
    if not isinstance(value, (list, tuple)):
        return figures
    for item in value:
        if not isinstance(item, Mapping):
            continue
        identifier = str(item.get("id") or "").strip()
        if not identifier:
            continue
        figures.append(
            {
                "id": identifier,
                "decision": str(item.get("decision") or "include").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return figures



def _json_for_html(value: Any) -> str:
    # Prevent embedded data from terminating the application/json script tag.
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_review_html(record: Mapping[str, Any]) -> str:
    """Return a self-contained offline HTML review page for one paper."""

    normalized = normalize_paper_record(record)
    try:
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"review template not found: {_TEMPLATE_PATH}") from exc
    try:
        katex_js = _KATEX_JS_PATH.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"offline KaTeX runtime not found: {_KATEX_JS_PATH}") from exc
    katex_js = re.sub(r"</script", r"<\\/script", katex_js, flags=re.IGNORECASE)
    note_html, claims_html = _render_reader(record)
    replacements = {
        "__PAPER_RECORD_JSON__": _json_for_html(normalized),
        _READER_MARKER: note_html,
        "__CLAIMS_HTML__": claims_html,
        "__KATEX_RUNTIME_JS__": katex_js,
    }
    for marker in replacements:
        if template.count(marker) != 1:
            raise RuntimeError(f"review template must contain exactly one {marker} marker")
    rendered = template
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value, 1)
    # Older templates carried separate figure and source-page panes; figures now
    # live in the reading flow and source pages are no longer rendered at all.
    return rendered.replace("__FIGURES_HTML__", "").replace("__PAGES_HTML__", "")



def write_review_html(record: Mapping[str, Any], output_path: str | Path) -> Path:
    """Write a single-paper offline page and return its resolved path."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review_html(record), encoding="utf-8")
    return path.resolve()


def _main() -> int:
    parser = argparse.ArgumentParser(description="Generate one offline paper-review page")
    parser.add_argument("record", type=Path, help="single-paper JSON input")
    parser.add_argument("output", type=Path, help="output .html path")
    args = parser.parse_args()
    record = json.loads(args.record.read_text(encoding="utf-8"))
    write_review_html(record, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
