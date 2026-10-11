"""``http_request`` task type: makes one outbound HTTP call, described
entirely by the task's payload (``method``, ``url``, optional
``headers``/``json``/``timeout``).

SSRF guardrails (all deliberate, not incidental):
  - only ``http``/``https`` URL schemes are accepted
  - the hostname is resolved and every returned address is checked
    against the private/loopback/link-local/reserved/multicast ranges
    before any request is made -- a payload cannot reach
    ``169.254.169.254`` (cloud metadata endpoints), ``127.0.0.1``,
    RFC1918 ranges, etc. Override for local dev/test only via
    ``CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS=1``.
  - redirects are never followed automatically (a redirect could repoint
    an already-validated URL at a private address)
  - an explicit, bounded timeout (default 10s, hard cap 30s)
  - the response body is streamed and capped (1MB) rather than buffered
    in full, so a huge/slow response can't exhaust worker memory or hang
    indefinitely

**Known limitation, documented rather than silently assumed away**: the
hostname is resolved and checked once, before the request; the actual
outbound request resolves DNS again (via httpx/the OS resolver). A
DNS-rebinding attacker who controls the target hostname could in
principle return a public IP for the check and a private one moments
later for the real connection (a time-of-check/time-of-use gap). Closing
this fully would mean resolving once and connecting directly to the
pinned IP while still sending the original Host header -- a larger change
than this task type's current scope. Not fixed here; flagged so it isn't
mistaken for a complete SSRF defense.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

import httpx

from db.models import TaskRecord
from worker.tasks.errors import InvalidTaskPayloadError, SsrfBlockedError

logger = logging.getLogger(__name__)

HTTP_REQUEST_TASK_TYPE = "http_request"

_ALLOWED_SCHEMES = {"http", "https"}
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_TIMEOUT_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 1_000_000


def _allow_private_hosts() -> bool:
    return os.environ.get("CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS") == "1"


def _reject_if_private(hostname: str) -> None:
    if _allow_private_hosts():
        return
    try:
        resolved = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise InvalidTaskPayloadError(f"could not resolve host {hostname!r}: {exc}") from exc

    for info in resolved:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise SsrfBlockedError(
                f"http_request refuses to contact {ip} (resolved from host "
                f"{hostname!r}) -- private/internal/loopback addresses are "
                "blocked; set CHRONOS_HTTP_TASK_ALLOW_PRIVATE_HOSTS=1 to "
                "override for local dev/test"
            )


def http_request_task(record: TaskRecord) -> None:
    payload = record.payload or {}

    method = payload.get("method", "GET")
    if not isinstance(method, str) or not method:
        raise InvalidTaskPayloadError("'method' must be a non-empty string")

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise InvalidTaskPayloadError("http_request task requires a non-empty 'url' string")

    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise InvalidTaskPayloadError(
            f"unsupported URL scheme {parsed.scheme!r}; only http/https are allowed"
        )
    if not parsed.hostname:
        raise InvalidTaskPayloadError("URL has no hostname")

    _reject_if_private(parsed.hostname)

    timeout = payload.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise InvalidTaskPayloadError("'timeout' must be a positive number")
    timeout = min(float(timeout), _MAX_TIMEOUT_SECONDS)

    headers = payload.get("headers")
    if headers is not None and not isinstance(headers, dict):
        raise InvalidTaskPayloadError("'headers' must be an object")

    json_body = payload.get("json")

    with httpx.stream(
        method,
        url,
        headers=headers,
        json=json_body,
        timeout=timeout,
        follow_redirects=False,
    ) as response:
        total_bytes = 0
        for chunk in response.iter_bytes():
            total_bytes += len(chunk)
            if total_bytes > _MAX_RESPONSE_BYTES:
                raise InvalidTaskPayloadError(
                    f"response body exceeds the maximum allowed size ({_MAX_RESPONSE_BYTES} bytes)"
                )
        response.raise_for_status()

    logger.info(
        "http_request_task: task_id=%s method=%s url=%s status=%s",
        record.id,
        method,
        url,
        response.status_code,
    )
