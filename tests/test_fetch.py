from __future__ import annotations

import subprocess
import sys
import threading
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from kdpw_rze import transport
from kdpw_rze.collector import Collector, cooldown_until
from kdpw_rze.common import CollectorError
from kdpw_rze.locking import exclusive_lock


@pytest.fixture
def server(monkeypatch):
    state = {"status": 200, "body": b"", "headers": {}, "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(dict(self.headers))
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(state["body"])))
            for key, val in state["headers"].items():
                self.send_header(key, val)
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *_):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(transport, "EXPORT_URL", f"http://127.0.0.1:{httpd.server_port}/export")
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_one_request_per_day_and_local_import_stays_offline(tmp_path, make_zip, clock, server):
    path = make_zip()
    server["body"] = path.read_bytes()
    collector = Collector(tmp_path / "data", now_fn=clock)
    result = collector.fetch()
    assert result["status"] == "complete"
    assert result["http_status"] == 200
    # A fresh process/object still sees the persisted daily limit.
    assert Collector(tmp_path / "data", now_fn=clock).fetch()["status"] == "already_attempted"
    collector.import_local(path)
    collector.resume()
    assert collector.fetch()["status"] == "already_attempted"
    assert len(server["requests"]) == 1
    assert server["requests"][0]["User-Agent"] == transport.USER_AGENT
    clock.instant += timedelta(days=1)
    assert collector.fetch()["report_id"] == result["report_id"]
    assert len(server["requests"]) == 2


@pytest.mark.parametrize("status", [401, 403])
def test_access_refusal_pauses_across_days_resume_keeps_daily_limit(tmp_path, clock, server, make_zip, status):
    server["status"] = status
    collector = Collector(tmp_path / "data", now_fn=clock)
    with pytest.raises(CollectorError, match=str(status)):
        collector.fetch()
    assert collector.fetch()["status"] == "paused"
    collector.resume()
    assert collector.fetch()["status"] == "already_attempted"
    clock.instant += timedelta(days=1)
    with pytest.raises(CollectorError):
        collector.fetch()
    clock.instant += timedelta(days=3)
    assert collector.fetch()["status"] == "paused"
    assert len(server["requests"]) == 2
    assert collector.status()["last_run"]["http_status"] == status
    collector.resume()
    server["status"] = 200
    server["body"] = make_zip().read_bytes()
    assert collector.fetch()["status"] == "complete"


def test_rate_limit_persists_retry_after_and_resume_cannot_bypass(tmp_path, clock, server):
    server["status"] = 429
    server["headers"]["Retry-After"] = str(3 * 86400)
    collector = Collector(tmp_path / "data", now_fn=clock)
    with pytest.raises(CollectorError, match="429"):
        collector.fetch()
    clock.instant += timedelta(days=1)
    collector.resume()
    assert collector.fetch()["status"] == "cooldown"
    assert len(server["requests"]) == 1
    clock.instant += timedelta(days=3)
    with pytest.raises(CollectorError):
        collector.fetch()
    assert len(server["requests"]) == 2


@pytest.mark.parametrize("header", [None, "invalid", "-1", "0", "60", "Tue, 17 Nov 2026 13:00:00 GMT"])
def test_short_or_invalid_retry_after_waits_at_least_until_polish_midnight(clock, header):
    until = datetime.fromisoformat(cooldown_until(clock(), header))
    assert until == datetime(2026, 11, 17, 23, tzinfo=UTC)


def test_http_date_retry_after_and_dst():
    now = datetime(2026, 3, 28, 23, 30, tzinfo=UTC)
    assert cooldown_until(now, None) == "2026-03-29T22:00:00+00:00"
    assert cooldown_until(now, "Tue, 31 Mar 2026 15:00:00 GMT") == "2026-03-31T15:00:00+00:00"


@pytest.mark.parametrize("status", [302, 500, 503])
def test_errors_and_redirects_are_not_retried(tmp_path, clock, server, status):
    server["status"] = status
    server["headers"]["Location"] = transport.EXPORT_URL + "?redirected=1"
    collector = Collector(tmp_path / "data", now_fn=clock)
    with pytest.raises(CollectorError):
        collector.fetch()
    assert collector.fetch()["status"] == "already_attempted"
    assert len(server["requests"]) == 1


def test_timeout_is_recorded_without_retry(tmp_path, clock, monkeypatch):
    calls = []

    def timeout(_):
        calls.append(1)
        raise TimeoutError("connection timeout")

    monkeypatch.setattr(transport, "download", timeout)
    collector = Collector(tmp_path / "data", now_fn=clock)
    with pytest.raises(TimeoutError):
        collector.fetch()
    assert collector.fetch()["status"] == "already_attempted"
    assert len(calls) == 1
    assert collector.status()["last_run"]["status"] == "failed"


def test_download_size_limit_cleans_temporary_file(tmp_path, clock, server, monkeypatch):
    server["body"] = b"too large"
    monkeypatch.setattr(transport, "MAX_ZIP_BYTES", 2)
    collector = Collector(tmp_path / "data", now_fn=clock)
    with pytest.raises(CollectorError):
        collector.fetch()
    assert not list((tmp_path / "data").rglob("*.part"))
    assert collector.fetch()["status"] == "already_attempted"


def test_lock_is_cross_process_and_released(tmp_path):
    script = """from pathlib import Path
from kdpw_rze.locking import exclusive_lock
from kdpw_rze.common import CollectorError
import sys
try:
    with exclusive_lock(Path(sys.argv[1])):
        print('acquired')
except CollectorError:
    print('blocked')
"""
    with exclusive_lock(tmp_path):
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0
        assert result.stdout.strip() == "blocked"
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.stdout.strip() == "acquired"


def test_polish_day_boundary_not_utc_day(tmp_path, make_zip, clock, server):
    clock.instant = datetime(2026, 11, 17, 22, 59, tzinfo=UTC)
    server["body"] = make_zip().read_bytes()
    collector = Collector(tmp_path / "data", now_fn=clock)
    first = collector.fetch()
    clock.instant += timedelta(minutes=2)
    second = collector.fetch()
    assert first["observation_date"] == "2026-11-17"
    assert second["observation_date"] == "2026-11-18"
    # Clock moving backwards also must not reopen the daily allowance.
    clock.instant -= timedelta(days=2)
    assert collector.fetch()["status"] == "already_attempted"
    assert len(server["requests"]) == 2
