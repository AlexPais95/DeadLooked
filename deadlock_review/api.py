"""Thin client for the public Deadlock API (https://api.deadlock-api.com).

Everything is cached on disk so re-running a report doesn't spend the
replay-query rate limit (200 queries/hour per IP) again.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path

import requests
import zstandard

BASE = "https://api.deadlock-api.com"
DAY = 24 * 3600


def _day_floor() -> int:
    """Timestamps rounded to the day so analytics requests stay cacheable."""
    return int(time.time()) // DAY * DAY


class ApiError(RuntimeError):
    pass


class Api:
    def __init__(self, cache_dir: str | Path = ".cache", verbose: bool = True):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "deadlock-review/0.1"
        self.busy_retries = 4      # retries when the API/Steam says it's busy
        self.query_times: list[float] = []  # replay queries actually submitted (for throttling)

    # ------------------------------------------------------------------ utils
    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[api] {msg}", flush=True)

    def _cache_path(self, key: str, suffix: str = ".json") -> Path:
        safe = hashlib.sha1(key.encode()).hexdigest()[:16]
        return self.cache / f"{safe}{suffix}"

    def _get_json(self, path: str, params: dict | None = None, max_age: float | None = None):
        key = path + "?" + json.dumps(params or {}, sort_keys=True)
        cp = self._cache_path(key)
        if cp.exists() and (max_age is None or time.time() - cp.stat().st_mtime < max_age):
            return json.loads(cp.read_text())
        self._log(f"GET {path} {params or ''}")
        for attempt in range(self.busy_retries):
            r = self.session.get(BASE + path, params=params, timeout=180)
            if r.status_code not in (429, 503):
                break
            wait = min(int(r.headers.get("retry-after") or 60) + 5, 1800)
            print(f"[api] {path}: API busy (HTTP {r.status_code}), retrying in {wait / 60:.0f} min", flush=True)
            time.sleep(wait)
        if r.status_code != 200:
            raise ApiError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        cp.write_text(json.dumps(data))
        return data

    # ----------------------------------------------------------------- assets
    def heroes(self) -> list[dict]:
        return self._get_json("/v1/assets/heroes", max_age=7 * DAY)

    def items(self) -> list[dict]:
        return self._get_json("/v1/assets/items", max_age=7 * DAY)

    # ---------------------------------------------------------------- matches
    def metadata(self, match_id: int) -> dict:
        data = self._get_json(f"/v1/matches/{match_id}/metadata")
        return data.get("match_info", data)

    # -------------------------------------------------------------- analytics
    def item_stats(self, hero_id: int, min_badge: int, days: int = 30) -> list[dict]:
        params = {
            "hero_id": hero_id,
            "game_mode": "normal",
            "min_average_badge": min_badge,
            "min_unix_timestamp": _day_floor() - days * DAY,
            "min_matches": 100,
        }
        return self._get_json("/v1/analytics/item-stats", params, max_age=DAY)

    def ability_order_stats(self, hero_id: int, min_badge: int, days: int = 30) -> list[dict]:
        params = {
            "hero_id": hero_id,
            "game_mode": "normal",
            "min_average_badge": min_badge,
            "min_unix_timestamp": _day_floor() - days * DAY,
            "min_matches": 50,
            "min_ability_upgrades": 12,
        }
        return self._get_json("/v1/analytics/ability-order-stats", params, max_age=DAY)

    def hero_stats(self, min_badge: int | None = None, days: int = 30) -> list[dict]:
        params = {"game_mode": "normal", "min_unix_timestamp": _day_floor() - days * DAY}
        if min_badge:
            params["min_average_badge"] = min_badge
        return self._get_json("/v1/analytics/hero-stats", params, max_age=DAY)

    # ----------------------------------------------------------- demo queries
    def has_replay(self, match_id: int) -> bool | None:
        """Whether a replay file is stored for the match, from its salts (does not use the
        replay-query budget). None = unknown (salts not stored)."""
        r = self.session.get(f"{BASE}/v1/matches/{match_id}/salts", params={"disable_steam": "true"}, timeout=60)
        if r.status_code != 200:
            return None
        return bool(r.json().get("replay_salt"))

    def demo_query(self, match_id: int, sql: str, name: str) -> list[dict]:
        """Run SQL against the match replay. Results are cached per (match, name, sql)."""
        cp = self._cache_path(f"demo:{match_id}:{name}:{sql}", ".ndjson")
        if cp.exists():
            return [json.loads(line) for line in cp.read_text().splitlines() if line]

        self._log(f"replay query '{name}' for match {match_id}")
        body = {"match_id": match_id, "query": sql, "format": "ndjson"}
        for attempt in range(4):
            self.query_times.append(time.time())
            r = self.session.post(BASE + "/v1/matches/demo/query", json=body, timeout=60)
            if r.status_code != 429:
                break
            wait = int(r.headers.get("retry-after") or 600)
            print(f"[api] replay rate limit hit (HTTP 429), waiting {wait / 60:.0f} min", flush=True)
            time.sleep(min(wait, 900))
        if r.status_code not in (200, 201, 202):
            raise ApiError(f"replay query '{name}' rejected: HTTP {r.status_code}: {r.text[:400]}")
        job = r.json()["job_id"]

        deadline = time.time() + 300
        while True:
            s = self.session.get(f"{BASE}/v1/matches/demo/query/{job}", timeout=60).json()
            if s["status"] == "done":
                break
            if s["status"] == "failed":
                raise ApiError(f"replay query '{name}' failed: {s.get('error')}")
            if time.time() > deadline:
                raise ApiError(f"replay query '{name}' timed out (job {job})")
            time.sleep(3)

        raw = self.session.get(s["result_url"], timeout=300).content
        text = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)).read().decode()
        cp.write_text(text)
        return [json.loads(line) for line in text.splitlines() if line]
