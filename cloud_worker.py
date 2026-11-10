"""One isolated search; killed by the HTTP parent when its time budget expires."""
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "skyscanner"))
from skyscanner import SkyScanner
from skyscanner.errors import BannedWithCaptcha, AttemptsExhaustedIncompleteResponse
from skyscanner.types import CabinClass


def search(kind, payload):
    client = SkyScanner(locale="en-US", currency="USD", market="US", max_retries=12)

    # Preserve provider failures explicitly; upstream assumes every 403 has redirect_to.
    for method in ("get", "post"):
        original = getattr(client.session, method)

        def checked(*args, _original=original, **kwargs):
            kwargs.setdefault("timeout", 12)
            response = _original(*args, **kwargs)
            if response.status_code in (401, 403, 429):
                raise BannedWithCaptcha("https://www.skyscanner.net")
            if response.status_code >= 400:
                raise RuntimeError("Provider HTTP error")
            return response

        setattr(client.session, method, checked)

    departure = dt.datetime.fromisoformat(payload["depart_date"]).replace(hour=10) if kind == "flights" else dt.datetime.now() + dt.timedelta(days=30)
    returning = dt.datetime.fromisoformat(payload["return_date"]).replace(hour=10) if payload.get("return_date") else None

    def airports(query):
        return client.search_airports(query, depart_date=departure,
                                      return_date=returning or departure + dt.timedelta(days=7))

    if kind == "airports":
        return {"airports": [{"name": a.title, "code": a.skyId}
                             for a in airports(payload["query"])[:10]]}
    origin = next((a for a in airports(payload["origin_airport_code"])
                   if a.skyId == payload["origin_airport_code"]), None)
    destination = next((a for a in airports(payload["destination_airport_code"])
                        if a.skyId == payload["destination_airport_code"]), None)
    if not origin or not destination:
        return {"error": "airport_not_found", "message": "Confirm both airport codes"}
    response = client.get_flight_prices(
        origin, destination, departure, returning,
        cabinClass=CabinClass(payload["cabin_class"]), adults=payload["adults"],
    )
    buckets = response.json.get("itineraries", {}).get("buckets", [])
    # Preserve provider labels and fields rather than invent a fare/total interpretation.
    compact = []
    for bucket in buckets:
        item = {key: value for key, value in bucket.items() if key != "items"}
        item["items"] = bucket.get("items", [])[:3]
        compact.append(item)
    return {"currency": "USD", "searched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "query": payload, "buckets": compact,
            "notice": "Compare the provider's cheapest bucket. Prices are quotes, not booking confirmations."}


if __name__ == "__main__":
    try:
        result = search(sys.argv[1], json.load(sys.stdin))
    except BannedWithCaptcha:
        result = {"error": "provider_blocked", "message": "Skyscanner declined this search. No live fare is available."}
    except AttemptsExhaustedIncompleteResponse:
        result = {"error": "search_timeout", "message": "The provider did not finish searching in time."}
    except Exception:
        result = {"error": "provider_unavailable", "message": "Live flight search is temporarily unavailable."}
    print(json.dumps(result))
