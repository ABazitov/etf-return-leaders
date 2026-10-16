"""HTTP with per-host rate limiting, retries and exponential backoff (spec §11)."""
from __future__ import annotations

import http.client
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_sleep = time.sleep
_lock = threading.Lock()
_last_call: dict[str, float] = {}


class HttpError(Exception):
    def __init__(self, url: str, status: int | None, message: str):
        super().__init__(f"{status} {message} for {url}")
        self.url = url
        self.status = status


def _wait_turn(host: str, min_interval: float) -> None:
    with _lock:
        now = time.monotonic()
        wait = _last_call.get(host, 0.0) + min_interval - now
        _last_call[host] = max(now, _last_call.get(host, 0.0) + min_interval)
    if wait > 0:
        _sleep(wait)


def fetch(url: str, *, ua: str, data: bytes | None = None, headers: dict | None = None,
          min_interval: float = 0.2, attempts: int = 5, timeout: float = 60) -> bytes:
    host = urlsplit(url).netloc
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            _sleep(2 ** (attempt - 1))
        _wait_turn(host, min_interval)
        req = urllib.request.Request(url, data=data, headers={"User-Agent": ua, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                last = HttpError(url, e.code, e.reason)
                continue
            raise HttpError(url, e.code, str(e.reason)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, OSError) as e:
            # also errors while reading the body: a truncated response (IncompleteRead), TLS or socket errors
            last = HttpError(url, None, f"{type(e).__name__}: {e}")
            continue
    raise last  # type: ignore[misc]
