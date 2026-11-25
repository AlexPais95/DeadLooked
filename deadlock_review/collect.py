"""Collect top-tier baselines.

    python -m deadlock_review.collect --heroes Dynamo,Wraith,Ivy --matches 20

Finds recent high-rank matches that include each hero, runs the replay analysis on them
and appends one row per player to baselines/raw.jsonl. Every match gives rows for all
12 heroes, so baselines for other heroes grow too. Already-collected matches are skipped,
and replay queries are throttled to stay under the API's hourly limit.

    python -m deadlock_review.collect --summarize

rebuilds baselines/summary.json (percentiles per hero and metric) from raw.jsonl.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from .api import DAY, Api
from .match import Match
from .metrics import METRICS, match_metrics

BASE_DIR = Path("baselines")
RAW = BASE_DIR / "raw.jsonl"
SUMMARY = BASE_DIR / "summary.json"
QUERIES_PER_MATCH = 5


class Throttle:
    """Keeps replay queries under `per_hour`, counting only queries the API client actually sent
    (a match whose replay is missing costs 1 query, not 5)."""

    def __init__(self, api: Api, per_hour: int):
        self.api, self.per_hour = api, per_hour

    def wait_for(self, n: int) -> None:
        while True:
            now = time.time()
            sent = [t for t in self.api.query_times if now - t < 3600]
            self.api.query_times[:] = sent
            if len(sent) + n <= self.per_hour:
                return
            wait = 3600 - (now - sent[0]) + 1
            print(f"[collect] replay budget used ({len(sent)}/{self.per_hour} this hour), waiting {wait / 60:.0f} min",
                  flush=True)
            time.sleep(min(wait, 600))


def collected_ids() -> set[int]:
    if not RAW.exists():
        return set()
    return {json.loads(line)["match_id"] for line in RAW.read_text().splitlines() if line}


def find_matches(api: Api, hero_id: int, min_badge: int, days: int, limit: int) -> list[int]:
    params = {
        "hero_ids": hero_id, "min_average_badge": min_badge, "game_mode": "normal",
        "min_unix_timestamp": int(time.time()) - days * DAY, "min_duration_s": 1200,
        "order_by": "start_time", "order_direction": "desc", "limit": limit, "include_info": "true",
    }
    r = api.session.get("https://api.deadlock-api.com/v1/matches/metadata", params=params, timeout=180)
    r.raise_for_status()
    return [row["match_id"] for row in r.json()]


def collect(api: Api, heroes: list[str], per_hero: int, min_badge: int, days: int, per_hour: int) -> None:
    BASE_DIR.mkdir(exist_ok=True)
    name_to_id = {h["name"].lower(): h["id"] for h in api.heroes()}
    done = collected_ids()
    throttle = Throttle(api, per_hour)
    for hero in heroes:
        hid = name_to_id.get(hero.lower())
        if hid is None:
            print(f"[collect] unknown hero {hero}")
            continue
        candidates = [mid for mid in find_matches(api, hid, min_badge, days, per_hero * 6) if mid not in done]
        got = 0
        for mid in candidates:
            if got >= per_hero:
                break
            if api.has_replay(mid) is False:
                print(f"[collect] {mid}: no replay stored, skipped (free check)", flush=True)
                continue
            throttle.wait_for(QUERIES_PER_MATCH)
            try:
                m = Match(api, mid)
            except Exception as e:
                print(f"[collect] {mid}: metadata failed ({e})", flush=True)
                continue
            if not m.replay_ok:
                print(f"[collect] {mid}: replay unavailable, skipped ({m.replay_error})", flush=True)
                continue
            rows = match_metrics(m)
            with RAW.open("a") as fh:
                for r in rows:
                    fh.write(json.dumps(r) + "\n")
            done.add(mid)
            got += 1
            print(f"[collect] {hero}: {got}/{per_hero} (match {mid})", flush=True)
    summarize()


def _pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize() -> dict:
    rows = [json.loads(line) for line in RAW.read_text().splitlines() if line] if RAW.exists() else []
    out: dict = {"generated": int(time.time()), "heroes": {}}
    by_hero: dict[str, list[dict]] = {}
    for r in rows:
        by_hero.setdefault(str(r["hero_id"]), []).append(r)
    for hid, rs in by_hero.items():
        h = {"hero": rs[0]["hero"], "samples": len(rs), "metrics": {}}
        for key in METRICS:
            vals = [r[key] for r in rs if r.get(key) is not None]
            if len(vals) < 5:
                continue
            h["metrics"][key] = {"n": len(vals), "values": sorted(vals),
                                 "p25": _pct(vals, .25), "p50": _pct(vals, .5), "p75": _pct(vals, .75)}
        out["heroes"][hid] = h
    BASE_DIR.mkdir(exist_ok=True)
    SUMMARY.write_text(json.dumps(out))
    print(f"[collect] summary: {len(rows)} player rows, {len(by_hero)} heroes -> {SUMMARY}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review.collect")
    ap.add_argument("--heroes", default="Dynamo,Wraith,Ivy")
    ap.add_argument("--matches", type=int, default=20, help="new matches per hero")
    ap.add_argument("--min-badge", type=int, default=90)
    ap.add_argument("--days", type=int, default=7, help="only matches this recent (replays expire)")
    ap.add_argument("--per-hour", type=int, default=150, help="replay queries per hour (API limit is 200)")
    ap.add_argument("--summarize", action="store_true", help="only rebuild summary.json")
    ap.add_argument("--cache", default=".cache")
    a = ap.parse_args(argv)
    if a.summarize:
        summarize()
        return 0
    collect(Api(a.cache), [h.strip() for h in a.heroes.split(",")], a.matches, a.min_badge, a.days, a.per_hour)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
