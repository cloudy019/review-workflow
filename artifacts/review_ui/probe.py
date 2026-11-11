"""Measure the real layout in headless Edge and report overflow / clipping."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "review_ui"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

PROBE = """
<script>
setTimeout(() => {
  const pick = ['.app','.appbar','.headline','.cols','.rail','.steps','.reader','.reader-h',
                '.tabs','.docnav','.reader-body','.pane','.cardcol','.cards','.card',
                '.cardfoot','#status','#export','#prev','.note','.summary-section'];
  const out = {win:[innerWidth,innerHeight],
               doc:[document.documentElement.scrollWidth, document.documentElement.scrollHeight],
               items:{}};
  for (const sel of pick) {
    const el = document.querySelector(sel);
    if (!el) { out.items[sel] = null; continue; }
    const r = el.getBoundingClientRect();
    out.items[sel] = [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height),
                      el.scrollWidth, el.clientWidth, el.scrollHeight, el.clientHeight,
                      getComputedStyle(el).overflowY, getComputedStyle(el).flexWrap];
  }
  out.counts = {page:document.querySelectorAll('.page').length,
                fig:document.querySelectorAll('.fig').length,
                claim:document.querySelectorAll('.claim').length,
                card:document.querySelectorAll('.card').length,
                navChips:[...document.querySelectorAll('.docnav button')].filter(b=>!b.hidden).length};
  out.overflows = [...document.querySelectorAll('body *')]
    .filter(el => { const r = el.getBoundingClientRect();
                    return r.width > 0 && (r.right > innerWidth + 2 || r.left < -2); })
    .slice(0, 12)
    .map(el => el.tagName.toLowerCase() + '.' + (el.className || '').toString().slice(0, 40));
  const d = document.createElement('pre');
  d.id = 'probe-out';
  d.textContent = JSON.stringify(out);
  document.body.appendChild(d);
}, 400);
</script>
"""


def probe(page: Path, size: str) -> dict:
    html = page.read_text(encoding="utf-8")
    tmp = page.with_suffix(".probe.html")
    tmp.write_text(html.replace("</body>", PROBE + "</body>", 1), encoding="utf-8")
    result = subprocess.run(
        [str(EDGE), "--headless=new", "--disable-gpu", f"--window-size={size}",
         "--virtual-time-budget=9000", "--dump-dom", tmp.as_uri()],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    match = re.search(r'<pre id="probe-out">(.*?)</pre>', result.stdout, re.S)
    if not match:
        return {"error": "probe not found", "stderr": result.stderr[-400:]}
    return json.loads(match.group(1).replace("&quot;", '"').replace("&amp;", "&"))


def main() -> int:
    targets = [
        ("paper-7844aca4d0f7364f", "1600,1000"),
        ("paper-7844aca4d0f7364f", "820,1180"),
        ("paper-7844aca4d0f7364f", "1280,900"),
    ]
    for name, size in targets:
        data = probe(OUT / "preview" / f"{name}.html", size)
        print(f"\n=== {name} @ {size}")
        if "error" in data:
            print(data)
            continue
        print(" win/doc:", data["win"], data["doc"])
        print(" counts:", data["counts"])
        print(" overflow:", data["overflows"] or "none")
        for sel in (".cols", ".rail", ".reader-h", ".docnav", ".reader-body", ".cards", ".cardfoot", "#status", "#export"):
            print(f"  {sel:14}", data["items"].get(sel))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
