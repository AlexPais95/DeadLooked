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


def build(cache: str = ".cache", store_name: str = "winprob_matches_v3.jsonl") -> dict:
    api = Api(cache, verbose=False)
    groups = item_groups(api.items())
    group_of: dict[int, list[str]] = {}
    for g, items in groups.items():
        for i in items:
            group_of.setdefault(i["id"], []).append(g)
    model = (load() or {}).get("model")
    acc: dict = {}
    n_matches = 0
    for line in (Path(cache) / store_name).read_text().splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
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
                    for k in (f"{g}|{iid}|{key}", f"{g}|*|{key}", f"{g}|{iid}|{key}|{pl['h']}"):
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


MIN_HERO_N = 800


def value(group: str, item_id: int | str, first: bool, hero_id: int | None = None):
    """(wins above expected, margin, n, for this hero?) for this item in this situation, or None.
    Uses purchases by your hero when there are enough (an item can work very differently per hero:
    Silencer on Haze is +2.7 as the team's first crowd control, about 0 averaged over all heroes)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(OUT.read_text()) if OUT.exists() else {}
    key = "first" if first else "dup"
    if hero_id is not None:
        row = (((_CACHE.get("by_hero", {}).get(str(hero_id), {}) or {}).get(group, {}) or {}).get(str(item_id), {}) or {}).get(key)
        if row and row["n"] >= MIN_HERO_N:
            return row["above"], row["margin"], row["n"], True
    row = ((_CACHE.get("groups", {}).get(group, {}) or {}).get(str(item_id), {}) or {}).get(key)
    return (row["above"], row["margin"], row["n"], False) if row else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review.situational")
    ap.add_argument("--cache", default=".cache")
    a = ap.parse_args(argv)
    out = build(a.cache)
    names = {i["id"]: i["name"] for i in Api(a.cache, verbose=False).items()}
    print(f"{out['matches']:,} matches. Wins per 100 above expected (± 95% margin; purchases):")
    for g, items in out["groups"].items():
        allr = items.get("*", {})
        f, d = allr.get("first"), allr.get("dup")
        fmt = lambda r: f"{100 * r['above']:+5.1f} ±{100 * r['margin']:.1f} ({r['n']:,})" if r else "      -      "
        label = "already had one yourself" if g in PERSONAL else "a teammate already had one"
        print(f"\n{g}: first on the team {fmt(f)} | {label} {fmt(d)}")
        for iid, r in sorted(items.items(), key=lambda kv: -(kv[1].get("first") or {}).get("n", 0)):
            if iid == "*":
                continue
            print(f"   {names.get(int(iid), iid):22} first {fmt(r.get('first'))} | already {fmt(r.get('dup'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
