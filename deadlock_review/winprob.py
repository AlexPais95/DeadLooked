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
STORE = "winprob_matches_v4.jsonl"
TIER_BY_NAME = {"Tier1": "g", "Tier2": "w", "BarrackBoss": "b", "TitanShieldGenerator": "s", "Titan": "p"}  # longest prefix first
TIER_BY_ID = {**{i: "g" for i in (1, 2, 3, 4)}, **{i: "w" for i in (5, 6, 7, 8)},
              **{i: "b" for i in (12, 13, 14, 15)}, 10: "s", 11: "s", 9: "p"}   # 9 Titan = Patron's first phase; 0 Core ends the game
TIERS = ("g", "w", "b", "s", "p")   # Guardian, Walker, Base Guardian, Shrine, Patron (first phase)


def _team(v) -> int:
    return v if isinstance(v, int) else (0 if v == "Team0" else 1)


def _tier_name(name: str) -> str | None:
    for prefix, tier in TIER_BY_NAME.items():
        if name.startswith(prefix):
            return tier
    return None


def _compact(m: dict) -> dict:
    return {
        "id": m["match_id"], "win": _team(m["winning_team"]), "dur": m["duration_s"],
        "o": [[o["destroyed_time_s"], _team(o["team"]), _tier_name(o["team_objective"])]
              for o in m.get("objectives") or [] if o.get("destroyed_time_s") and _tier_name(o["team_objective"])],
        "p": [{"t": _team(pl["team"]), "h": pl["hero_id"],
               "i": [[it["game_time_s"], it["item_id"]] for it in pl.get("items") or [] if it.get("game_time_s", 0) > 0],
               "nw": [[s["time_stamp_s"], s["net_worth"]] for s in pl.get("stats") or []],
               # v4: per snapshot [time, healing done, spirit power, gun power]; last snapshot's kill types
               "st": [[s["time_stamp_s"], s.get("player_healing") or 0, s.get("tech_power") or 0, s.get("weapon_power") or 0]
                      for s in pl.get("stats") or []],
               "k": [((pl.get("stats") or [{}])[-1]).get(k) or 0 for k in ("ability_kills", "bullet_kills", "melee_kills")],
               "d": [[d["game_time_s"], d.get("death_duration_s") or 0] for d in pl.get("death_details") or []]}
              for pl in m["players"]],
    }


def collect(api: Api, n: int, days: int, min_badge: int, store: Path) -> list[dict]:
    have = {}
    if store.exists():
        for line in store.read_text().splitlines():
            if line:
                m = json.loads(line)
                have[m["id"]] = m
    max_id = (min(have) - 1) if have else None    # resume below what we already have
    t0 = int(time.time()) - days * DAY
    with store.open("a") as fh:
        while len(have) < n:
            params = dict(min_average_badge=min_badge, game_mode="normal", min_unix_timestamp=t0, min_duration_s=900,
                          include_info="true", include_player_items="true", include_player_stats="true",
                          include_objectives="true", include_player_death_details="true",
                          order_by="match_id", order_direction="desc", limit=200)
            if max_id:
                params["max_match_id"] = max_id
            try:
                r = api.session.get(BASE + "/v1/matches/metadata", params=params, timeout=300)
            except Exception as e:      # connection reset, proxy hiccup: wait and retry the same page
                print(f"[winprob] network error ({type(e).__name__}), retrying in 20 s", flush=True)
                time.sleep(20)
                continue
            if r.status_code in (429, 502, 503, 504):
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


FEATURES = ("lead", "g", "w", "b", "s", "p", "alive")


def features(lead_k: float, objectives, team: int, t: float, alive_diff: int) -> list[float]:
    """[1, soul lead (k), Guardians/Walkers/Base Guardians/Shrines taken minus lost, heroes alive minus theirs]."""
    obj = {k: 0 for k in TIERS}
    for ot, owner, tier in objectives:
        if ot <= t and tier in obj:
            obj[tier] += 1 if owner != team else -1
    return [1.0, lead_k, obj["g"], obj["w"], obj["b"], obj["s"], obj["p"], float(alive_diff)]


