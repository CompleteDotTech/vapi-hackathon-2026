"""Provision matching Railway/Vapi credentials without displaying or saving secrets.

Uses Python's standard library. API credentials are supplied through the environment.
Run manually or in a secret-enabled CI job; never run as part of the web server.
"""
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
VAPI_API = "https://api.vapi.ai"


class SetupError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward platform credentials to a redirected host.
        return None


def json_request(url, headers, payload=None, method=None):
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # Remote error bodies may echo secrets. Do not print them or the request.
        raise SetupError(f"{urllib.parse.urlsplit(url).hostname} returned HTTP {error.code}") from None
    except (urllib.error.URLError, ValueError, OSError):
        raise SetupError("Platform connection or JSON response failed") from None


def config_from_env():
    required = ["RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID",
                "VAPI_API_KEY", "FLIGHT_API_URL"]
    missing = [name for name in required if not os.environ.get(name)]
    project_token = os.environ.get("RAILWAY_TOKEN")
    account_token = os.environ.get("RAILWAY_API_TOKEN")
    if not project_token and not account_token:
        missing.append("RAILWAY_TOKEN (project token) or RAILWAY_API_TOKEN (account token)")
    if missing:
        raise SetupError("Missing setup configuration: " + ", ".join(missing))
    base = os.environ["FLIGHT_API_URL"].rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise SetupError("FLIGHT_API_URL must be a bare HTTPS service origin")
    return {
        "projectId": os.environ["RAILWAY_PROJECT_ID"],
        "environmentId": os.environ["RAILWAY_ENVIRONMENT_ID"],
        "serviceId": os.environ["RAILWAY_SERVICE_ID"],
        "railway_headers": ({"Project-Access-Token": project_token} if project_token
                            else {"Authorization": "Bearer " + account_token}),
        "vapi_headers": {"Authorization": "Bearer " + os.environ["VAPI_API_KEY"]},
        "base_url": base,
    }


def unique_named(items, name):
    if not isinstance(items, list):
        raise SetupError("Unexpected Vapi list response; no resources were changed")
    matches = [item for item in items if item.get("name") == name]
    if len(matches) > 1:
        raise SetupError("Duplicate managed resources found; select the intended resource before retrying")
    return matches[0] if matches else None


def provision(config, request=json_request):
    scope = {key: config[key] for key in ("projectId", "environmentId", "serviceId")}

    def railway(query, variables):
        response = request(RAILWAY_API, config["railway_headers"],
                           {"query": query, "variables": variables})
        if response.get("errors") or "data" not in response:
            raise SetupError("Railway rejected the operation; check token permissions and target IDs")
        return response["data"]

    existing = railway(
        "query($projectId:String!,$environmentId:String!,$serviceId:String!){"
        "variables(projectId:$projectId,environmentId:$environmentId,serviceId:$serviceId)}", scope,
    ).get("variables")
    if not isinstance(existing, dict):
        raise SetupError("Railway did not return service variables")
    token = existing.get("FLIGHT_API_TOKEN")
    if token is not None and (not isinstance(token, str) or len(token) < 32 or token.startswith("${{")):
        raise SetupError("Existing flight token cannot be reused; inspect it without exposing its value")

    name = "westbound-flight-" + config["serviceId"]
    credential = unique_named(request(VAPI_API + "/credential", config["vapi_headers"]), name)
    tool = unique_named(request(VAPI_API + "/tool", config["vapi_headers"]), name)
    if credential and credential.get("provider") != "custom-credential":
        raise SetupError("Managed credential name belongs to a different provider")
    if tool and (tool.get("type") != "apiRequest" or tool.get("url") != config["base_url"] + "/search/flights"):
        raise SetupError("Managed tool name belongs to a different endpoint")
    # Reject unrecognized state before making any writes.
    if any(item and not item.get("id") for item in (credential, tool)):
        raise SetupError("Managed Vapi resource is missing its ID")

    created_token = token is None
    if created_token:
        token = secrets.token_urlsafe(32)
        saved = railway(
            "mutation($input:VariableUpsertInput!){variableUpsert(input:$input)}",
            {"input": {**scope, "name": "FLIGHT_API_TOKEN", "value": token}},
        )
        if saved.get("variableUpsert") is not True:
            raise SetupError("Railway did not confirm storing the flight token")

    credential_body = {
        "provider": "custom-credential", "name": name,
        "authenticationPlan": {"type": "bearer", "token": token,
                               "headerName": "Authorization", "bearerPrefixEnabled": True},
    }
    credential = request(
        VAPI_API + "/credential" + ("/" + urllib.parse.quote(credential["id"], safe="") if credential else ""),
        config["vapi_headers"], credential_body, method="PATCH" if credential else "POST",
    )
    credential_id = credential.get("id")
    if not credential_id:
        raise SetupError("Vapi did not confirm the credential; rerun to resume")

    schema = json.loads((Path(__file__).parent / "vapi-flight-body.schema.json").read_text())
    tool_body = {
        "type": "apiRequest", "name": name,
        "description": "Find flight quotes after confirming airport codes, dates and passengers. Never invent unavailable fares.",
        "method": "POST", "url": config["base_url"] + "/search/flights",
        "credentialId": credential_id, "timeoutSeconds": 60, "body": schema,
        "messages": [
            {"type": "request-start", "content": "I'll compare flights for those dates."},
            {"type": "request-response-delayed", "timingMilliseconds": 8000,
             "content": "I'm still waiting for the flight results."},
        ],
    }
    tool = request(
        VAPI_API + "/tool" + ("/" + urllib.parse.quote(tool["id"], safe="") if tool else ""),
        config["vapi_headers"], tool_body, method="PATCH" if tool else "POST",
    )
    if not tool.get("id"):
        raise SetupError("Vapi did not confirm the tool; rerun to resume")
    # Return IDs/status only; never tokens, request bodies, or response objects.
    return {"flight_token": "created" if created_token else "reused",
            "vapi_credential_id": credential_id, "vapi_tool_id": tool["id"]}


if __name__ == "__main__":
    try:
        print(json.dumps(provision(config_from_env())))
    except SetupError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("Setup failed; no secrets were logged. Check configuration and rerun.", file=sys.stderr)
        sys.exit(1)
