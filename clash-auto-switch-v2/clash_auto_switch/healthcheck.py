"""
Node health-check helpers.

- Single-URL delay probing
- Multi-URL / multi-sample comprehensive scoring
- Direct-network reachability check
"""

from __future__ import annotations

import concurrent.futures
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

from .clash_api import Api
from .config import Config


SCORE_CACHE_TTL = 300


@dataclass(frozen=True)
class Candidate:
    name: str
    provider: str
    region: Any = None
    parent_group: str | None = None


@dataclass(frozen=True)
class Score:
    candidate: Candidate
    average: int
    maximum: int
    samples: tuple[int, ...]


def direct_network_online(config: Config) -> bool:
    """Check whether the host can reach the internet *without* a proxy."""
    req = urllib.request.Request(
        config.direct_check_url,
        headers={"User-Agent": "clash-auto-switch/2.0"},
    )
    handlers = [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=config.direct_check_timeout) as resp:
            resp.read(128)
            return 200 <= resp.status < 500
    except Exception:
        return False


def _single_delay(
    api: Api,
    name: str,
    test_url: str,
    timeout_ms: int,
) -> int | None:
    """Return delay in ms for *name* → *test_url*, or None on failure."""
    return api.proxy_delay(name, test_url, timeout_ms)


def proxy_score(
    api: Api,
    name: str,
    test_urls: list[str],
    timeout_ms: int,
    samples: int,
) -> tuple[int, int, tuple[int, ...]] | None:
    """Run *samples* rounds of parallel multi-URL testing.

    Returns (average_ms, max_ms, tuple_of_all_delays) or None when any
    round fails completely.
    """
    delays: list[int] = []
    for idx in range(max(1, samples)):
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(test_urls) or 1
        ) as executor:
            futures = [
                executor.submit(_single_delay, api, name, url, timeout_ms)
                for url in test_urls
            ]
            round_delays: list[int] = []
            for future in futures:
                delay = future.result()
                if delay is None:
                    return None
                round_delays.append(delay)

        delays.extend(round_delays)
        if idx + 1 < samples:
            time.sleep(0.2)

    average = round(sum(delays) / len(delays))
    return average, max(delays), tuple(delays)


def cached_score(
    cache: dict[str, tuple[float, int, int]],
    candidate: Candidate,
    invalidated_at: float,
    hard_threshold: int,
) -> Score | None:
    """Return a previously computed Score if it is still fresh."""
    entry = cache.get(candidate.name)
    if not entry:
        return None
    tested_at, average, maximum = entry
    now = time.monotonic()
    if tested_at < invalidated_at:
        return None
    if now - tested_at > SCORE_CACHE_TTL:
        return None
    if average > hard_threshold:
        return None
    return Score(
        candidate=candidate,
        average=average,
        maximum=maximum,
        samples=(average, maximum),
    )


def remember_score(
    cache: dict[str, tuple[float, int, int]],
    name: str,
    average: int,
    maximum: int,
) -> None:
    cache[name] = (time.monotonic(), average, maximum)
