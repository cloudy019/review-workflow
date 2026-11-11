#!/usr/bin/env python
"""从单篇论文 PDF 中定位并裁剪图/表区域，输出 PNG 与可追溯清单 JSON。

约束（遵循 docs/CONTRACTS.md）：
- 源 PDF 只读；不复制 PDF 进项目目录。
- 输出 PNG 全部来自原始 PDF 页面的真实渲染，不做任何重绘、推断或"美化"。
- 定位失败时写明确的 missing_reason，绝不生成占位图或 AI 生成图。

子命令：
  scan    扫描全文或指定页，列出所有检测到的图/表候选与图块方便判断
  crop    按 --include 指定的图号裁剪渲染，输出清单 JSON

用法：
  python extract_figures.py scan  --pdf <PDF> --json-out out.json [--pages 3,4]
  python extract_figures.py crop  --pdf <PDF> --out-dir <dir> --json-out out.json \
      [--pages 3,4] [--include "Fig 1|Fig 2|Table 1"] [--dpi 200]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF

CAPTION_RE = re.compile(
    r"^\s*(?P<label>(?:(?:Fig(?:ure)?|FIG|Scheme|Table|TABLE|"
    r"Supplementary\s+Fig(?:ure)?|SI\s+Fig(?:ure)?)"
    r"\.?\s*(?P<number>[0-9]+[A-Za-z]?(?:\.[0-9]+)?))\b)"
)

MIN_CLUSTER_AREA = 2000.0  # pt^2，过滤装饰线条
MAX_GAP_BELOW = 120.0  # pt，图块底部到图题顶部的最大距离
MAX_GAP_ABOVE = 70.0  # pt，图题底部到图块顶部的最大距离（图题在上）
MIN_REGION_W = 60.0
MIN_REGION_H = 45.0


# ---------------------------------------------------------------- 基础提取 ---


def line_boxes(page: fitz.Page):
    lines = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type", 0) != 0:
            continue
        for line in block.get("lines", []):
            spans = [span for span in line.get("spans", []) if span.get("text", "").strip()]
            if not spans:
                continue
            bbox = (
                min(span["bbox"][0] for span in spans),
                min(span["bbox"][1] for span in spans),
                max(span["bbox"][2] for span in spans),
                max(span["bbox"][3] for span in spans),
            )
            lines.append(
                {"text": "".join(span["text"] for span in spans).strip(), "bbox": bbox}
            )
    lines.sort(key=lambda item: (round(item["bbox"][1], 1), round(item["bbox"][0], 1)))
    return lines


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _intersects(a, b, pad=0.0):
    return not (
        a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1]
    )


def _area(rect):
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def graphic_clusters(page: fitz.Page):
    rects = []
    for drawing in page.get_drawings():
        r = drawing.get("rect")
        if r and (r.width > 0 or r.height > 0):
            rects.append((r.x0, r.y0, r.x1, r.y1))
    try:
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            if bbox:
                rects.append(tuple(bbox))
    except Exception:
        pass

    rects = [r for r in rects if _area(r) >= 4.0]
    clusters: list[tuple[float, float, float, float]] = []
    for rect in rects:
        for index, cluster in enumerate(clusters):
            if _intersects(rect, cluster, pad=10.0):
                clusters[index] = _union(cluster, rect)
                break
        else:
            clusters.append(rect)

    for _ in range(3):
        changed = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if clusters[i] != clusters[j] and _intersects(clusters[i], clusters[j], pad=8.0):
                    merged = _union(clusters[i], clusters[j])
                    clusters[i] = merged
                    clusters[j] = merged
                    changed = True
        unique: list[tuple[float, float, float, float]] = []
        for cluster in clusters:
            if cluster not in unique:
                unique.append(cluster)
        clusters = unique
        if not changed:
            break

    return sorted(
        [c for c in clusters if _area(c) >= MIN_CLUSTER_AREA], key=lambda c: (c[1], c[0])
    )


def table_rects(page: fitz.Page):
    rects = []
    try:
        tables = page.find_tables()
        for table in getattr(tables, "tables", []):
            bbox = getattr(table, "bbox", None)
            if bbox:
                rects.append(tuple(bbox))
    except Exception:
        pass
    return rects


def _merge_caption_lines(lines, start_index):
    """把多行图题合并为一段文字。"""
    parts = [lines[start_index]["text"]]
    bbox = list(lines[start_index]["bbox"])
    cursor = start_index
    while cursor + 1 < len(lines):
        nxt = lines[cursor + 1]
        vertical_gap = nxt["bbox"][1] - bbox[3]
        same_start = abs(nxt["bbox"][0] - lines[start_index]["bbox"][0]) < 12
        if vertical_gap > 4 or not same_start:
            break
        if CAPTION_RE.match(nxt["text"]) or re.match(r"^(This journal|Open Access)", nxt["text"]):
            break
        parts.append(nxt["text"])
        bbox = _union(tuple(bbox), nxt["bbox"])
        cursor += 1
    return " ".join(parts), tuple(bbox), cursor


def detect_captions(page: fitz.Page):
    found = []
    lines = line_boxes(page)
    index = 0
    while index < len(lines):
        match = CAPTION_RE.match(lines[index]["text"])
        if match:
            raw_label = re.sub(r"\s+", " ", match.group("label").strip())
            label = re.sub(r"\s*([0-9]+[A-Za-z]?(?:\.[0-9]+)?)$", r" \1", raw_label)
            kind = "table" if raw_label.lower().startswith("table") else "figure"
            text, bbox, end = _merge_caption_lines(lines, index)
            body = text[match.end():].strip()
            found.append(
                {
                    "label": label,
                    "kind": kind,
                    "page": page.number + 1,
                    "caption": body,
                    "raw_caption": text,
                    "caption_bbox": [round(v, 2) for v in bbox],
                }
            )
            index = end + 1
        else:
            index += 1
    return found


# ---------------------------------------------------------------- 区域选择 ---


def pick_region(page: fitz.Page, entry, clusters, tables):
    """为图题选择最可能的图形区域：先找图题下方紧邻，再找上方紧邻。"""
    cap = tuple(entry["caption_bbox"])
    page_rect = page.rect
    candidates = clusters + (tables if entry["kind"] == "table" else [])

    def horizontal_ok(rect):
        overlap = min(rect[2], cap[2]) - max(rect[0], cap[0])
        spread = max(rect[2] - rect[0], cap[2] - cap[0])
        return overlap > 0 or (min(rect[2], cap[2]) - max(rect[0], cap[0])) > -spread * 0.5

    best = None
    for rect in candidates:
        if rect[3] <= cap[1] + MAX_GAP_BELOW:  # 位于图题上方
            gap = cap[1] - rect[3]
            if gap < -2 or gap > MAX_GAP_BELOW:
                continue
            if not horizontal_ok(rect):
                continue
            score = _area(rect) - gap * 400
        elif rect[1] >= cap[3] - MAX_GAP_ABOVE:  # 位于图题下方（图题在上的排版）
            gap = rect[1] - cap[3]
            if gap < -2 or gap > MAX_GAP_ABOVE:
                continue
            if not horizontal_ok(rect):
                continue
            score = _area(rect) - gap * 400 - 5000
        else:
            continue
        if best is None or score > best[0]:
            best = (score, rect)

    if best is None:
        return None, "no_figure_region_near_caption"

    rect = [
        max(0.0, best[1][0] - 5),
        max(0.0, best[1][1] - 5),
        min(page_rect.x1, best[1][2] + 5),
        min(page_rect.y1, best[1][3] + 5),
    ]
    if (rect[2] - rect[0]) < MIN_REGION_W or (rect[3] - rect[1]) < MIN_REGION_H:
        return None, "candidate_region_thin_band"
    return rect, ""


def ink_ratio(pix, step=40):
    """粗采样统计非白像素比例，判断裁剪是否空白或近似空白。"""
    samples = pix.samples
    n = pix.n
    total = 0
    nonwhite = 0
    for offset in range(0, len(samples), step * n):
        chunk = samples[offset: offset + n]
        if len(chunk) < n:
            break
        total += 1
        r, g, b = (chunk[0], chunk[1], chunk[2]) if n >= 3 else (chunk[0],) * 3
        if not (r > 245 and g > 245 and b > 245):
            nonwhite += 1
    return round(nonwhite / max(total, 1), 4)


def verify_crop(doc: fitz.Document, page_index: int, rect, label, pix, caption_text_pdf=""):
    """程序化身份核验，不依赖目视。

    两条证据任选其一即可确认身份：
      1. 裁剪区域内能回抽出图号/表号（图题被裁进图内）；
      2. 同页从 PDF 精确抽取的图题文本含该图号（图题在图外）。
    另加非空白检查（ink_ratio）和尺寸检查。
    """
    page = doc[page_index]
    region_text = page.get_text("text", clip=fitz.Rect(*rect)) or ""
    normalized = re.sub(r"\s+", " ", region_text).strip()
    caption_norm = re.sub(r"\s+", " ", caption_text_pdf or "").strip()
    core = re.sub(r"\s+", " ", label).strip()
    number_match = re.search(r"([0-9]+[A-Za-z]?(?:\.[0-9]+)?)$", core)
    prefix = core.split(" ")[0].rstrip(".")

    def label_in(text):
        if not text:
            return False
        if core in text:
            return True
        return bool(number_match and number_match.group(1) in text and prefix in text)

    checks = {
        "region_text_chars": len(normalized),
        "label_in_crop": label_in(normalized),
        "label_in_pdf_caption": label_in(caption_norm),
        "ink_ratio": ink_ratio(pix),
        "region_text_excerpt": normalized[:300],
    }
    checks["identity_ok"] = bool(checks["label_in_crop"] or checks["label_in_pdf_caption"])
    checks["not_blank"] = checks["ink_ratio"] >= 0.01
    checks["verified"] = bool(checks["identity_ok"] and checks["not_blank"])
    return checks


def render_region(
    doc: fitz.Document, page_index: int, rect, out_path: Path, dpi: int, label="", caption_text_pdf=""
):
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi, clip=fitz.Rect(*rect))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    data = out_path.read_bytes()
    asset = {
        "path": str(out_path.resolve()),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width_px": pix.width,
        "height_px": pix.height,
        "render_dpi": dpi,
    }
    if label:
        asset["verification"] = verify_crop(doc, page_index, rect, label, pix, caption_text_pdf)
    return asset


def render_page(doc: fitz.Document, page_index: int, out_path: Path, dpi: int):
    """兜底：整页渲染。标记为 page_fallback，便于子任务在图注中说明。"""
    pix = doc[page_index].get_pixmap(dpi=dpi)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(out_path))
    data = out_path.read_bytes()
    return {
        "path": str(out_path.resolve()),
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width_px": pix.width,
        "height_px": pix.height,
        "render_dpi": dpi,
        "mode": "page_fallback",
    }


def caption_lines(page: fitz.Page):
    """页面内所有出现图/表标签候选的文本行（不要求行首），按阅读顺序。"""
    hits = []
    for line in line_boxes(page):
        text = line["text"]
        for match in CAPTION_RE.finditer(text):
            raw = re.sub(r"\s+", " ", match.group("label").strip())
            label = re.sub(r"\s*([0-9]+[A-Za-z]?(?:\.[0-9]+)?)$", r" \1", raw)
            hits.append(
                {
                    "label": label,
                    "kind": "table" if raw.lower().startswith("table") else "figure",
                    "bbox": tuple(line["bbox"]),
                    "at_line_start": match.start() == len(text) - len(text.lstrip()),
                    "lead": text[: match.start()].strip(),
                    "tail": text[match.end():].strip(),
                }
            )
    return hits


def figure_zones(page: fitz.Page):
    """以图形块的连通区域为主体，合并同列相邻块为一个图区。"""
    clusters = graphic_clusters(page)
    tables = table_rects(page)
    zones: list[dict] = []
    for rect in clusters + tables:
        for zone in zones:
            if _intersects(rect, zone["rect"], pad=14.0):
                zone["rect"] = _union(zone["rect"], rect)
                zone["parts"] += 1
                break
        else:
            zones.append({"rect": tuple(rect), "parts": 1, "from_table": False})
    page_area = page.rect.width * page.rect.height
    filtered = []
    for zone in zones:
        rect = zone["rect"]
        if _area(rect) / max(page_area, 1.0) > 0.97:  # 整页背景
            continue
        if (rect[2] - rect[0]) < MIN_REGION_W or (rect[3] - rect[1]) < MIN_REGION_H:
            continue
        zone["rect"] = list(rect)
        filtered.append(zone)
    filtered.sort(key=lambda z: (round(z["rect"][1], 1), round(z["rect"][0], 1)))
    return filtered


def match_zone_to_caption(page: fitz.Page, zone, hits):
    """给图区找最近的图题：优先下方同列，其次上方同列。"""
    rect = tuple(zone["rect"])
    best = None
    for hit in hits:
        cap = hit["bbox"]
        vertical_gap = None
        if cap[1] >= rect[3] - 4:
            vertical_gap = cap[1] - rect[3]
        elif cap[3] <= rect[1] + 4:
            vertical_gap = (rect[1] - cap[3]) + 25  # 轻微惩罚图题在图上方的情况
        if vertical_gap is None or vertical_gap > MAX_GAP_BELOW:
            continue
        horizontal_overlap = min(rect[2], cap[2]) - max(rect[0], cap[0])
        if horizontal_overlap < -40:
            continue
        score = -vertical_gap + horizontal_overlap * 0.05 + (10 if hit["at_line_start"] else 0)
        if best is None or score > best[0]:
            best = (score, hit)
    return best[1] if best else None


def caption_block_bbox(page: fitz.Page, start_bbox, column_span, max_gap=8.0, max_lines=12):
    """从图题标签所在行合并出完整图题段落。

    先合并同一视觉行上的其余文字（很多排版把"Fig. 1"与图题正文拆成
    两个 PyMuPDF 行），再向下合并连续行，直到遇到空行、新图题或栏外文字。
    """
    bbox = list(start_bbox)
    lines = line_boxes(page)

    # 同一视觉行
    for line in lines:
        lb = line["bbox"]
        if abs(lb[1] - bbox[1]) <= 4.0 and lb[0] >= bbox[2] - 2:
            bbox[2] = max(bbox[2], lb[2])
            bbox[3] = max(bbox[3], lb[3])

    # 向下合并连续行
    count = 0
    for line in lines:
        if count >= max_lines:
            break
        lb = line["bbox"]
        if lb[1] <= bbox[3] - 1:
            continue
        if lb[1] - bbox[3] > max_gap:
            break
        if CAPTION_RE.match(line["text"]):
            break
        # 与已合并图题横向范围有实质重叠才算同一段
        overlap = min(lb[2], bbox[2]) - max(lb[0], bbox[0])
        if overlap < 0.35 * max(lb[2] - lb[0], 20):
            break
        bbox[2] = max(bbox[2], lb[2])
        bbox[3] = max(bbox[3], lb[3])
        count += 1
    return tuple(bbox)


def caption_text_near(page: fitz.Page, zone, max_lines=6):
    """取图区下方紧邻的正文行，作为原始图题文本（可能跨行）。"""
    rect = tuple(zone["rect"])
    texts = []
    for line in line_boxes(page):
        bbox = line["bbox"]
        if bbox[1] < rect[3] - 4 or bbox[1] > rect[3] + 90:
            continue
        if min(rect[2], bbox[2]) - max(rect[0], bbox[0]) < -40:
            continue
        texts.append((bbox[1], line["text"]))
        if len(texts) >= max_lines:
            break
    return " ".join(text for _, text in texts)[:600]


# ---------------------------------------------------------------- 主流程 ---


def run_captions(args):
    """列出全文所有行首图/表题：页码、图号、完整图题文本、bbox。"""
    doc = fitz.open(args.pdf)
    rows = []
    for number in _pages_to_scan(doc, args.pages):
        if not 1 <= number <= doc.page_count:
            continue
        page = doc[number - 1]
        for hit in caption_lines(page):
            if not hit["at_line_start"] and not args.include_inline:
                continue
            block = caption_block_bbox(page, hit["bbox"], hit["bbox"])
            text = re.sub(r"\s+", " ", page.get_text("text", clip=fitz.Rect(*block)) or "").strip()
            rows.append(
                {
                    "page": number,
                    "label": hit["label"],
                    "kind": hit["kind"],
                    "bbox": [round(v, 2) for v in block],
                    "caption": text[:800],
                    "at_line_start": hit["at_line_start"],
                }
            )
    doc.close()
    json_out = Path(args.json_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[captions] 共 {len(rows)} 条 → {json_out}")
    return 0


def _pages_to_scan(doc, pages_arg):
    if pages_arg.strip():
        return [int(p.strip()) for p in pages_arg.split(",") if p.strip()]
    return list(range(1, doc.page_count + 1))


def _safe_name(label):
    return re.sub(r"[^0-9A-Za-z]+", "_", label).strip("_") or "item"


def run_scan(args):
    doc = fitz.open(args.pdf)
    inventory = {"pdf_path": str(args.pdf), "page_count": doc.page_count, "items": [], "page_zones": []}
    for number in _pages_to_scan(doc, args.pages):
        if not 1 <= number <= doc.page_count:
            continue
        page = doc[number - 1]
        hits = caption_lines(page)
        zones = figure_zones(page)
        for zone in zones:
            hit = match_zone_to_caption(page, zone, hits)
            inventory["items"].append(
                {
                    "label": hit["label"] if hit else f"未标注图区 p{number}",
                    "kind": hit["kind"] if hit else "figure",
                    "page": number,
                    "status": "located",
                    "bbox_pt": [round(v, 2) for v in zone["rect"]],
                    "caption": (hit["tail"] if hit and hit["tail"] else caption_text_near(page, zone)),
                    "line_start_caption": bool(hit and hit["at_line_start"]),
                }
            )
        inventory["page_zones"].append(
            {"page": number, "zones": [[round(v, 1) for v in z["rect"]] for z in zones]}
        )
    doc.close()
    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(
        json.dumps(inventory, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    located = sum(1 for i in inventory["items"] if i["status"] == "located")
    print(
        f"[scan] 候选条目 {len(inventory['items'])}，可定位 {located}，"
        f"无法定位 {len(inventory['items']) - located} → {args.json_out}"
    )
    return 0


def run_crop(args):
    pdf_path = Path(args.pdf)
    if not pdf_path.is_file():
        print(f"[error] PDF 不存在：{pdf_path}", file=sys.stderr)
        return 2
    doc = fitz.open(str(pdf_path))
    include_re = re.compile(args.include) if args.include else None
    out_dir = Path(args.out_dir)
    inventory = {
        "pdf_path": str(pdf_path),
        "page_count": doc.page_count,
        "render_dpi": args.dpi,
        "items": [],
    }

    for number in _pages_to_scan(doc, args.pages):
        if not 1 <= number <= doc.page_count:
            continue
        page = doc[number - 1]
        hits = caption_lines(page)
        zones = figure_zones(page)
        # 先记录页面上出现的所有图/表标签，供完整性清单核对（即使无对应图区）
        for hit in hits:
            if not hit["at_line_start"]:
                continue
            inventory.setdefault("page_caption_labels", []).append(
                {"page": number, "label": hit["label"], "kind": hit["kind"], "tail": hit["tail"][:300]}
            )
        for index, zone in enumerate(zones):
            hit = match_zone_to_caption(page, zone, hits)
            label = hit["label"] if hit else f"未标注图区 p{number}-{index+1}"
            item = {
                "label": label,
                "kind": hit["kind"] if hit else "figure",
                "page": number,
                "caption": (hit["tail"] if hit and hit["tail"] else caption_text_near(page, zone)),
                "raw_caption": "",
                "caption_bbox": [round(v, 2) for v in hit["bbox"]] if hit else None,
                "status": "detected",
                "missing_reason": "",
                "asset": None,
            }
            if include_re and not include_re.search(label):
                item["status"] = "skipped_by_filter"
                inventory["items"].append(item)
                continue
            page_rect = page.rect
            rect = [
                max(0.0, zone["rect"][0] - 5),
                max(0.0, zone["rect"][1] - 5),
                min(page_rect.x1, zone["rect"][2] + 5),
                min(page_rect.y1, zone["rect"][3] + 5),
            ]
            # 图题文本一律从 PDF 精确抽取（不转录、不改写）。
            caption_text_pdf = ""
            if hit is not None:
                full_cap = caption_block_bbox(page, hit["bbox"], hit["bbox"])
                caption_text_pdf = re.sub(
                    r"\s+", " ", page.get_text("text", clip=fitz.Rect(*full_cap)) or ""
                ).strip()
                item["caption"] = caption_text_pdf[:800]
                item["caption_bbox_full"] = [round(v, 2) for v in full_cap]

            # 只有图题与图区同栏（横向跨度接近）时，才把图题裁进图中，
            # 避免跨栏图题把整页正文一起框进来。
            if hit is not None and hit["at_line_start"] and args.include_caption:
                cap = hit["bbox"]
                if cap[1] - rect[3] <= MAX_GAP_BELOW or rect[1] - cap[3] <= MAX_GAP_ABOVE:
                    full_cap = caption_block_bbox(page, cap, cap)
                    zone_w = rect[2] - rect[0]
                    cap_w = full_cap[2] - full_cap[0]
                    if cap_w <= max(zone_w * 1.15, zone_w + 30):
                        rect = list(_union(tuple(rect), full_cap))
                        rect = [
                            max(0.0, rect[0] - 5),
                            max(0.0, rect[1] - 5),
                            min(page_rect.x1, rect[2] + 5),
                            min(page_rect.y1, rect[3] + 5),
                        ]
                        item["crop_includes_caption"] = True
                    else:
                        item["crop_includes_caption"] = False
            item["bbox_pt"] = [round(v, 2) for v in rect]
            filename = _safe_name(label)
            asset = render_region(
                doc,
                number - 1,
                rect,
                out_dir / f"p{number:03d}_{filename}.png",
                args.dpi,
                label,
                caption_text_pdf,
            )
            item.update(status="rendered", asset=asset)
            inventory["items"].append(item)

    doc.close()
    json_out = Path(args.json_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(inventory, ensure_ascii=False, indent=1), encoding="utf-8")
    rendered = sum(1 for i in inventory["items"] if i["status"].startswith("rendered"))
    unlocatable = sum(1 for i in inventory["items"] if i["status"] == "unlocatable")
    print(
        f"[crop] 条目 {len(inventory['items'])}，已渲染 {rendered}，无法定位 {unlocatable} → {json_out}"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="论文 PDF 图表定位与裁剪")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="列出候选图/表与页面图块")
    scan.add_argument("--pdf", required=True)
    scan.add_argument("--json-out", required=True)
    scan.add_argument("--pages", default="")
    scan.set_defaults(func=run_scan)

    caps = sub.add_parser("captions", help="列出全文图/表题清单")
    caps.add_argument("--pdf", required=True)
    caps.add_argument("--json-out", required=True)
    caps.add_argument("--pages", default="")
    caps.add_argument("--include-inline", action="store_true", help="一并列出正文内的图表引用")
    caps.set_defaults(func=run_captions)

    crop = sub.add_parser("crop", help="渲染指定图号")
    crop.add_argument("--pdf", required=True)
    crop.add_argument("--out-dir", required=True)
    crop.add_argument("--json-out", required=True)
    crop.add_argument("--pages", default="")
    crop.add_argument("--dpi", type=int, default=200)
    crop.add_argument("--include", default="", help="正则，匹配图号，例如 'Fig\\. 1$|Table\\. 1$'")
    crop.add_argument("--page-fallback", action="store_true", help="定位失败时整页渲染兜底")
    crop.add_argument("--include-caption", action="store_true", help="把紧邻的原始图题一起裁进图中")
    crop.set_defaults(func=run_crop)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
