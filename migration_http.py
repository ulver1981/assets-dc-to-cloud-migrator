"""Shared retrying HTTP client helpers for Data Center and Cloud API calls."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests


RETRYABLE_SERVER_STATUS = 500


def redact_url(url: str) -> str:
    """Return a request URL without embedded credentials or query values."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    auth: Any = None,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
    retries: int = 3,
    success_statuses: set[int] | None = None,
) -> Any:
    """Request JSON with bounded 429/5xx retry and contextual final failures."""
    for attempt in range(1, retries + 1):
        response = session.request(
            method,
            url,
            auth=auth,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout,
        )
        if response.status_code == 429 and attempt < retries:
            try:
                delay = int(response.headers.get("Retry-After", "10"))
            except ValueError:
                delay = 10
            time.sleep(delay)
            continue
        if response.status_code >= RETRYABLE_SERVER_STATUS and attempt < retries:
            time.sleep(2 * attempt)
            continue

        if success_statuses is not None:
            if response.status_code not in success_statuses:
                raise requests.HTTPError(_error_message(method, url, response),
                                         response=response)
        else:
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                raise requests.HTTPError(_error_message(method, url, response),
                                         response=response) from exc

        if response.status_code == 204:
            return None
        return response.json()

    raise RuntimeError(f"{method} {redact_url(url)} failed after {retries} retries")


def _error_message(method: str, url: str, response: requests.Response) -> str:
    """Keep request context without exposing response data or URL credentials."""
    return (f"{method} {redact_url(url)} failed with {response.status_code} "
            f"{response.reason}")
