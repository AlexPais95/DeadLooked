"""Situational buys: does a counter item still help when a teammate already has one like it?

    python -m deadlock_review.situational

For every purchase of a counter item (anti-heal, shred, resist, silence, disarm, stun groups from
counters.item_groups) in the collected top-tier matches, checks whether a teammate already owned an
item from the same group, and scores the purchase like winprob.item_value: did the buyer's team win
more often than its chance of winning at the moment of purchase? Split into "first on the team" and
"a teammate already had one", per item and per group.

Resist only protects its owner, so for resist groups "already had one" means the buyer themselves.

Writes baselines/situational_value.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import Api
from .counters import item_groups
from .winprob import load, win_prob_state

OUT = Path("baselines/situational_value.json")
PERSONAL = {"spirit_resist", "bullet_resist"}
MIN_N = 40


def _num(item, prop):
    v = (item.get("properties") or {}).get(prop)
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError):
        return 0.0


def _heal_per_min(pl, t):
    """Healing per minute so far at time t (interpolated from the stats snapshots)."""
    pts = [(0, 0)] + [(x[0], x[1]) for x in pl.get("st", [])]
    prev = pts[0]
    val = pts[-1][1]
    for cur in pts[1:]:
        if cur[0] >= t:
            val = prev[1] + (cur[1] - prev[1]) * (t - prev[0]) / ((cur[0] - prev[0]) or 1)
            break
        prev = cur
    return val / max(1.0, t / 60)


def _heal_thresholds(lines) -> dict:
    """80th percentile of players' healing per minute, per 5-minute window."""
    by: dict = {}
    for line in lines[:8000]:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        for pl in m["p"]:
            for x in pl.get("st", []):
                if x[0] > 0:
                    by.setdefault(min(9, int(x[0] // 300)), []).append(x[1] / (x[0] / 60))
    return {b: sorted(v)[int(0.8 * (len(v) - 1))] for b, v in by.items() if len(v) > 50}


def _need(group, m, team, t, heal_p80, resist):
    """Did the enemy team call for this answer at time t? None = no definition for this group."""
    enemies = [pl for pl in m["p"] if pl["t"] != team]
    if group == "anti_heal":
        thr = heal_p80.get(min(9, int(t // 300)))
        return None if thr is None else any(_heal_per_min(pl, t) >= thr for pl in enemies)
    if group in ("spirit_resist", "bullet_resist"):
        k = [sum(pl.get("k", [0, 0, 0])[i] for pl in enemies) for i in range(3)]
        tot = sum(k)
        if not tot:
            return None
        return (k[0] / tot >= 0.6) if group == "spirit_resist" else ((k[1] + k[2]) / tot >= 0.6)
    if group in ("spirit_shred", "bullet_shred"):
        idx = 0 if group == "spirit_shred" else 1
        stacked = sum(1 for pl in enemies if sum(resist.get(iid, (0, 0))[idx] for bt, iid in pl["i"] if bt <= t) >= 25)
        return stacked >= 2
    return None


def build(cache: str = ".cache", store_name: str = "winprob_matches_v4.jsonl") -> dict:
    api = Api(cache, verbose=False)
    groups = item_groups(api.items())
    group_of: dict[int, list[str]] = {}
    for g, items in groups.items():
        for i in items:
            group_of.setdefault(i["id"], []).append(g)
    model = (load() or {}).get("model")
    resist = {i["id"]: (_num(i, "TechResist"), _num(i, "BulletResist")) for i in api.items()}
    lines = (Path(cache) / store_name).read_text().splitlines()
    heal_p80 = _heal_thresholds(lines)
    acc: dict = {}
    n_matches = 0
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if "st" not in m["p"][0]:
            continue          # matches stored before healing was kept
        n_matches += 1
        # all counter-item purchases in this match: (time, team, player index, group)
        owned = []
        for pi, pl in enumerate(m["p"]):
            for t, iid in pl["i"]:
                for g in group_of.get(iid, []):
                    owned.append((t, pl["t"], pi, g))
        for pi, pl in enumerate(m["p"]):
            won = 1.0 if m["win"] == pl["t"] else 0.0
            for t, iid in pl["i"]:
                gs = group_of.get(iid)
                if not gs or t < 60:
                    continue
                exp = win_prob_state(model, m, pl["t"], t)
                for g in gs:
                    if g in PERSONAL:
                        had = any(o[0] < t and o[2] == pi and o[3] == g for o in owned)
                    else:
                        had = any(o[0] < t and o[1] == pl["t"] and o[2] != pi and o[3] == g for o in owned)
                    key = "dup" if had else "first"
                    need = _need(g, m, pl["t"], t, heal_p80, resist)
                    keys = [key] + ([f"{key}:{'need' if need else 'noneed'}"] if need is not None else [])
                    for kk in keys:
                      for k in (f"{g}|{iid}|{kk}", f"{g}|*|{kk}", f"{g}|{iid}|{kk}|{pl['h']}"):
                        a = acc.setdefault(k, [0, 0.0, 0.0])
                        a[0] += 1
                        a[1] += exp
                        a[2] += won
    out: dict = {"matches": n_matches, "groups": {}}
    for k, (n, exp, won) in acc.items():
        if n < MIN_N:
            continue
        parts = k.split("|")
        g, iid, key = parts[:3]
        p = won / n
        if len(parts) == 4:      # per hero
            out.setdefault("by_hero", {}).setdefault(parts[3], {}).setdefault(g, {}).setdefault(iid, {})[key] = {
                "n": n, "above": (won - exp) / n, "margin": 1.96 * (max(0.01, p * (1 - p)) / n) ** 0.5}
            continue
        out["groups"].setdefault(g, {}).setdefault(iid, {})[key] = {
            "n": n, "expected": exp / n, "actual": p, "above": (won - exp) / n,
            "margin": 1.96 * (max(0.01, p * (1 - p)) / n) ** 0.5}
    OUT.write_text(json.dumps(out))
    return out


_CACHE = None


PRIOR_N = 1000   # hero data counts as much as all-hero data once the hero has this many purchases


def value(group: str, item_id: int | str, first: bool, hero_id: int | None = None, need: bool | None = None):
    """(wins above expected, margin, purchases by your hero, share of the number from your hero).

    Blends your hero's purchases with all heroes' (empirical Bayes): weight = n / (n + 1000).
    So 237 Haze purchases count for 19%, 6,602 for 87%: an item can work very differently per hero
    (Silencer as the team's first silence: Haze +2.2, Wraith +3.2, all heroes +0.2), but a few hundred
    games are too noisy to stand alone."""
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(OUT.read_text()) if OUT.exists() else {}
    key = "first" if first else "dup"
    if need is not None:
        tagged = f"{key}:{'need' if need else 'noneed'}"
        if ((_CACHE.get("groups", {}).get(group, {}) or {}).get(str(item_id), {}) or {}).get(tagged):
            key = tagged            # measured only in games where the enemy called for it
    base = ((_CACHE.get("groups", {}).get(group, {}) or {}).get(str(item_id), {}) or {}).get(key)
    hero = None
    if hero_id is not None:
        hero = (((_CACHE.get("by_hero", {}).get(str(hero_id), {}) or {}).get(group, {}) or {}).get(str(item_id), {}) or {}).get(key)
    if not base and not hero:
        return None
    if not hero:
        return base["above"], base["margin"], 0, 0.0
    if not base:
        return hero["above"], hero["margin"], hero["n"], 1.0
    w = hero["n"] / (hero["n"] + PRIOR_N)
    est = w * hero["above"] + (1 - w) * base["above"]
    margin = ((w * hero["margin"]) ** 2 + ((1 - w) * base["margin"]) ** 2) ** 0.5 + (1 - w) * abs(hero["above"] - base["above"]) * 0.5
    return est, margin, hero["n"], w


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review.situational")
    ap.add_argument("--cache", default=".cache")
    a = ap.parse_args(argv)
    out = build(a.cache)
    names = {i["id"]: i["name"] for i in Api(a.cache, verbose=False).items()}
    fmt = lambda r: f"{100 * r['above']:+5.1f} ±{100 * r['margin']:.1f} ({r['n']:,})" if r else "        -        "
    print(f"{out['matches']:,} matches. Wins per 100 above expected (± 95% margin; purchases).")
    print("'needed' = the enemy called for it at the time of purchase (heavy healer, spirit/gun-heavy team, resist stacked)")
    for g, items in out["groups"].items():
        allr = items.get("*", {})
        print(f"\n{g}:  first & needed {fmt(allr.get('first:need'))} | first & not needed {fmt(allr.get('first:noneed'))} | "
              f"already covered & needed {fmt(allr.get('dup:need'))}")
        for iid, r in sorted(items.items(), key=lambda kv: -(kv[1].get("first") or {}).get("n", 0))[:8]:
            if iid == "*":
                continue
            print(f"   {names.get(int(iid), iid):22} first&needed {fmt(r.get('first:need'))} | first&not {fmt(r.get('first:noneed'))} | "
                  f"covered&needed {fmt(r.get('dup:need'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
