import json
import subprocess
import sys
from pathlib import Path

import pytest

from evidence_loop.core import ValidationError
from evidence_loop.locking import store_lock
from evidence_loop.providers import CommandReader
from evidence_loop.workflow import Workflow
from test_workflow import human, ready, result_for, source


@pytest.mark.parametrize("envelope", ["raw", "result", "structured_output", "workbuddy-array"])
def test_command_reader_same_contract_without_host_file_tools(tmp_path, envelope):
    flow = Workflow(tmp_path / "workspace")
    job = flow.add("example", source(tmp_path))
    request_path = flow.issue(job["job_id"], reader="workbuddy")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    result = result_for(request)
    record = result if envelope == "raw" else {envelope: json.dumps(result) if envelope == "result" else result, "is_error": False}
    if envelope == "workbuddy-array":
        record = [{"type": "message", "content": "fixture"}, {"type": "result", "subtype": "success", "result": json.dumps(result), "is_error": False}]
    script = tmp_path / "fake_host.py"
    script.write_text("import json,sys\npayload=json.loads(sys.stdin.read().split('\\n',1)[1])\nassert 'pdf_path' not in payload\nassert all('image' not in page for page in payload['source_pages'])\nprint(" + repr(json.dumps(record)) + ")\n", encoding="utf-8")
    reader = CommandReader([sys.executable, str(script)], name="workbuddy")
    completed = flow.submit(job["job_id"], request["attempt_id"], reader.generate(request))
    assert completed["state"] == "human_reviewing"
    assert not completed["citation_permission"]["allowed"]


def test_process_lock_blocks_competing_writer(tmp_path):
    script = tmp_path / "competitor.py"
    script.write_text("from pathlib import Path\nfrom evidence_loop.locking import store_lock\nimport sys\ntry:\n with store_lock(Path(sys.argv[1]),timeout=0.1):\n  raise SystemExit(2)\nexcept TimeoutError:\n print('blocked')\n", encoding="utf-8")
    with store_lock(tmp_path / "store"):
        process = subprocess.run([sys.executable, str(script), str(tmp_path / "store")], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "blocked"


def test_revision_requires_new_human_judgment(tmp_path):
    from evidence_loop.review_server import ReviewApplication
    flow, job, _ = ready(tmp_path)
    ReviewApplication(flow.workspace).save_review(job["job_id"], human(job))
    assert flow.knowledge()
    reopened = flow.reopen(job["job_id"])
    assert reopened["state"] == "source_ready"
    assert flow.knowledge() == []
    request = json.loads(flow.issue(job["job_id"], reader="workbuddy").read_text(encoding="utf-8"))
    revised = flow.submit(job["job_id"], request["attempt_id"], result_for(request))
    assert revised["state"] == "human_reviewing"
    assert flow.knowledge() == []
    assert list((Path(job["job_dir"]) / "input").glob("revision-*.json"))