def _alive_diff(m: dict, team: int, t: float) -> int:
    dead = {0: 0, 1: 0}
    for pl in m["p"]:
        if any(dt <= t < dt + dd for dt, dd in pl.get("d", [])):
            dead[pl["t"]] += 1
    return dead[1 - team] - dead[team]


def state(m: dict, team: int, t: float) -> list[float]:
    return features(lead(m, team, t), m.get("o", []), team, t, _alive_diff(m, team, t))


TERMS = ("1", "t", "lead", "lead*t", "lead*t2", "g", "w", "b", "s", "p", "alive", "alive*t")


def _terms(t: float, x: list[float]) -> list[float]:
    """One model for the whole game: objectives have one value each; the value of souls and of
    heroes alive changes smoothly with time (t in units of 30 minutes)."""
    tn = t / 1800
    _, ld, g, w, b, s, pt, al = x
    return [1.0, tn, ld, ld * tn, ld * tn * tn, g, w, b, s, pt, al, al * tn]


def fit(matches: list[dict], step: int | None = None, ridge: float = 1.0) -> dict:
    """Logistic regression on game states sampled every 20 s (both teams of every match).
    Checked on held-out matches: Brier 0.176 (soul lead alone 0.181, coin flip 0.250)."""
    step = step or max(20, 20 * (len(matches) // 5000))   # ~1M samples whatever the number of matches
    X, y = [], []
    for m in matches:
        for t in range(60, m["dur"], step):
            for team in (0, 1):
                X.append(_terms(t, state(m, team, t)))
                y.append(1.0 if m["win"] == team else 0.0)
    X, y = np.array(X, dtype=np.float64), np.array(y)
    beta = np.zeros(X.shape[1])
    for _ in range(40):  # Newton / IRLS with a little ridge
        pr = 1 / (1 + np.exp(-X @ beta))
        g = X.T @ (y - pr) - ridge * beta
        H = X.T @ (X * (pr * (1 - pr))[:, None]) + ridge * np.eye(X.shape[1])
        step_ = np.linalg.solve(H, g)
        beta += step_
        if np.abs(step_).max() < 1e-9:
            break
    return {"pooled": [float(v) for v in beta], "terms": list(TERMS)}


def win_prob_x(model: dict, t: float, x: list[float]) -> float:
    beta = model.get("pooled")
    if not beta:
        return 0.5
    z = sum(b * v for b, v in zip(beta, _terms(t, x)))
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def win_prob_state(model: dict, m: dict, team: int, t: float) -> float:
    return win_prob_x(model, t, state(m, team, t))


# ------------------------------------------------------------------ item value
def item_value(matches: list[dict], model: dict) -> dict:
    acc: dict = {}
    for m in matches:
        for pl in m["p"]:
            won = 1.0 if m["win"] == pl["t"] else 0.0
            for t, iid in pl["i"]:
                if t < 60:
                    continue
                exp = win_prob_state(model, m, pl["t"], t)
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


def death_swing(matches: list[dict], model: dict) -> dict:
    """Average change in a team's win chance from 2 s before one of its heroes dies to 45 s after,
    by game minute (5-minute buckets). Includes what deaths typically lead to (souls, objectives,
    more deaths), so it is the usual cost of a death, not just the empty slot."""
    acc: dict = {}
    for m in matches:
        for pl in m["p"]:
            for dt, _ in pl.get("d", []):
                if dt < 60 or dt + 45 > m["dur"]:
                    continue
                before = win_prob_state(model, m, pl["t"], dt - 2)
                after = win_prob_state(model, m, pl["t"], dt + 45)
                k = str(min(9, int(dt // 300)))
                a = acc.setdefault(k, [0, 0.0])
                a[0] += 1
                a[1] += after - before
    return {k: v[1] / v[0] for k, v in acc.items() if v[0] >= 50}


def build(api: Api, n: int, days: int, min_badge: int) -> dict:
    store = Path(api.cache) / STORE   # v4: + healing, spirit/gun power per snapshot, kill types
    matches = collect(api, n, days, min_badge, store)
    model = fit(matches)
    value = item_value(matches, model)
    model["deaths"] = death_swing(matches, model)
    out = {"generated": int(time.time()), "matches": len(matches), "min_badge": min_badge, "days": days,
           "features": list(FEATURES), "model": model, **value}
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
