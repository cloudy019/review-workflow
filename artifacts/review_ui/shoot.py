"""Screenshot the review page in the states that matter for reading comfort.

Edge's launcher returns before the page is painted, so every shot polls for the
file instead of assuming the subprocess call means "done".
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts" / "review_ui"
PREVIEW = OUT / "preview"
SHOTS = OUT / "shots"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")

SCROLL_RESULT = """
const body = document.querySelector('#reader-body');
body.style.scrollBehavior = 'auto';
const sec = document.querySelector('[data-summary-topic="result"]');
if (sec) {
  const delta = sec.getBoundingClientRect().top - body.getBoundingClientRect().top;
  body.scrollTop = body.scrollTop + delta - 16;
}
"""

VARIANTS: dict[str, str] = {
    "top": "",
    "result": SCROLL_RESULT,
    "figonly": SCROLL_RESULT + """
document.querySelector('[data-summary-filter="figs"]').click();
""",
    "tail": """
const body = document.querySelector('#reader-body');
body.style.scrollBehavior = 'auto';
body.scrollTop = body.scrollHeight;
""",
    "lightbox": """
const body = document.querySelector('#reader-body');
body.style.scrollBehavior = 'auto';
const fig = document.querySelector('.note .fig');
if (fig) {
  const delta = fig.getBoundingClientRect().top - body.getBoundingClientRect().top;
  body.scrollTop = body.scrollTop + delta - 80;
  fig.querySelector('img').click();
}
""",
    "dark": """
document.querySelector('#btn-theme').click();
""" + SCROLL_RESULT,
}


def build(source: Path, name: str, script: str) -> Path:
    html = source.read_text(encoding="utf-8")
    injected = f"<script>setTimeout(()=>{{{script}}},160);</script>\n</body>"
    target = PREVIEW / f"{source.stem}.{name}.html"
    target.write_text(html.replace("</body>", injected, 1), encoding="utf-8")
    return target


def _wait_for(path: Path, timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.stat().st_size > 2048:
            return True
        time.sleep(0.4)
    return False


def shoot(page: Path, size: str, name: str) -> tuple[Path, bool]:
    png = SHOTS / f"{name}.png"
    if png.exists():
        png.unlink()
    SHOTS.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(EDGE),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={size}",
            "--virtual-time-budget=6000",
            f"--screenshot={png}",
            page.as_uri(),
        ],
        capture_output=True,
        timeout=120,
    )
    return png, _wait_for(png)


def main() -> int:
    jobs = ["paper-45d514c98a379d42", "paper-7844aca4d0f7364f", "paper-af794951649f8519"]
    for job in jobs:
        source = PREVIEW / f"{job}.html"
        if not source.is_file():
            print(f"skip {job}: 先跑 build_preview.py")
            continue
        tag = job.split("-")[1][:4]
        for name, script in VARIANTS.items():
            page = build(source, name, script)
            png, ok = shoot(page, "1600,1000", f"{tag}-{name}")
            print(f"{'ok ' if ok else 'FAIL'} {png.name} ({png.stat().st_size // 1024 if ok else 0} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
