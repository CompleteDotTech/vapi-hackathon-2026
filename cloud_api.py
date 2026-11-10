"""Authenticated HTTP adapter for the existing Skyscanner client."""
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parent
CAPACITY = threading.BoundedSemaphore(2)
app = FastAPI(title="Westbound flight search", docs_url=None, redoc_url=None,
              openapi_url=None)


def authenticate(authorization: Annotated[str | None, Header()] = None):
    token = os.environ.get("FLIGHT_API_TOKEN", "")
    if len(token) < 32:
        raise HTTPException(503, "Flight service authentication is not configured")
    if not secrets.compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "Invalid credentials")


class FlightSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    origin_airport_code: str = Field(pattern=r"^[A-Z]{3}$")
    destination_airport_code: str = Field(pattern=r"^[A-Z]{3}$")
    depart_date: date
    return_date: date | None = None
    adults: int = Field(default=1, ge=1, le=8, strict=True)
    cabin_class: Literal["economy", "premium_economy", "business", "first"] = "economy"

    @model_validator(mode="after")
    def dates_and_route(self):
        if self.depart_date < date.today():
            raise ValueError("Departure must not be in the past")
        if self.return_date and self.return_date < self.depart_date:
            raise ValueError("Return must be on or after departure")
        if self.origin_airport_code == self.destination_airport_code:
            raise ValueError("Origin and destination must differ")
        return self


def run_search(kind: str, payload: dict):
    if not CAPACITY.acquire(blocking=False):
        raise HTTPException(429, "Search capacity is busy; try again shortly")
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "cloud_worker.py"), kind],
            input=json.dumps(payload), capture_output=True, text=True,
            timeout=50, cwd=ROOT,
        )
        if result.returncode:
            raise HTTPException(502, "Flight provider is temporarily unavailable")
        response = json.loads(result.stdout)
        if response.get("error"):
            codes = {"provider_blocked": 502, "search_timeout": 504,
                     "airport_not_found": 404, "provider_unavailable": 502}
            raise HTTPException(codes.get(response["error"], 502), response)
        return response
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Flight search exceeded 50 seconds") from None
    except (ValueError, OSError):
        raise HTTPException(502, "Flight provider returned an invalid response") from None
    finally:
        CAPACITY.release()


@app.get("/healthz")
def health():
    # No provider network requests: a provider failure must not restart the service.
    return {"status": "ok", "service": "westbound-flight-search"}


@app.post("/search/flights", dependencies=[Depends(authenticate)])
def flights(search: FlightSearch):
    return run_search("flights", search.model_dump(mode="json"))


@app.get("/search/airports", dependencies=[Depends(authenticate)])
def airports(query: str = Query(min_length=2, max_length=100)):
    return run_search("airports", {"query": query})
