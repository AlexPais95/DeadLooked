"""Per-player match metrics: the numbers v2 compares against top-tier baselines.

Every metric is defined so it can be computed the same way for the reviewed player
and for top-tier players, and is normalised (per 10 minutes, shares, medians) so games
of different lengths compare fairly.
"""

from __future__ import annotations

import statistics

from .analysis import (ISOLATED_M, _killer_ult, _present_seconds, find_fights, fight_damage,
                       hero_efficiency, ult_windows)
from .heroes import cfg
from .match import Match, Player, dist_m

# name -> (label, higher_is_better)
METRICS = {
    "deaths_per10": ("Deaths per 10 min", False),
    "isolated_death_share": ("Deaths with no ally within 30 m", False),
    "fight_presence": ("Team fights attended (when alive)", True),
    "fight_damage_ratio": ("Damage in fights vs expected (median)", True),
    "ult_per10": ("Ultimates per 10 min (after unlock)", True),
    "ult_enemies_caught": ("Enemies caught per ultimate", True),
    "ult_allies_close": ("Allies within 15 m per ultimate", True),
    "ult_idle_s": ("Seconds ultimate sat ready before use (median)", False),
    "ally_distance_m": ("Distance to nearest ally (median, m)", False),
    "souls_per_min_10": ("Souls per minute at 10:00", True),
    "souls_per_min_20": ("Souls per minute at 20:00", True),
    "lane_deaths": ("Deaths before 10:00", False),
}


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def player_metrics(m: Match, p: Player, fights=None, damage_rows=None) -> dict:
    fights = fights if fights is not None else find_fights(m)
    minutes = max(1.0, m.duration / 60)
    out: dict[str, float | None] = {k: None for k in METRICS}

    out["deaths_per10"] = len(p.deaths) / minutes * 10
    out["lane_deaths"] = float(sum(1 for d in p.deaths if d.t < 600))
    for t, key in ((600, "souls_per_min_10"), (1200, "souls_per_min_20")):
        if m.duration > t:
            out[key] = m.networth_at(p, t) / (t / 60)

    if not m.replay_ok:
        return out

    # deaths alone (not counting deaths during an enemy ultimate that moves you)
    judged = isolated = 0
    for d in p.deaths:
        killer = m.players.get(d.killer_slot)
        if _killer_ult(m, killer, d.t):
            continue
        allies, _ = m.near(p, d.t - 3, ISOLATED_M)
        judged += 1
        isolated += 0 if allies else 1
    out["isolated_death_share"] = isolated / judged if judged else None

    # fights
    alive_fights = [f for f in fights if p.alive_at(f.deaths[0][0] - 5) and p.alive_at(f.end)]
    if alive_fights:
        out["fight_presence"] = sum(1 for f in alive_fights if _present_seconds(m, p, f) > 0) / len(alive_fights)
    if damage_rows is not None:
        out["fight_damage_ratio"] = _median([r.ratio for r in damage_rows if r.player.slot == p.slot])

    # ultimate
    ult, casts, ready = ult_windows(m, p)
    unlock = next((t for t, cn in p.ability_ups if cn == ult), None) if ult else None
    if casts and unlock is not None:
        out["ult_per10"] = len(casts) / max(1.0, (m.duration - unlock) / 60) * 10
        radius = cfg(p.hero)["ult_radius_m"]
        caught, close = [], []
        for t in casts:
            allies, enemies = m.near(p, t + 1, max(radius, 15))
            caught.append(sum(1 for e in enemies if e[1] <= radius))
            close.append(sum(1 for a in allies if a[1] <= 15))
        out["ult_enemies_caught"] = statistics.mean(caught)
        out["ult_allies_close"] = statistics.mean(close)
        idle = [c - a for (a, c) in ready if c in casts]
        out["ult_idle_s"] = _median(idle)

    # how close to teammates (sampled every 5 s while alive, after the laning phase)
    dists = []
    for t in range(600, m.duration, 5):
        me = p.at(t, tol=1)
        if not me or not me[3] or not p.alive_at(t):
            continue
        others = [q.at(t, tol=1) for q in m.team(p.team) if q.slot != p.slot and q.alive_at(t)]
        ds = [dist_m(me, o) for o in others if o and o[3]]
        if ds:
            dists.append(min(ds))
    out["ally_distance_m"] = _median(dists)
    return out


def match_metrics(m: Match) -> list[dict]:
    """Metrics for all 12 players of a match, ready to store as baseline rows."""
    fights = find_fights(m)
    rows = []
    damage_rows = None
    if m.replay_ok and fights:
        damage_rows, _ = fight_damage(m, fights, hero_efficiency(m))
    for p in m.players.values():
        rows.append({
            "match_id": m.match_id,
            "hero_id": p.hero_id,
            "hero": p.hero,
            "won": p.team == m.winning_team,
            "duration_s": m.duration,
            "replay": m.replay_ok,
            **player_metrics(m, p, fights, damage_rows),
        })
    return rows


def compare_to_baseline(m: Match, p: Player, summary: dict, fights=None, damage_rows=None) -> list[dict]:
    """Your metrics next to top-tier players of the same hero.

    `better_than` = share of top-tier games you did better than on that metric
    (direction-aware: fewer deaths is better, more fight damage is better)."""
    base = (summary.get("heroes") or {}).get(str(p.hero_id))
    if not base:
        return []
    mine = player_metrics(m, p, fights, damage_rows)
    out = []
    for key, (label, higher_better) in METRICS.items():
        b = base["metrics"].get(key)
        v = mine.get(key)
        if not b or v is None:
            continue
        vals = b["values"]
        below = sum(1 for x in vals if x < v)
        equal = sum(1 for x in vals if x == v)
        pct = (below + equal / 2) / len(vals)
        better_than = pct if higher_better else 1 - pct
        out.append({"key": key, "label": label, "you": v, "p25": b["p25"], "p50": b["p50"], "p75": b["p75"],
                    "n": b["n"], "better_than": better_than})
    return out
