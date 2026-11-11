import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from evidence_loop.core import ValidationError
from evidence_loop.providers import OpenAICompatibleReader
from evidence_loop.workflow import Workflow
from test_workflow import result_for, source


@pytest.fixture
def api(monkeypatch):
    calls = []
    behavior = {"finish_reason": "stop", "empty": False}
    monkeypatch.setenv("FIXTURE_API_KEY", "synthetic-test-key")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": body, "authorization": self.headers["Authorization"]})
            payload = json.loads(body["messages"][1]["content"])
            if "output_example" in payload:
                example = payload["output_example"]
                response = result_for({"source_pages": [{"page": 1}], "figure_inventory": payload["figure_inventory"], **{key: example[key] for key in ("paper_id", "pdf_sha256")}})
            else:
                response = {"observations": [{"text": "Synthetic page label without experimental data", "source_pages": [item["page"]], "kind": "source_statement"} for item in payload["source_pages"]]}
            content = "" if behavior["empty"] else json.dumps(response)
            envelope = {"choices": [{"finish_reason": behavior["finish_reason"], "message": {"content": content}}]}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(envelope).encode("utf-8"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reader = OpenAICompatibleReader(base_url=f"http://127.0.0.1:{server.server_port}", model="configured-model", api_key_env="FIXTURE_API_KEY", chunk_chars=2000)
        yield reader, calls, behavior
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_direct_api_to_same_human_review_page(tmp_path, api):
    reader, calls, _ = api
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    finished = flow.read_api(job["job_id"], reader)
    assert finished["state"] == "human_reviewing"
    assert not finished["citation_permission"]["allowed"]
    assert not finished["reading_receipt"]["visual_review"]
    assert calls[0]["path"] == "/chat/completions"
    assert calls[0]["body"]["response_format"] == {"type": "json_object"}
    assert calls[0]["authorization"] == "Bearer synthetic-test-key"
    assert all("synthetic-test-key" not in file.read_text(encoding="utf-8") for file in flow.workspace.rglob("*.json"))


@pytest.mark.parametrize("behavior", [{"finish_reason": "length"}, {"empty": True}])
def test_incomplete_api_never_marks_job_complete(tmp_path, api, behavior):
    reader, _, state = api
    state.update(behavior)
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    with pytest.raises(ValidationError):
        flow.read_api(job["job_id"], reader)
    assert flow.core.get_job(job["job_id"])["state"] == "reading"
    assert not (Path(job["agent_output_dir"]) / "paper-review.html").exists()


def test_long_document_every_page_enters_chunk_reading(api):
    reader, calls, _ = api
    request = {"paper_id": "example", "pdf_sha256": "a" * 64, "research_context": "fixture", "figure_inventory": [],
               "source_pages": [{"page": n, "text": "Evidence label. " * 180} for n in range(1, 4)]}
    reader.generate(request)
    observed_pages = {page["page"] for call in calls[:-1] for page in json.loads(call["body"]["messages"][1]["content"])["source_pages"]}
    assert observed_pages == {1, 2, 3}
    assert "observations" in json.loads(calls[-1]["body"]["messages"][1]["content"])


def test_reject_insecure_or_embedded_credential_endpoint():
    for endpoint in ("http://example.invalid", "https://user:password@example.invalid", "https://example.invalid?key=secret"):
        with pytest.raises(ValidationError):
            OpenAICompatibleReader(base_url=endpoint, model="configured-model")
