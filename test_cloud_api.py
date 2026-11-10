import json
import subprocess
from datetime import date, timedelta

from fastapi.testclient import TestClient
import cloud_api

TOKEN = "t" * 40
HEADERS = {"Authorization": "Bearer " + TOKEN}


def request():
    return {"origin_airport_code": "SFO", "destination_airport_code": "JFK",
            "depart_date": (date.today() + timedelta(days=30)).isoformat()}


def test_health_does_not_need_provider_or_secret(monkeypatch):
    monkeypatch.delenv("FLIGHT_API_TOKEN", raising=False)
    with TestClient(cloud_api.app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/search/flights", json=request()).status_code == 503


def test_authentication_and_input_reject_before_search(monkeypatch):
    monkeypatch.setenv("FLIGHT_API_TOKEN", TOKEN)
    def must_not_run(*args, **kwargs):
        raise AssertionError("Invalid requests must not start a search")
    monkeypatch.setattr(cloud_api.subprocess, "run", must_not_run)
    with TestClient(cloud_api.app) as client:
        assert client.post("/search/flights", json=request()).status_code == 401
        invalid = dict(request(), depart_date="2020-01-01")
        assert client.post("/search/flights", headers=HEADERS, json=invalid).status_code == 422
        invalid = dict(request(), return_date="2020-01-01")
        assert client.post("/search/flights", headers=HEADERS, json=invalid).status_code == 422
        invalid = dict(request(), adults=0)
        assert client.post("/search/flights", headers=HEADERS, json=invalid).status_code == 422


def test_provider_block_is_not_reported_as_empty_flights(monkeypatch):
    monkeypatch.setenv("FLIGHT_API_TOKEN", TOKEN)
    monkeypatch.setattr(cloud_api.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, json.dumps({"error": "provider_blocked"})))
    with TestClient(cloud_api.app) as client:
        response = client.post("/search/flights", headers=HEADERS, json=request())
        assert response.status_code == 502
        assert response.json()["detail"]["error"] == "provider_blocked"


def test_timeout_releases_capacity_and_allows_next_search(monkeypatch):
    monkeypatch.setenv("FLIGHT_API_TOKEN", TOKEN)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("search", 50)
    monkeypatch.setattr(cloud_api.subprocess, "run", timeout)
    with TestClient(cloud_api.app) as client:
        for _ in range(3):
            assert client.post("/search/flights", headers=HEADERS, json=request()).status_code == 504
        monkeypatch.setattr(cloud_api.subprocess, "run", lambda *a, **kw:
                            subprocess.CompletedProcess(a, 0, '{"buckets": []}'))
        assert client.post("/search/flights", headers=HEADERS, json=request()).status_code == 200


def test_invalid_provider_output_is_sanitized(monkeypatch):
    monkeypatch.setenv("FLIGHT_API_TOKEN", TOKEN)
    monkeypatch.setattr(cloud_api.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, "private provider details"))
    with TestClient(cloud_api.app) as client:
        response = client.post("/search/flights", headers=HEADERS, json=request())
        assert response.status_code == 502
        assert "private" not in response.text
