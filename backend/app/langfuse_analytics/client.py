"""Read-only client for Langfuse's public REST API: GET /api/public/traces and /api/public/observations.

Basic auth with the project's public/secret key (the same keys the app traces with). Pages through results, retries
rate limits (429) and server errors with backoff, and stops after `max_pages` per call (`truncated` is then True).
"""

import datetime as dt
import logging
import time
from collections.abc import Callable, Iterator

import httpx

log = logging.getLogger(__name__)


class LangfuseAPIError(RuntimeError):
    pass


def iso(value: dt.datetime) -> str:
    """UTC ISO 8601 with a Z suffix, the form every Langfuse version accepts."""
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _retry_after(response: httpx.Response, attempt: int) -> float:
    try:
        return min(float(response.headers.get("Retry-After", "")), 60.0)
    except ValueError:
        return float(min(2**attempt, 30))


class LangfuseReader:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        *,
        timeout: float = 30.0,
        retries: int = 4,
        page_size: int = 100,
        max_pages: int = 500,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.retries, self.page_size, self.max_pages, self._sleep = retries, page_size, max_pages, sleep
        self.truncated = False  # True when a listing stopped at max_pages
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(public_key, secret_key),
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, params: dict) -> dict:
        response = None
        for attempt in range(self.retries + 1):
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt == self.retries:
                    raise LangfuseAPIError(f"Langfuse is unreachable ({type(exc).__name__}: {exc})") from exc
                self._sleep(float(min(2**attempt, 30)))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    self._sleep(_retry_after(response, attempt))
                    continue
                break
            if response.status_code in (401, 403):
                raise LangfuseAPIError(
                    "Langfuse rejected the API keys (check LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL)"
                )
            if response.status_code >= 400:
                raise LangfuseAPIError(f"Langfuse {path} returned HTTP {response.status_code}: {response.text[:300]}")
            try:
                return response.json()
            except ValueError as exc:
                raise LangfuseAPIError(f"Langfuse {path} did not return JSON (is LANGFUSE_BASE_URL right?)") from exc
        raise LangfuseAPIError(f"Langfuse {path} kept answering HTTP {response.status_code}; try again later")

    def paginate(self, path: str, params: dict) -> Iterator[dict]:
        page = 1
        while True:
            if page > self.max_pages:
                self.truncated = True
                log.warning(
                    "Langfuse analytics: %s stopped after %d pages (ANALYTICS_SYNC_MAX_PAGES)", path, self.max_pages
                )
                return
            body = self._get(path, {**params, "page": page, "limit": self.page_size})
            data = body.get("data") or []
            yield from data
            total_pages = (body.get("meta") or {}).get("totalPages")
            if (
                not data
                or (total_pages is not None and page >= total_pages)
                or (total_pages is None and len(data) < self.page_size)
            ):
                return
            page += 1

    def traces(
        self, since: dt.datetime, until: dt.datetime, tag: str | None = None, environment: str | None = None
    ) -> Iterator[dict]:
        params = {"fromTimestamp": iso(since), "toTimestamp": iso(until), "orderBy": "timestamp.asc"}
        if tag:
            params["tags"] = tag
        if environment:
            params["environment"] = environment
        return self.paginate("/api/public/traces", params)

    def observations(self, since: dt.datetime, until: dt.datetime, environment: str | None = None) -> Iterator[dict]:
        params = {"fromStartTime": iso(since), "toStartTime": iso(until)}
        if environment:
            params["environment"] = environment
        return self.paginate("/api/public/observations", params)
