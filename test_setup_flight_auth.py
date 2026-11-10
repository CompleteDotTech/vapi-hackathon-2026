import json
import urllib.error
import pytest
import setup_flight_auth as setup


def config():
    return {"projectId": "project", "environmentId": "environment", "serviceId": "service",
            "railway_headers": {}, "vapi_headers": {}, "base_url": "https://example.up.railway.app"}


class FakePlatforms:
    def __init__(self):
        self.token = None
        self.credential = None
        self.tool = None
        self.writes = []

    def request(self, url, headers, payload=None, method=None):
        if url == setup.RAILWAY_API:
            if payload["query"].startswith("query"):
                return {"data": {"variables": {} if self.token is None else {"FLIGHT_API_TOKEN": self.token}}}
            self.writes.append((url, payload))
            self.token = payload["variables"]["input"]["value"]
            return {"data": {"variableUpsert": True}}
        if method is None:
            item = self.credential if url.endswith("/credential") else self.tool
            return [item] if item else []
        self.writes.append((url, payload))
        if "/credential" in url:
            self.credential = {**payload, "id": "credential-id"}
            return self.credential
        self.tool = {**payload, "id": "tool-id"}
        return self.tool


def test_generated_secret_is_shared_without_being_returned():
    fake = FakePlatforms()
    result = setup.provision(config(), fake.request)
    assert len(fake.token) >= 32
    assert fake.credential["authenticationPlan"]["token"] == fake.token
    assert fake.tool["credentialId"] == "credential-id"
    assert result["flight_token"] == "created"
    assert fake.token not in json.dumps(result)


def test_rerun_reuses_token_and_resources():
    fake = FakePlatforms()
    setup.provision(config(), fake.request)
    token = fake.token
    result = setup.provision(config(), fake.request)
    assert fake.token == token
    assert result["flight_token"] == "reused"
    assert sum(url == setup.RAILWAY_API for url, _ in fake.writes) == 1
    assert len([url for url, _ in fake.writes if url.endswith("/credential")]) == 1
    assert len([url for url, _ in fake.writes if url.endswith("/tool")]) == 1


def test_existing_tool_conflict_stops_before_writes():
    fake = FakePlatforms()
    fake.tool = {"id": "other", "name": "westbound-flight-service", "type": "apiRequest", "url": "https://other.example"}
    with pytest.raises(setup.SetupError, match="different endpoint"):
        setup.provision(config(), fake.request)
    assert not fake.writes


def test_provider_authentication_failure_stops_before_generating_secret():
    fake = FakePlatforms()
    def denied(url, *args, **kwargs):
        if url != setup.RAILWAY_API:
            raise setup.SetupError("API access denied")
        return fake.request(url, *args, **kwargs)
    with pytest.raises(setup.SetupError, match="denied"):
        setup.provision(config(), denied)
    assert not fake.writes


def test_preflight_reports_names_not_secret_values(monkeypatch):
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    monkeypatch.setenv("VAPI_API_KEY", "must-never-be-shown")
    with pytest.raises(setup.SetupError) as error:
        setup.config_from_env()
    assert "RAILWAY_PROJECT_ID" in str(error.value)
    assert "must-never-be-shown" not in str(error.value)
