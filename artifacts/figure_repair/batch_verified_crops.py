#!/usr/bin/env python
"""为一批评审任务生成「可身份核验」的单图裁图资产。

背景：某批次的 9 篇任务只记录了 ``figure_decisions``
（意图清单），从未生成 ``reader/assets/*.png``，导致评审页是纯文字页。
本脚本从**原 PDF 的真实区域**渲染裁图，并把每条图题与图区一一配对。

硬约束（见 docs/CONTRACTS.md §5）：
- 不臆造、不重绘、不美化；不做整页兜底（整页图不是单图裁图）。
- 身份核验走 ``verify_crop()``：裁剪区回抽文本含图号 **或** 同页 PDF 图题
  文本含图号，且 ``ink_ratio >= 0.01``。未通过的一律不写入正式材料，
  只写具体 ``missing_reason`` 到清单。
- 只写 ``agent-output/reader/`` 下的新文件；不碰任何哈希锚定的既有产物
  （deep-paper-note.md / reading-result.json / review-sections.json /
  runs/*/request.json / source-pages/*）。

用法：
    python artifacts/figure_repair/batch_verified_crops.py <job_id> [...] [--dpi 150]
    python artifacts/figure_repair/batch_verified_crops.py --all-missing
    python ... --dry-run        # 只报告，不落盘
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import fitz  # PyMuPDF

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from extract_figures import (  # noqa: E402
    MIN_REGION_H,
    MIN_REGION_W,
    _area,
    _safe_name,
    _intersects,
    _union,
    caption_block_bbox,
    caption_lines,
    line_boxes,
    render_region,
    table_rects,
)

JOBS = ROOT / "文献知识库" / "review-jobs" / "jobs"
MAX_REGION_PAGE_RATIO = 0.85
MAX_GAP_CAPTION_BELOW = 180.0  # pt：图区底边到下方图题
MAX_GAP_CAPTION_ABOVE = 110.0  # pt：图题上方排版时，图题底边到图区


def candidate_zones(page: fitz.Page) -> list[dict]:
    """Figure zones that ignore a full-page background bitmap.

    Some publishers lay a near-page-sized image behind the text.
    ``graphic_clusters`` unions that bitmap with every drawing on the page, so
    the resulting "one cluster" is filtered out as a whole-page zone and every
    real figure disappears.  Dropping only oversized *individual* rects keeps
    the genuine vector/raster figure clusters intact.
    """

    page_area = page.rect.width * page.rect.height
    rects: list[tuple[float, float, float, float]] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect and (rect.width > 0 or rect.height > 0):
            rects.append((rect.x0, rect.y0, rect.x1, rect.y1))
    try:
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            if bbox:
                rects.append(tuple(bbox))
    except Exception:
        pass
    rects = [
        rect for rect in rects
        if _area(rect) >= 4.0 and _area(rect) < 0.9 * page_area
    ]
    rects.extend(table_rects(page))
    clusters: list[tuple[float, float, float, float]] = []
    for rect in rects:
        for index, cluster in enumerate(clusters):
            if _intersects(rect, cluster, pad=14.0):
                clusters[index] = _union(cluster, rect)
                break
        else:
            clusters.append(rect)
    zones = []
    for rect in clusters:
        if _area(rect) / max(page_area, 1.0) > 0.97:
            continue
        if (rect[2] - rect[0]) < MIN_REGION_W or (rect[3] - rect[1]) < MIN_REGION_H:
            continue
        zones.append({"rect": list(rect)})
    zones.sort(key=lambda item: (round(item["rect"][1], 1), round(item["rect"][0], 1)))
    return zones


def _label_in_text(label: str, text: str) -> bool:
    """Same identity rule as ``verify_crop``: exact label, or its number + prefix."""

    import re

    if not text:
        return False
    core = " ".join(label.split())
    if core in text:
        return True
    number = re.search(r"([0-9]+[A-Za-z]?(?:\.[0-9]+)?)$", core)
    prefix = core.split(" ")[0].rstrip(".")
    return bool(number and number.group(1) in text and prefix in text)


def _horizontal_ok(zone, cap) -> bool:
    overlap = min(zone[2], cap[2]) - max(zone[0], cap[0])
    spread = max(zone[2] - zone[0], cap[2] - cap[0])
    return overlap > 0 or overlap > -spread * 0.5


def _pair_captions_to_zones(page_number: int, captions: list[dict], zones: list[dict]) -> list[dict]:
    """Globally greedy one-to-one pairing, scored by area minus vertical gap.

    A per-caption first-come loop lets a nearby caption steal the zone that a
    later caption sits directly under (e.g. a table below a big figure).  Ranking
    every caption/zone candidate first gives the strongest geometric claim
    priority, which is the one a reader would actually believe.
    """

    candidates: list[tuple[float, int, int, tuple[float, float, float, float]]] = []
    for ci, caption in enumerate(captions):
        cap = caption["bbox"]
        for zi, zone in enumerate(zones):
            rect = tuple(zone["rect"])
            if not _horizontal_ok(rect, cap):
                continue
            crop = rect
            if rect[3] <= cap[1] + 4:  # 图在上、图题在下（常见）
                gap = cap[1] - rect[3]
                penalty = 0
            elif rect[1] >= cap[3] - 4:  # 图题在上、图在下
                gap = rect[1] - cap[3]
                penalty = 6000
            elif rect[1] <= cap[1] <= rect[3] and cap[1] - rect[1] <= 40:
                # 表格常把「Table N …」印在图区顶部，图题被图区包含。
                if rect[3] - cap[3] < MIN_REGION_H:
                    continue
                gap = cap[1] - rect[1]
                penalty = 6000
            elif rect[1] < cap[1] < rect[3] and cap[1] - rect[1] >= MIN_REGION_H:
                # 连通区把图题及其下方内容一起并了进来（多面板图常见）。
                crop = (rect[0], rect[1], rect[2], cap[1] - 2)
                gap = 0
                penalty = 0
            else:
                continue
            if gap < -2 or gap > (MAX_GAP_CAPTION_BELOW if penalty == 0 else MAX_GAP_CAPTION_ABOVE):
                continue
            candidates.append((_area(crop) - gap * 300 - penalty, ci, zi, crop))
    candidates.sort(key=lambda item: -item[0])
    used_captions: set[int] = set()
    used_zones: set[int] = set()
    pairs: list[dict | None] = [None] * len(captions)
    for _score, ci, zi, crop in candidates:
        if ci in used_captions or zi in used_zones:
            continue
        used_captions.add(ci)
        used_zones.add(zi)
        pairs[ci] = {**captions[ci], "zone": crop, "reason": ""}
    return [
        pair or {**caption, "zone": None, "reason": "no_figure_region_near_caption"}
        for pair, caption in zip(pairs, captions)
    ]


def table_text_region(page: fitz.Page, caption: dict) -> tuple[float, float, float, float] | None:
    """A text-only table has no drawing cluster; take its own contiguous lines.

    RSC/Elsevier text tables are set as ordinary text under a caption.  The crop
    is still the PDF's real region: we stop at the first blank-line-sized gap and
    stay inside the caption's column, so body prose is not swept into the image.
    """

    block = caption.get("caption_block") or caption["bbox"]
    top = block[3]
    chosen: list[tuple[float, float, float, float]] = []
    previous_bottom = top
    # 表格常横跨多个文本列；只按图题的横向范围裁会把最右列切断，所以
    # 取整个连续文本带的左右边界，而不是过滤「与图题重叠」的行。
    for line in line_boxes(page):
        bbox = line["bbox"]
        if bbox[1] < top - 2:
            continue
        if bbox[1] - previous_bottom > 14:
            break
        if bbox[0] < block[0] - 20:
            continue
        chosen.append(bbox)
        previous_bottom = max(previous_bottom, bbox[3])
    if len(chosen) < 2 or previous_bottom - top < MIN_REGION_H:
        return None
    return (
        max(0.0, min(b[0] for b in chosen) - 4),
        max(0.0, top),
        min(page.rect.x1, max(b[2] for b in chosen) + 4),
        min(page.rect.y1, previous_bottom + 4),
    )


def graphic_band_region(page: fitz.Page, caption: dict) -> tuple[float, float, float, float] | None:
    """Union real drawing/image marks just above a caption (scatter-plot case).

    Some figures are hundreds of tiny vector marks.  Each one is too small to be
    a cluster, so no zone survives, but their union is the figure.  Only marks
    above the caption, inside its column, and reaching close to the caption are
    used, so an unrelated graphic higher up the column is not swept in.
    """

    # 用图题所在行的 bbox 界定「这一栏」，合并后的图题块可能跨栏，
    # 跨栏取并集会把相邻栏的正文和另一张图一起框进来。
    cap = caption["bbox"]
    # 左边界只留 5pt（再宽会把相邻栏的正文带进来）；右边界多留 20pt，
    # 因为流程图的 Output 等元素常比图题行更靠右。
    left, right = cap[0] - 5, cap[2] + 20
    ceiling = cap[1] - 2
    page_area = page.rect.width * page.rect.height
    rects: list[tuple[float, float, float, float]] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect and (rect.width > 0 or rect.height > 0):
            rects.append((rect.x0, rect.y0, rect.x1, rect.y1))
    try:
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            if bbox:
                rects.append(tuple(bbox))
    except Exception:
        pass
    picked = []
    for rect in rects:
        if _area(rect) < 1.0 or _area(rect) >= 0.9 * page_area:
            continue
        if rect[3] > ceiling or rect[1] < ceiling - 520:
            continue
        if rect[0] < left or rect[2] > right + 10:
            continue
        overlap = min(rect[2], right) - max(rect[0], left)
        if overlap < 0.35 * max(rect[2] - rect[0], 20):
            continue
        picked.append(rect)
    if not picked:
        return None
    # 横向用「图题所在栏」的完整宽度，而不是所有散点的最小/最大 x：
    # 散点常常集中在图中央，按散点边界裁会把两侧的坐标/标签切掉。
    union = (
        max(0.0, left),
        max(0.0, min(r[1] for r in picked) - 4),
        min(page.rect.x1, right),
        min(page.rect.y1, max(r[3] for r in picked) + 4),
    )
    if union[3] < ceiling - 60:
        return None
    if (union[2] - union[0]) < MIN_REGION_W or (union[3] - union[1]) < MIN_REGION_H:
        return None
    return union


def crop_one_pdf(pdf: Path, out_dir: Path, dpi: int, dry_run: bool) -> dict:
    doc = fitz.open(str(pdf))
    rows: list[dict] = []
    written: list[Path] = []
    for page_index in range(doc.page_count):
        page = doc[page_index]
        number = page_index + 1
        captions = []
        for hit in caption_lines(page):
            if not hit["at_line_start"]:
                continue
            label = hit["label"]
            block = caption_block_bbox(page, hit["bbox"], hit["bbox"])
            text = " ".join((page.get_text("text", clip=fitz.Rect(*block)) or "").split())
            # 真图题的正文以图号本身开头（"Fig. 2 | ..." / "Table 1. ..."）；
            # 正文里恰好顶格的「Fig. 2 shows ...」是交叉引用。这里不做
            # first-hit-wins 去重：同页先出现的正文引用常会把真正的图题吞掉，
            # 改在渲染完成后按「优先保留核验通过者」去重。
            if not text.startswith(label):
                continue
            captions.append(
                {
                    "label": label,
                    "kind": hit["kind"],
                    "page": number,
                    "bbox": tuple(hit["bbox"]),
                    "caption_block": tuple(block),
                    "caption_text_pdf": text[:800],
                    "caption": text[:300],
                }
            )
        pairs = _pair_captions_to_zones(number, captions, candidate_zones(page))
        for pair in pairs:
            if pair["zone"] is None and pair["kind"] == "table":
                fallback = table_text_region(page, pair)
                if fallback is not None:
                    pair["zone"] = fallback
                    pair["reason"] = ""
                    pair["source"] = "caption_anchored_text_region"
            if pair["zone"] is None and pair["kind"] == "figure":
                fallback = graphic_band_region(page, pair)
                if fallback is not None:
                    pair["zone"] = fallback
                    pair["reason"] = ""
                    pair["source"] = "caption_anchored_graphic_band"
        for pair in pairs:
            row = {
                "label": pair["label"],
                "kind": pair["kind"],
                "page": pair["page"],
                "caption": pair["caption"],
                "source": pair.get("source", "graphic_zone"),
                "status": "missing",
                "missing_reason": "图表缺失：无法从当前 PDF 提取 —— " + (pair["reason"] or ""),
                "asset": None,
                "verification": None,
            }
            zone = pair["zone"]
            if zone is None:
                rows.append(row)
                continue
            rect = [
                max(0.0, zone[0] - 5),
                max(0.0, zone[1] - 5),
                min(page.rect.x1, zone[2] + 5),
                min(page.rect.y1, zone[3] + 5),
            ]
            if (rect[2] - rect[0]) < MIN_REGION_W or (rect[3] - rect[1]) < MIN_REGION_H:
                row["missing_reason"] += "candidate_region_thin_band"
                rows.append(row)
                continue
            if _area(rect) / max(page.rect.width * page.rect.height, 1.0) > MAX_REGION_PAGE_RATIO:
                row["missing_reason"] += "candidate_region_is_almost_whole_page"
                rows.append(row)
                continue
            row["bbox_pt"] = [round(value, 2) for value in rect]
            filename = f"p{number:03d}_{_safe_name(pair['label'])}.png"
            target = out_dir / filename
            if dry_run:
                # Dry-run still renders to memory so the identity gate is real.
                pix = page.get_pixmap(dpi=dpi, clip=fitz.Rect(*rect))
                from extract_figures import verify_crop

                checks = verify_crop(
                    doc, page_index, rect, pair["label"], pix, pair["caption_text_pdf"]
                )
                row["verification"] = checks
                if checks["verified"]:
                    row["status"] = "verified"
                    row["missing_reason"] = ""
                    row["asset"] = {"path": str(target), "dry_run": True}
                else:
                    row["missing_reason"] += (
                        "identity_check_failed" if not checks["identity_ok"] else "blank_crop"
                    )
                rows.append(row)
                continue
            asset = render_region(
                doc, page_index, rect, target, dpi, pair["label"], pair["caption_text_pdf"]
            )
            checks = asset.get("verification") or {}
            row["verification"] = checks
            if checks.get("verified"):
                row["status"] = "verified"
                row["missing_reason"] = ""
                row["asset"] = asset
                written.append(target)
            else:
                target.unlink(missing_ok=True)
                row["missing_reason"] += (
                    "identity_check_failed" if not checks.get("identity_ok") else "blank_crop"
                )
            rows.append(row)
    page_count = doc.page_count
    doc.close()
    # 同一图号可能在正文引用和真图题处各出现一次；只留核验通过的那一版。
    best: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = row["label"]
        if key not in best:
            best[key] = row
            order.append(key)
        elif row["status"] == "verified" and best[key]["status"] != "verified":
            best[key] = row
    rows = [best[key] for key in order]
    if not dry_run:
        kept = {
            Path(row["asset"]["path"]).resolve()
            for row in rows if row["asset"] and row["status"] == "verified"
        }
        for path in written:
            if path.resolve() not in kept:
                path.unlink(missing_ok=True)
    return {
        "pdf_path": str(pdf),
        "page_count": page_count,
        "render_dpi": dpi,
        "captions": len(rows),
        "verified": sum(1 for row in rows if row["status"] == "verified"),
        "missing": sum(1 for row in rows if row["status"] == "missing"),
        "rows": rows,
    }


def process_job(job_id: str, dpi: int, dry_run: bool, force: bool) -> dict:
    job = json.loads((JOBS / job_id / "job.json").read_text(encoding="utf-8"))
    pdf = Path(job["pdf_path"])
    output = Path(job["agent_output_dir"])
    assets = output / "reader" / "assets"
    existing = sorted(assets.glob("*.png")) if assets.is_dir() else []
    if existing and not force:
        return {"job_id": job_id, "skipped": True, "reason": f"已有 {len(existing)} 张裁图，未覆盖", "existing": len(existing)}
    if not pdf.is_file():
        return {"job_id": job_id, "skipped": True, "reason": f"PDF 不存在：{pdf}"}
    result = json.loads((output / "reading-result.json").read_text(encoding="utf-8"))
    decisions = [d for d in (result.get("figure_decisions") or []) if isinstance(d, dict)]
    expected = sum(1 for d in decisions if str(d.get("decision") or "include") != "omit")
    report = crop_one_pdf(pdf, assets, dpi, dry_run)
    report["expected_from_decisions"] = expected
    report["job_id"] = job_id
    report["dry_run"] = dry_run
    if not dry_run:
        assets.parent.mkdir(parents=True, exist_ok=True)
        (assets.parent / "figure-crops.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_ids", nargs="*")
    parser.add_argument("--all-missing", action="store_true")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    job_ids = list(args.job_ids)
    if args.all_missing:
        for path in sorted(JOBS.glob("*/job.json")):
            job = json.loads(path.read_text(encoding="utf-8"))
            output = Path(job["agent_output_dir"])
            if (output / "reading-result.json").is_file() and not (output / "reader" / "assets").is_dir():
                job_ids.append(job["job_id"])
    if not job_ids:
        print("没有需要处理的任务")
        return 0
    total_v = total_m = 0
    for job_id in dict.fromkeys(job_ids):
        report = process_job(job_id, args.dpi, args.dry_run, args.force)
        if report.get("skipped"):
            print(f"{job_id}  跳过：{report['reason']}")
            continue
        total_v += report["verified"]
        total_m += report["missing"]
        print(
            f"{job_id}  图题 {report['captions']:>3}  核验通过 {report['verified']:>3}  "
            f"缺失 {report['missing']:>3}  (决策预期 {report['expected_from_decisions']})"
            + ("  [dry-run]" if args.dry_run else "")
        )
        for row in report["rows"]:
            if row["status"] == "missing":
                print(f"    缺失 {row['label']:<14} p.{row['page']:<3} {row['missing_reason']}")
    print(f"合计：核验通过 {total_v}，缺失 {total_m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
