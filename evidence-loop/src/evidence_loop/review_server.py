"""Loopback-only review UI server with constrained JSON persistence."""

from __future__ import annotations

import html
import json
import re
import secrets
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, urlparse

from .core import ReviewOrchestrator, ReviewWorkflowError, ValidationError, sha256_file
from .html_review import _render_claims
from .locking import serialized
from .registry import build_review_registry, write_review_registry


_JOB_ID_RE = re.compile(r"paper-[0-9a-f]{16}")
_MAX_REVIEW_BYTES = 1024 * 1024


class ReviewServerError(ValueError):
    """Raised when a browser submission is unsafe or stale."""


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class ReviewApplication:
    """Read review pages and save validated records inside their own job."""

    def __init__(self, kb: str | Path, token: str | None = None) -> None:
        self.kb = Path(kb).expanduser().resolve()
        self.orchestrator = ReviewOrchestrator(self.kb / "review-jobs")
        self.root = self.orchestrator.root
        self.token = token or secrets.token_urlsafe(32)
        write_review_registry(self.kb)

    def _job(self, job_id: str) -> dict[str, Any]:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ReviewServerError("论文任务编号无效")
        try:
            return self.orchestrator.get_job(job_id)
        except (KeyError, FileNotFoundError, ValidationError) as exc:
            raise ReviewServerError("找不到这篇论文的评审任务") from exc

    def review_page(self, job_id: str) -> bytes:
        job = self._job(job_id)
        page = Path(job["agent_output_dir"]) / "paper-review.html"
        if not page.is_file():
            raise ReviewServerError("这篇论文还没有生成人工评审页面")
        content = page.read_text(encoding="utf-8")
        if job.get("ai_citation_permission") and (job["state"] == "ai_verified"
                or job.get("citation_permission", {}).get("content_origin") == "ai_verification"):
            try:
                review = self.orchestrator.ai_review_content(job)
            except ReviewWorkflowError as exc:
                raise ReviewServerError(str(exc)) from exc
            claims = [{**claim, "kind": "ai_verified"} for claim in review["verified_claims"]]
            match = re.search(r'(<script type="application/json" id="paper-data">)(.*?)(</script>)', content, re.S)
            if match:
                record = json.loads(match.group(2))
                record["claims"] = claims
                record["ai_review_sha256"] = job["ai_citation_permission"]["review_sha256"]
                encoded = json.dumps(record, ensure_ascii=False).replace("<", "\\u003c")
                content = content[:match.start(2)] + encoded + content[match.end(2):]
            claim_html = '<p>以下为最新 AI 核验后的具体主张；人工选择将裁决这些文本及其限制。</p>' + _render_claims({"claims": claims})
            content, replaced = re.subn(r'(<section\b[^>]*\bid="pane-claims"[^>]*>).*?(</section>)',
                                        lambda match: match.group(1) + claim_html + match.group(2), content, count=1, flags=re.S)
            if not replaced:
                content = content.replace("</body>", claim_html + "</body>")
            # Existing generated pages keep their original file bytes. Upgrade
            # their served form and draft fencing as well as their visible claims.
            if "ai_review_sha256: paper.ai_review_sha256" not in content:
                content = content.replace("ai_note_sha256: paper.ai_note_sha256,",
                    "ai_note_sha256: paper.ai_note_sha256,\n...(paper.ai_review_sha256 ? {ai_review_sha256: paper.ai_review_sha256} : {}),", 1)
            content = content.replace('paper.ai_note_sha256].join(":")',
                                      'paper.ai_note_sha256, ...(paper.ai_review_sha256 ? [paper.ai_review_sha256] : [])].join(":")', 1)
            if "draft.ai_review_sha256" not in content:
                content = content.replace("function applyDraft(draft, message) {",
                    'function applyDraft(draft, message) {\nif ((draft.ai_review_sha256 || null) !== (paper.ai_review_sha256 || null)) { flash("核验主张已变更，请重新判断。", "bad"); return; }', 1)
        return content.encode("utf-8")

    def available_jobs(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for paper in build_review_registry(self.kb)["papers"]:
            page_value = paper["files"].get("review_html")
            results.append(
                {
                    "job_id": paper.get("job_id"),
                    "paper_id": str(paper["paper_id"]),
                    "title": str(paper["title"]),
                    "zotero_key": str(paper["zotero_key"]),
                    "state": str(paper["state"]),
                    "review_group": str(paper["review_group"]),
                    "page_ready": bool(page_value and Path(str(page_value)).is_file()),
                }
            )
        return results

    def index_page(self) -> bytes:
        cards = []
        # Surface every managed paper from the project ledger so the user sees
        # the full reviewed/unreviewed split. Papers without a generated review
        # page render as disabled cards; the tab filter keeps reviewable ones
        # easy to reach without hiding the legacy pool.
        all_jobs = self.available_jobs()
        total_managed = len(all_jobs)
        page_ready_count = sum(1 for job in all_jobs if job["page_ready"])
        jobs = all_jobs
        state_labels = {
            "legacy_unconfirmed": "尚未生成阅读材料",
            "candidate": "等待来源核验",
            "identity_verified": "等待PDF",
            "source_ready": "等待AI阅读",
            "reading": "AI阅读中",
            "ai_note_ready": "阅读材料待生成",
            "ai_verified": "AI已核验，可随时人工评审",
            "human_reviewing": "可以开始评审",
            "human_confirmed": "已评审",
            "indexed": "已评审并入库",
            "synthesis_eligible": "已评审，可用于综合",
            "excluded": "已评审，已排除",
            "blocked_source": "来源阻塞",
            "needs_clarification": "需要补充判断",
            "rescinded": "许可已撤回",
        }
        for job in jobs:
            group = html.escape(job["review_group"])
            label = html.escape(state_labels.get(job["state"], job["state"]))
            title = html.escape(job["title"])
            meta = f"{html.escape(job['zotero_key'])} · {label}"
            if job["page_ready"] and job["job_id"]:
                url = f"/jobs/{quote(str(job['job_id']))}?token={quote(self.token)}"
                body = f'<a href="{url}"><strong>{title}</strong><span>{meta}</span></a>'
            else:
                body = f'<div class="paper disabled"><strong>{title}</strong><span>{meta}</span></div>'
            cards.append(f'<li data-group="{group}">{body}</li>')
        content = "".join(cards) or "<li class=\"empty\">还没有可阅读的论文页面。</li>"
        reviewed = sum(job["review_group"] == "reviewed" for job in jobs)
        unreviewed = len(jobs) - reviewed
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>论文人工评审</title><style>
body{{margin:0;background:#f3f5f7;color:#17202a;font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif}}
main{{width:min(980px,calc(100% - 32px));margin:40px auto}}h1{{margin-bottom:4px}}p{{color:#65717f;margin-top:0}}
.tabs{{display:flex;gap:10px;margin:22px 0 18px}}button{{border:1px solid #cfd8df;background:#fff;border-radius:999px;padding:9px 18px;font:inherit;cursor:pointer}}button.active{{background:#216869;color:#fff;border-color:#216869}}
ul{{list-style:none;padding:0}}li{{margin:10px 0}}a,.paper{{display:flex;justify-content:space-between;gap:20px;padding:16px 18px;border:1px solid #d9e0e7;border-radius:12px;color:inherit;background:#fff;text-decoration:none}}
a:hover{{border-color:#216869}}.disabled{{background:#f8f9fa;color:#59636d}}span{{color:#65717f;white-space:nowrap}}.empty{{padding:20px;background:#fff;border-radius:12px}}
@media(max-width:640px){{a,.paper{{display:block}}span{{display:block;margin-top:6px;white-space:normal}}}}
</style></head><body><main><h1>论文人工评审</h1><p>知识库共管理 {total_managed} 篇论文，其中 {page_ready_count} 篇已生成阅读页面，可直接点击评审。</p>
<div class="tabs"><button class="active" data-filter="unreviewed">未评审 {unreviewed}</button><button data-filter="reviewed">已评审 {reviewed}</button><button data-filter="all">全部 {len(jobs)}</button></div>
<ul>{content}</ul></main><script>
const buttons=[...document.querySelectorAll('button[data-filter]')];const rows=[...document.querySelectorAll('li[data-group]')];
function show(group){{buttons.forEach(b=>b.classList.toggle('active',b.dataset.filter===group));rows.forEach(r=>r.hidden=group!=='all'&&r.dataset.group!==group);}}
buttons.forEach(b=>b.addEventListener('click',()=>show(b.dataset.filter)));show('unreviewed');
</script></body></html>"""
        return page.encode("utf-8")

    @serialized
    def save_review(self, job_id: str, review: Mapping[str, Any]) -> Path:
        if not isinstance(review, Mapping):
            raise ReviewServerError("评审内容必须是JSON对象")
        job = self._job(job_id)
        try:
            self.orchestrator.validate_human_review(review)
        except ValidationError as exc:
            raise ReviewServerError(str(exc)) from exc

        for field in ("job_id", "paper_id", "zotero_key"):
            if review[field] != job[field]:
                raise ReviewServerError(f"评审的{field}与当前论文不一致")

        artifact_fields = (
            ("pdf_sha256", "pdf_path"),
            ("ai_note_sha256", "ai_note_path"),
        )
        for hash_field, path_field in artifact_fields:
            artifact = Path(str(job.get(path_field) or ""))
            if not artifact.is_file():
                raise ReviewServerError(f"当前论文的{path_field}不存在，不能保存许可")
            current_hash = sha256_file(artifact)
            if current_hash != job.get(hash_field) or review[hash_field] != current_hash:
                raise ReviewServerError("PDF或AI笔记已变化，请重新生成评审页面")

        sections = list(review["sections"])
        if review["review_outcome"] == "human_confirmed":
            if {item["section_id"] for item in sections} != set(range(1, 9)):
                raise ReviewServerError("确认评审前必须完成八节判断")
            if any(item["verdict"] == "unable_to_judge" for item in sections):
                raise ReviewServerError("仍有无法判断的关键内容，不能标记为确认")
        permission = review["citation_permission"]

        target = Path(job["job_dir"]) / "human_reviews" / (
            f"{job['zotero_key']}.human-review.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".json", delete=False) as handle:
            pending = Path(handle.name)
        try:
            _atomic_json(pending, dict(review))
            self.orchestrator.import_human_review(pending)
            pending.replace(target)
        except (ReviewWorkflowError, ValueError) as exc:
            raise ReviewServerError(str(exc)) from exc
        finally:
            # 清理临时文件失败不应把已成功的保存报成失败：受沙箱回收站策略等影响，
            # unlink 可能抛 OSError；此时 replace 已完成，正式文件不受影响。
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
        write_review_registry(self.kb)
        return target.resolve()

    def load_review(self, job_id: str) -> dict[str, Any] | None:
        job = self._job(job_id)
        target = Path(job["job_dir"]) / "human_reviews" / (
            f"{job['zotero_key']}.human-review.json"
        )
        if not target.is_file():
            return None
        try:
            value = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewServerError("已保存的人工评审无法读取") from exc
        if not isinstance(value, dict) or value.get("job_id") != job_id:
            raise ReviewServerError("已保存的人工评审与当前论文不一致")
        return value


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: ReviewApplication) -> None:
        self.app = app
        super().__init__(address, ReviewRequestHandler)


class ReviewRequestHandler(BaseHTTPRequestHandler):
    server: ReviewHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _security_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self' 'unsafe-inline' data:; connect-src 'self'; "
            "img-src data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._security_headers(content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, value: Mapping[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _authorized(self, parsed: Any) -> bool:
        supplied = parse_qs(parsed.query).get("token", [""])[0]
        return secrets.compare_digest(supplied, self.server.app.token)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized(parsed):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "本机评审链接无效"})
            return
        try:
            if parsed.path == "/":
                self._send(HTTPStatus.OK, self.server.app.index_page(), "text/html; charset=utf-8")
                return
            api_match = re.fullmatch(r"/api/reviews/(paper-[0-9a-f]{16})", parsed.path)
            if api_match:
                review = self.server.app.load_review(api_match.group(1))
                if review is None:
                    self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "尚无已保存评审"})
                else:
                    self._json(HTTPStatus.OK, {"ok": True, "review": review})
                return
            match = re.fullmatch(r"/jobs/(paper-[0-9a-f]{16})", parsed.path)
            if match:
                self._send(
                    HTTPStatus.OK,
                    self.server.app.review_page(match.group(1)),
                    "text/html; charset=utf-8",
                )
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "页面不存在"})
        except ReviewServerError as exc:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": str(exc)})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not self._authorized(parsed):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "本机评审链接无效"})
            return
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Origin") != expected_origin:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "拒绝非本机页面提交"})
            return
        match = re.fullmatch(r"/api/reviews/(paper-[0-9a-f]{16})", parsed.path)
        if not match:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "保存地址不存在"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 0 < length <= _MAX_REVIEW_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "message": "评审文件大小无效"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            target = self.server.app.save_review(match.group(1), payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "评审JSON无法解析"})
            return
        except ReviewServerError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": str(exc)})
            return
        self._json(
            HTTPStatus.OK,
            {"ok": True, "message": "评审已保存", "file_name": target.name},
        )


def make_server(
    kb: str | Path, *, port: int = 0, token: str | None = None
) -> ReviewHTTPServer:
    """Create a server bound exclusively to the IPv4 loopback interface."""

    return ReviewHTTPServer(("127.0.0.1", port), ReviewApplication(kb, token=token))


__all__ = ["ReviewApplication", "ReviewServerError", "make_server"]
