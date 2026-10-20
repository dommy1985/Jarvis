import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from jarvis import config
from jarvis.server import App

CONFIG = Path(__file__).resolve().parent / "fixtures" / "projects.yaml"


@pytest.fixture
def base_url(tmp_path):
    app = App(config.load(CONFIG), str(tmp_path / "t.db"), token="secret")
    server = app.server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def call(url, body=None, token="secret"):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_health_is_public(base_url):
    assert call(f"{base_url}/health", token=None) == (200, {"ok": True})


def test_auth_required(base_url):
    assert call(f"{base_url}/sources", token="wrong")[0] == 401
    assert call(f"{base_url}/ingest", body={"tasks": []}, token=None)[0] == 401


def test_ingest_roundtrip(base_url):
    task = {
        "source": "github", "external_id": "alex-dev/catch-ball#3", "container": "alex-dev/catch-ball",
        "title": "t", "url": "u", "source_status": "OPEN",
    }
    assert call(f"{base_url}/ingest", body={"tasks": [task]}) == (
        200, {"inserted": 1, "updated": 0, "unchanged": 0, "closed": 0},
    )
    assert call(f"{base_url}/ingest", body={"tasks": "x"})[0] == 400


def test_plan_and_briefing_endpoints(tmp_path):
    def fake(system, message, schema, model, timeout):
        return {"summary": "ok", "items": []}

    app = App(config.load(CONFIG), str(tmp_path / "p.db"), token="secret", runner=fake)
    server = app.server("127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert call(f"{url}/briefing?date=2026-10-11")[0] == 404
        status, body = call(f"{url}/plan", body={"date": "2026-10-11", "available_hours": 2})
        assert (status, body["status"], body["available_hours"]) == (200, "draft", 2)
        assert call(f"{url}/briefing?date=2026-10-11")[1]["summary"] == "Nessun task aperto ed eligible da proporre oggi."
        assert call(f"{url}/plan", body={"date": "11/10"})[0] == 400
    finally:
        server.shutdown()
