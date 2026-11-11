"""Regression tests for the verified-crop generator.

Run with the project venv (PyMuPDF lives there):
    .workbuddy/venv/Scripts/python.exe -m pytest tests/test_figure_crops.py -q
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "artifacts" / "figure_repair"))

import batch_verified_crops as crops  # noqa: E402

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_near_full_page_background_bitmap_does_not_hide_real_figures():
    """A publisher background bitmap must not swallow the real figure.

    Before the fix the oversized bitmap was unioned with every drawing into one
    near-page-sized cluster, which was then filtered out as a whole-page zone;
    the job produced zero crops.
    """

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=TINY_PNG)
    page.draw_rect(fitz.Rect(60, 100, 300, 400), color=(0, 0, 0), width=1)
    try:
        zones = crops.candidate_zones(page)
    finally:
        doc.close()

    assert zones, "真实图区必须仍然被识别"
    assert any(
        zone["rect"][0] <= 61 and zone["rect"][1] <= 101 and zone["rect"][2] >= 299
        for zone in zones
    ), "识别出的应是真实图区，而不是整页背景"
    page_area = 595 * 842
    assert all(
        crops._area(zone["rect"]) < 0.9 * page_area for zone in zones
    ), "不能把近整页区域当成单图裁图"


def test_caption_zone_pairing_is_one_to_one():
    """A nearby caption must not steal the zone belonging to the next caption."""

    captions = [
        {"label": "Fig. 1", "page": 1, "bbox": (60, 210, 300, 226)},
        {"label": "Fig. 2", "page": 1, "bbox": (60, 410, 300, 426)},
    ]
    zones = [
        {"rect": [60, 100, 300, 200]},
        {"rect": [60, 300, 300, 400]},
    ]
    pairs = crops._pair_captions_to_zones(1, captions, zones)

    assert all(pair["zone"] is not None for pair in pairs)
    assert pairs[0]["zone"] == (60, 100, 300, 200)
    assert pairs[1]["zone"] == (60, 300, 300, 400)
    assert len({tuple(pair["zone"]) for pair in pairs}) == 2


def test_unpaired_caption_records_a_concrete_missing_reason():
    captions = [{"label": "Fig. 6", "page": 13, "bbox": (60, 700, 300, 716)}]
    pairs = crops._pair_captions_to_zones(13, captions, [])

    assert pairs[0]["zone"] is None
    assert pairs[0]["reason"] == "no_figure_region_near_caption"
    assert pairs[0]["label"] == "Fig. 6"
