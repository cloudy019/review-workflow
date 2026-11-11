"""Replaceable model providers, without workflow state or human authority."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .core import ValidationError
from .protocol import example_result


class Reader(Protocol):
    name: str
    def generate(self, request: dict[str, Any]) -> dict[str, Any]: ...


class OpenAICompatibleReader:
    """Direct JSON chat completions, including configurable DeepSeek endpoints."""
    name = "openai-compatible"
    visual_review = False

    def __init__(self, *, base_url: str, model: str, api_key_env: str = "DEEPSEEK_API_KEY", chunk_chars: int = 18000, timeout: int = 90, max_tokens: int = 12000, progress: Callable[[str], None] | None = None):
        parsed = urlparse(base_url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValidationError("use HTTPS or a loopback endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValidationError("endpoint must not contain credentials or query parameters")
        if not model.strip() or chunk_chars < 2000:
            raise ValidationError("model and a usable chunk size are required")
        self.base_url, self.model, self.api_key_env = base_url.rstrip("/"), model, api_key_env
        self.chunk_chars, self.timeout, self.max_tokens = chunk_chars, timeout, max_tokens
        self.progress = progress or (lambda message: None)

    def _call(self, system: str, payload: dict) -> dict:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise ValidationError(f"set the API key environment variable {self.api_key_env}")
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "response_format": {"type": "json_object"}, "max_tokens": self.max_tokens, "stream": False}
        call = Request(self.base_url + "/chat/completions", data=json.dumps(body).encode("utf-8"), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            with urlopen(call, timeout=self.timeout) as response:
                record = json.load(response)
        except HTTPError as exc:
            raise ValidationError(f"model API returned HTTP {exc.code}; no task was marked complete") from None
        except (URLError, TimeoutError, json.JSONDecodeError):
            raise ValidationError("model API did not return a complete response") from None
        if not isinstance(record, dict):
            raise ValidationError("model API response must be an object")
        choices = record.get("choices") or []
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or choices[0].get("finish_reason") != "stop":
            raise ValidationError("model response was truncated or incomplete")
        message = choices[0].get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ValidationError("model returned empty JSON content")
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            raise ValidationError("model returned invalid JSON; task remains unfinished") from None
        if not isinstance(result, dict):
            raise ValidationError("model JSON must be an object")
        return result

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        prompt = (Path(__file__).parent / "prompts" / "read.md").read_text(encoding="utf-8")
        chunks, pending, size = [], [], 0
        # Every page is visited; oversized individual pages are split, never truncated.
        for page in request["source_pages"]:
            text = page["text"]
            for offset in range(0, max(1, len(text)), self.chunk_chars):
                part = {"page": page["page"], "text": text[offset:offset + self.chunk_chars]}
                if pending and size + len(part["text"]) > self.chunk_chars:
                    chunks.append(pending)
                    pending, size = [], 0
                pending.append(part)
                size += len(part["text"])
        if pending:
            chunks.append(pending)
        context = {key: request[key] for key in ("paper_id", "pdf_sha256", "research_context", "figure_inventory")}
        if len(chunks) == 1:
            self.progress("reading all source pages")
            return self._call(prompt, {**context, "source_pages": chunks[0], "output_example": example_result(request), "visual_review": False})
        observations = []
        for number, chunk in enumerate(chunks, 1):
            self.progress(f"reading source chunk {number}/{len(chunks)}")
            item = self._call("Read all supplied page text. Output json: {observations:[{text,source_pages,kind}]}. Preserve methods, numeric results, limitations, figure captions and source/inference distinction. Do not make human decisions.", {**context, "source_pages": chunk})
            entries = item.get("observations")
            allowed_pages = {page["page"] for page in chunk}
            if not isinstance(entries, list) or not entries:
                raise ValidationError("chunk reader did not preserve evidence observations")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("text"), str) or not entry["text"].strip() or not isinstance(entry.get("source_pages"), list) or not entry["source_pages"] or any(type(page) is not int or page not in allowed_pages for page in entry["source_pages"]) or entry.get("kind") not in {"source_statement", "ai_inference"}:
                    raise ValidationError("chunk observation has invalid source anchors")
            observations.extend(entries)
        if len(json.dumps(observations, ensure_ascii=False)) > self.chunk_chars * 5:
            raise ValidationError("synthesis evidence exceeds configured budget; increase chunk size/model context explicitly")
        self.progress("synthesizing reading output from all chunks")
        return self._call(prompt, {**context, "observations": observations, "output_example": example_result(request), "visual_review": False})


class ExternalAgentReader:
    """Codex, WorkBuddy, or any other host submits the same local JSON contract."""
    name = "external-agent"

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        raise ValidationError("external agents read the issued request and submit result JSON; issuing a request is not AI reading")


class CommandReader:
    """A host model can return JSON on stdout; the core owns all file writes."""
    visual_review = False

    def __init__(self, command: list[str], *, name: str = "command-reader", timeout: int = 300, cwd: str | None = None, response_archive: Path | None = None):
        if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
            raise ValidationError("command must be an explicit argument list")
        self.command, self.name, self.timeout, self.cwd = command, name, timeout, cwd
        self.response_archive = response_archive

    def generate(self, request):
        payload = {key: request[key] for key in ("paper_id", "pdf_sha256", "research_context", "source_pages", "figure_inventory", "instructions", "output_example")}
        # The text-only subprocess adapter does not expose personal source paths.
        payload["source_pages"] = [{"page": item["page"], "text": item["text"]} for item in payload["source_pages"]]
        prompt = "Read the supplied paper text yourself and return ONLY the completed result JSON. Do not call tools or delegate. Human judgment remains pending.\n" + json.dumps(payload, ensure_ascii=False)
        try:
            process = subprocess.run(self.command, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                     timeout=self.timeout, cwd=self.cwd,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            raise ValidationError("host reader did not return a completed response") from None
        if process.returncode:
            raise ValidationError(f"host reader exited with status {process.returncode}; task remains unfinished")
        if self.response_archive:
            self.response_archive.parent.mkdir(parents=True, exist_ok=True)
            self.response_archive.write_text(process.stdout, encoding="utf-8")
        return self.parse_output(process.stdout)

    @staticmethod
    def parse_output(output: str) -> dict:
        try:
            envelope = json.loads(output)
            if isinstance(envelope, list):
                terminal = [item for item in envelope if isinstance(item, dict) and item.get("type") == "result"]
                if len(terminal) != 1:
                    raise ValueError()
                envelope = terminal[0]
            if not isinstance(envelope, dict) or envelope.get("is_error"):
                raise ValueError()
            if envelope.get("type") == "result" and envelope.get("subtype") not in {None, "success"}:
                raise ValueError()
            result = envelope.get("structured_output", envelope.get("result", envelope))
            if isinstance(result, str):
                result = result.strip()
                if result.startswith("```json\n") and result.endswith("```"):
                    result = result[8:-3].strip()
                result = json.loads(result)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, TypeError):
            raise ValidationError("host reader returned invalid JSON; no human judgment was created") from None
