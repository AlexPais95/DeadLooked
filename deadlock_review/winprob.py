"""Win probability model and item value above expectation.

    python -m deadlock_review.winprob --matches 5000

1. Collects recent top-tier matches (bulk metadata: winner, each player's items with purchase
   times, and their soul totals every few minutes). No replay queries.
2. Fits a win-probability model: chance a team wins given the game time and its soul lead
   (logistic regression per 5-minute window).
3. Scores every item purchase: did the buyer's team win more often than its chance of winning
   at the moment of purchase? Averaged per item (and per item and hero), this is the item's
   win rate above expected. It answers the problem with raw win rates: counter items are
   bought by teams that are behind (who were going to lose more often anyway) and skipped by
   teams that are winning.

Writes baselines/item_value.json (summary) and keeps the compact matches in .cache/.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np

from .api import DAY, Api

BASE = "https://api.deadlock-api.com"
OUT = Path("baselines/item_value.json")
WINDOW_S = 300          # one model per 5-minute window
MAX_WINDOW = 9          # 45:00+ shares the last window
MIN_PURCHASES = 30


# ------------------------------------------------------------------ collection
def _compact(m: dict) -> dict:
    return {
        "id": m["match_id"], "win": 0 if m["winning_team"] in (0, "Team0") else 1, "dur": m["duration_s"],
        "p": [{"t": pl["team"] if isinstance(pl["team"], int) else (0 if pl["team"] == "Team0" else 1),
               "h": pl["hero_id"],
               "i": [[it["game_time_s"], it["item_id"]] for it in pl.get("items") or [] if it.get("game_time_s", 0) > 0],
               "nw": [[s["time_stamp_s"], s["net_worth"]] for s in pl.get("stats") or []]}
              for pl in m["players"]],
    }


def collect(api: Api, n: int, days: int, min_badge: int, store: Path) -> list[dict]:
    have = {}
    if store.exists():
        for line in store.read_text().splitlines():
            if line:
                m = json.loads(line)
                have[m["id"]] = m
    max_id = None
    t0 = int(time.time()) - days * DAY
    with store.open("a") as fh:
        while len(have) < n:
            params = dict(min_average_badge=min_badge, game_mode="normal", min_unix_timestamp=t0, min_duration_s=900,
                          include_info="true", include_player_items="true", include_player_stats="true",
                          order_by="match_id", order_direction="desc", limit=200)
            if max_id:
                params["max_match_id"] = max_id
            r = api.session.get(BASE + "/v1/matches/metadata", params=params, timeout=300)
            if r.status_code == 429:
                time.sleep(int(r.headers.get("retry-after") or 5))
                continue
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for m in batch:
                if m["match_id"] not in have and m.get("players"):
                    c = _compact(m)
                    have[c["id"]] = c
                    fh.write(json.dumps(c) + "\n")
            max_id = min(m["match_id"] for m in batch) - 1
            print(f"[winprob] {len(have)} matches", flush=True)
    return list(have.values())


# ----------------------------------------------------------------------- model
def _team_souls(m: dict, team: int, t: float) -> float:
    total = 0.0
    for pl in m["p"]:
        if pl["t"] != team:
            continue
        pts = [(0, 0)] + [tuple(x) for x in pl["nw"]]
        prev = pts[0]
        val = pts[-1][1]
        for cur in pts[1:]:
            if cur[0] >= t:
                span = (cur[0] - prev[0]) or 1
                val = prev[1] + (cur[1] - prev[1]) * (t - prev[0]) / span
                break
            prev = cur
        total += val
    return total


def lead(m: dict, team: int, t: float) -> float:
    """The team's soul lead at time t, in thousands."""
    return (_team_souls(m, team, t) - _team_souls(m, 1 - team, t)) / 1000


def _window(t: float) -> int:
    return min(MAX_WINDOW, int(t // WINDOW_S))


def fit(matches: list[dict]) -> dict:
    """Logistic regression per window: P(win) = 1 / (1 + exp(-(a + b * lead_k))).
    Both teams of every match are used (the model is symmetric)."""
    xs = {w: [] for w in range(MAX_WINDOW + 1)}
    for m in matches:
        for t in range(60, m["dur"], 60):
            d = lead(m, 0, t)
            w = _window(t)
            xs[w].append((d, 1.0 if m["win"] == 0 else 0.0))
            xs[w].append((-d, 1.0 if m["win"] == 1 else 0.0))
    model = {}
    for w, rows in xs.items():
        if len(rows) < 200:
            continue
        X = np.array([[1.0, d] for d, _ in rows])
        y = np.array([v for _, v in rows])
        beta = np.zeros(2)
        for _ in range(25):  # Newton / IRLS
            p = 1 / (1 + np.exp(-X @ beta))
            g = X.T @ (y - p)
            H = X.T @ (X * (p * (1 - p))[:, None]) + 1e-6 * np.eye(2)
            step = np.linalg.solve(H, g)
            beta += step
            if np.abs(step).max() < 1e-8:
                break
        model[w] = [float(beta[0]), float(beta[1])]
    return model


def win_prob(model: dict, t: float, lead_k: float) -> float:
    w = _window(t)
    while w not in model and w > 0:
        w -= 1
    a, b = model.get(w, model.get(str(w), [0.0, 0.3]))
    return 1 / (1 + math.exp(-(a + b * lead_k)))


# ------------------------------------------------------------------ item value
def item_value(matches: list[dict], model: dict) -> dict:
    acc: dict = {}
    for m in matches:
        for pl in m["p"]:
            won = 1.0 if m["win"] == pl["t"] else 0.0
            for t, iid in pl["i"]:
                if t < 60:
                    continue
                exp = win_prob(model, t, lead(m, pl["t"], t))
                for key in (str(iid), f"{iid}:{pl['h']}"):
                    a = acc.setdefault(key, [0, 0.0, 0.0])
                    a[0] += 1
                    a[1] += exp
                    a[2] += won
    out = {"items": {}, "by_hero": {}}
    for key, (n, exp, won) in acc.items():
        if n < MIN_PURCHASES:
            continue
        row = {"n": n, "expected": exp / n, "actual": won / n, "above": (won - exp) / n}
        if ":" in key:
            iid, hid = key.split(":")
            out["by_hero"].setdefault(hid, {})[iid] = row
        else:
            out["items"][key] = row
    return out


def build(api: Api, n: int, days: int, min_badge: int) -> dict:
    store = Path(api.cache) / "winprob_matches.jsonl"
    matches = collect(api, n, days, min_badge, store)
    model = fit(matches)
    value = item_value(matches, model)
    out = {"generated": int(time.time()), "matches": len(matches), "min_badge": min_badge, "days": days,
           "model": {str(k): v for k, v in model.items()}, **value}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out))
    print(f"[winprob] {len(matches)} matches, {len(value['items'])} items -> {OUT}")
    return out


def load() -> dict | None:
    return json.loads(OUT.read_text()) if OUT.exists() else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review.winprob")
    ap.add_argument("--matches", type=int, default=5000)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--min-badge", type=int, default=90)
    ap.add_argument("--cache", default=".cache")
    a = ap.parse_args(argv)
    build(Api(a.cache, verbose=False), a.matches, a.days, a.min_badge)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
