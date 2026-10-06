"""caflood_common.py: FileStorageClient against a fake HTTP session."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from caflood_common import Config, FileStorageClient, FileStorageError

URL = "https://dataflow.stellio.io/dev/storage"
BUCKET = "urbaquantum-caflood-inputs-dem"


class FakeResponse:
    def __init__(self, status=200, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.text = content.decode(errors="replace")
        self.ok = status < 400
        self.is_redirect = status in (301, 302, 303, 307, 308) and "Location" in self.headers

    def json(self):
        return json.loads(self.content)


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def client(response):
    config = Config(
        stellio_base_url="https://stellio", stellio_token_url="https://sso/token",
        stellio_client_id="nifi", stellio_client_secret="secret",
        stellio_context_url=None, stellio_tenant=None,
        work_dir=Path("."), data_dir=Path("."), file_storage_url=URL,
    )
    c = FileStorageClient(config)
    c._session = FakeSession(response)
    c.access_token = lambda: "TOKEN"
    return c


def test_upload_posts_raw_body_and_returns_s3_url():
    c = client(FakeResponse(200, b'{"s3.url": "https://store/' + BUCKET.encode() + b'/abc-123"}'))
    assert c.upload(BUCKET, b"data") == f"https://store/{BUCKET}/abc-123"
    method, url, kwargs = c._session.calls[0]
    assert (method, url, kwargs["params"], kwargs["data"]) == ("POST", URL, {"bucket": BUCKET}, b"data")
    assert kwargs["headers"]["Authorization"] == "Bearer TOKEN"
    assert kwargs["allow_redirects"] is False


@pytest.mark.parametrize("object_id", ["abc-123", f"https://store/{BUCKET}/abc-123"])
def test_fetch_and_delete_send_the_bare_key(object_id):
    c = client(FakeResponse(200, b"file bytes"))
    assert c.fetch(BUCKET, object_id) == b"file bytes"
    c.delete(BUCKET, object_id)
    assert [(m, kw["params"]) for m, _, kw in c._session.calls] == [
        ("GET", {"bucket": BUCKET, "id": "abc-123"}),
        ("DELETE", {"bucket": BUCKET, "id": "abc-123"}),
    ]


@pytest.mark.parametrize("response, message", [
    (FakeResponse(401, b"<echoed request body>"), "401 - token from https://sso/token rejected"),
    (FakeResponse(405), "405 - GET not allowed"),
    (FakeResponse(302, headers={"Location": "/nifi/"}), "redirect to '/nifi/'"),
    # binary echoed body -> ASCII-safe excerpt (the console must be able to print it)
    (FakeResponse(500, bytes([98, 111, 111, 109, 0xFF])), r"HTTP 500 (body excerpt: 'boom\\xff')"),
])
def test_errors_are_explained(response, message):
    with pytest.raises(FileStorageError, match=re.escape(message)):
        client(response).fetch(BUCKET, "abc-123")


def test_upload_with_unexpected_answer():
    with pytest.raises(FileStorageError, match="unexpected response"):
        client(FakeResponse(200, b"<html>")).upload(BUCKET, b"data")


# --- SSH helpers -------------------------------------------------------------------

from caflood_common import run_ssh, ssh_argv  # noqa: E402

SSH = {"key": "/k", "known_hosts": "/kh", "port": "41596", "target": "u@h", "control_dir": "/tmp/x"}


def test_ssh_argv_shares_one_connection_by_default():
    argv = ssh_argv(SSH, "true")
    assert "ControlMaster=auto" in argv and "ControlPath=/tmp/x/caflood-ssh-%C" in argv
    assert any(a.startswith("ControlPersist=") for a in argv)
    assert argv[-2:] == ["u@h", "true"]  # target and command stay last


def test_ssh_argv_multiplexing_can_be_disabled():
    assert not any("Control" in a for a in ssh_argv({**SSH, "multiplex": "0"}, "true"))


def _fake_ssh(monkeypatch, script: str):
    import sys as _sys
    monkeypatch.setattr("caflood_common.ssh_argv", lambda settings, cmd: [_sys.executable, "-c", script])


def test_run_ssh_returns_stdout_and_reports_stderr(monkeypatch):
    _fake_ssh(monkeypatch, "import sys; sys.stdout.write(sys.stdin.read().upper())")
    assert run_ssh(SSH, "cat", input_text="abc") == "ABC"
    _fake_ssh(monkeypatch, "import sys; sys.stderr.write('Connection refused'); sys.exit(255)")
    with pytest.raises(RuntimeError, match="ssh exited with 255: Connection refused"):
        run_ssh(SSH, "true")


def test_run_ssh_does_not_wait_for_a_background_process_holding_stderr(monkeypatch):
    # Like an OpenSSH ControlPersist master started by the first call: it
    # outlives ssh, detaches from stdin/stdout but (older OpenSSH) may keep
    # the caller's stderr open.
    import time as _time
    _fake_ssh(monkeypatch, (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=sys.stderr); "
        "print('done')"))
    start = _time.monotonic()
    assert run_ssh(SSH, "true").strip() == "done"
    assert _time.monotonic() - start < 10
