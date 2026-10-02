"""Thin client for the Mapillary Graph API (v4).

Only the two calls the downloader needs are implemented:

* ``search_images`` - ``GET /images?bbox=...`` (max. 2000 results per request)
* ``get_image``     - ``GET /{image_id}?fields=...`` (used to refresh expired URLs)

Transient failures (HTTP 429, 5xx, network errors) are retried with
exponential backoff.
"""

from __future__ import annotations

import time
from typing import Callable, Iterable, Sequence

import requests

GRAPH_URL = "https://graph.mapillary.com"

# The API returns at most this many images per search request.
SEARCH_LIMIT = 2000

THUMB_FIELDS = {
    "256": "thumb_256_url",
    "1024": "thumb_1024_url",
    "2048": "thumb_2048_url",
    "original": "thumb_original_url",
}

METADATA_FIELDS = [
    "id",
    "captured_at",
    "compass_angle",
    "computed_compass_angle",
    "geometry",
    "computed_geometry",
    "altitude",
    "computed_altitude",
    "camera_type",
    "make",
    "model",
    "width",
    "height",
    "sequence",
    "creator",
]


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, timeout: bool = False):
        super().__init__(message)
        self.status = status
        self.timeout = timeout


class MapillaryClient:
    def __init__(
        self,
        token: str,
        base_url: str = GRAPH_URL,
        session: requests.Session | None = None,
        timeout: float = 60,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"OAuth {token}"
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep

    def _get_json(self, url: str, params: dict, retries: int | None = None) -> dict:
        retries = self.max_retries if retries is None else retries
        attempt = 0
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.Timeout as exc:
                error = ApiError(f"Request timed out: {exc}", timeout=True)
            except requests.RequestException as exc:
                error = ApiError(f"Network error: {exc}")
            else:
                if resp.status_code == 200:
                    data = _json_or_none(resp)
                    if isinstance(data, dict):
                        return data
                    # Truncated or garbled body (e.g. a proxy error page) - retry.
                    error = ApiError("Invalid JSON response from Mapillary", resp.status_code)
                elif resp.status_code in (401, 403):
                    raise ApiError(
                        "Mapillary rejected the access token "
                        f"(HTTP {resp.status_code}): {_error_message(resp)}",
                        resp.status_code,
                    )
                else:
                    error = ApiError(_error_message(resp), resp.status_code)
                    if resp.status_code != 429 and resp.status_code < 500:
                        raise error
            attempt += 1
            if attempt > retries:
                raise error
            self.sleep(min(2**attempt, 60))

    def search_images(
        self,
        bbox: Sequence[float],
        fields: Iterable[str],
        start_captured_at: str | None = None,
        end_captured_at: str | None = None,
        limit: int = SEARCH_LIMIT,
        retries: int | None = None,
    ) -> list[dict]:
        """Return images inside ``bbox`` = (west, south, east, north)."""
        params = {
            "bbox": ",".join(f"{v:.7f}" for v in bbox),
            "fields": ",".join(fields),
            "limit": limit,
        }
        if start_captured_at:
            params["start_captured_at"] = start_captured_at
        if end_captured_at:
            params["end_captured_at"] = end_captured_at
        data = self._get_json(f"{self.base_url}/images", params, retries)
        return data.get("data", [])

    def get_image(self, image_id: str, fields: Iterable[str]) -> dict:
        return self._get_json(
            f"{self.base_url}/{image_id}", {"fields": ",".join(fields)}
        )


def _json_or_none(resp: requests.Response):
    try:
        return resp.json()
    except ValueError:
        return None


def _error_message(resp: requests.Response) -> str:
    body = _json_or_none(resp)
    if body is None:
        return resp.text[:200] or resp.reason or f"HTTP {resp.status_code}"
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)
