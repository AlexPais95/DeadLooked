"""Rotations: moving between lanes to gank, joining fights quickly, and showing up when
objectives are attacked (to defend them or to take them).

The map has three lanes running along y at x = -7500 / 0 / +7500 (see match.lane_of).
Objective deaths come from the replay's BossKilledEvent, with their positions.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .match import LANES, Match, Player, dist_m, fmt_t, lane_of

LANING_END = 600          # laning phase ends around 10:00
OWN_LANE_WINDOW = (150, 420)
GANK_NEAR_M = 25          # counts as in on a kill if this close to the victim
ARRIVED_M = 40            # "at the fight" / "at the objective"
OBJECTIVE_LOOKBACK_S = 15
LANE_HOLD_S = 20          # a lane counts as visited after this long in it (filters passing through)
FAR_M = 100
BASE_KINDS = {"Base Guardian", "Shrine", "Patron"}
OBJECTIVE_IMPACT = {"Guardian": 1.0, "Walker": 2.0, "Base Guardian": 2.0, "Shrine": 2.5, "Patron": 0.0}


@dataclass
class ObjectiveVisit:
    t: float
    kind: str
    lost: bool            # True = your team's objective fell, False = your team took it
    lane: str
    alive: bool
    closest_m: float | None   # closest you got in the last 15 s
    busy: bool            # fighting an enemy hero at the time (within 40 m)
    allies_there: int

    @property
    def there(self) -> bool:
        return self.closest_m is not None and self.closest_m <= ARRIVED_M


def own_lane(p: Player) -> int | None:
    xs = [p.track[s][0] for s in range(*OWN_LANE_WINDOW) if s in p.track and p.track[s][3]]
    return lane_of(statistics.median(xs)) if xs else None


def _closest(p: Player, pos, t0: float, t1: float) -> float | None:
    ds = []
    for s in range(int(t0), int(t1) + 1):
        me = p.at(s, tol=0)
        if me and me[3] and p.alive_at(s):
            ds.append(dist_m(me, pos))
    return min(ds) if ds else None


def lane_ganks(m: Match, p: Player) -> list[float]:
    """Kills before 10:00 in another lane that you were in on (killer, or within 25 m)."""
    home = own_lane(p)
    out = []
    if home is None:
        return out
    for q in m.players.values():
        if q.team == p.team:
            continue
        for d in q.deaths:
            if d.t >= LANING_END or not d.pos or lane_of(d.pos[0]) == home:
                continue
            me = p.at(d.t, tol=1)
            close = me and me[3] and dist_m(me, d.pos) <= GANK_NEAR_M
            if d.killer_slot == p.slot or close:
                out.append(d.t)
    return out


def lane_changes(p: Player, start: int, end: int) -> int:
    """Times you moved to a different lane and stayed there 20 s+."""
    current, run_lane, run = None, None, 0
    changes = 0
    for s in range(start, end):
        pt = p.track.get(s)
        if not pt or not pt[3] or not p.alive_at(s):
            run = 0
            continue
        ln = lane_of(pt[0])
        run = run + 1 if ln == run_lane else 1
        run_lane = ln
        if run >= LANE_HOLD_S and ln != current:
            if current is not None:
                changes += 1
            current = ln
    return changes


def fight_arrivals(m: Match, p: Player, fights) -> list[float]:
    """Seconds after a fight's first death before you got within 40 m of it
    (fights you were alive for and reached; 0 = already there)."""
    out = []
    for f in fights:
        t0 = f.deaths[0][0]
        if not p.alive_at(t0):
            continue
        spots = [pos for (_, pos) in _death_spots(m, f)]
        if not spots:
            continue
        for s in range(int(t0) - 15, int(f.end) + 1):
            me = p.at(s, tol=0)
            if me and me[3] and min(dist_m(me, x) for x in spots) <= ARRIVED_M:
                out.append(max(0.0, s - t0))
                break
    return out


def _death_spots(m: Match, f):
    for q in m.players.values():
        for d in q.deaths:
            if f.deaths[0][0] - 1 <= d.t <= f.end + 1 and d.pos:
                yield d.t, d.pos


def objective_visits(m: Match, p: Player) -> list[ObjectiveVisit]:
    out = []
    for o in m.objective_kills:
        alive = p.alive_at(o.t)
        closest = _closest(p, o.pos, o.t - OBJECTIVE_LOOKBACK_S, o.t)
        _, enemies = m.near(p, o.t, ARRIVED_M) if alive else ([], [])
        allies = sum(1 for q in m.team(p.team) if q.slot != p.slot
                     and (_closest(q, o.pos, o.t - OBJECTIVE_LOOKBACK_S, o.t) or 1e9) <= ARRIVED_M)
        where = "base" if o.kind in BASE_KINDS else LANES[lane_of(o.pos[0])]
        out.append(ObjectiveVisit(o.t, o.kind, o.team == p.team, where, alive,
                                  closest, bool(enemies), allies))
    return out


def rotation_metrics(m: Match, p: Player, fights) -> dict:
    out = {}
    home = own_lane(p)
    lane_secs = [s for s in range(OWN_LANE_WINDOW[0], LANING_END)
                 if s in p.track and p.track[s][3] and p.alive_at(s)]
    if home is not None and lane_secs:
        out["lane_ganks"] = float(len(lane_ganks(m, p)))
        out["lane_time_away"] = sum(1 for s in lane_secs if lane_of(p.track[s][0]) != home) / len(lane_secs)
    alive_min = sum(1 for s in range(LANING_END, m.duration) if p.alive_at(s)) / 60
    if alive_min >= 5:
        out["lane_changes_per10"] = lane_changes(p, LANING_END, m.duration) / alive_min * 10
    arr = fight_arrivals(m, p, fights)
    out["fight_arrival_s"] = statistics.median(arr) if arr else None
    visits = [v for v in objective_visits(m, p) if v.alive and v.kind != "Patron"]
    lost = [v for v in visits if v.lost]
    taken = [v for v in visits if not v.lost]
    out["objectives_defended"] = sum(v.there for v in lost) / len(lost) if lost else None
    out["objectives_taken_present"] = sum(v.there for v in taken) / len(taken) if taken else None
    return out


def rotation_findings(m: Match, p: Player):
    """Objectives your team lost while you were alive, far away and not fighting anyone."""
    from .analysis import Finding  # local import: analysis imports this module's users
    found = []
    for v in objective_visits(m, p):
        if not (v.lost and v.alive and not v.busy and (v.closest_m or 1e9) > FAR_M):
            continue
        impact = OBJECTIVE_IMPACT.get(v.kind, 1.0)
        if not impact:
            continue
        found.append(Finding(p.slot, p.team, v.t, "missed_defense", impact,
                             f"{fmt_t(v.t)}: {v.kind} ({v.lane}) fell while you were alive and not fighting",
                             f"you were {v.closest_m:.0f} m away; {v.allies_there} teammate(s) defended it"))
    return found


# ---------------------------------------------------------------- being where you're needed
NEED_STEP_S = 5
SKIRMISH_M = 30           # allies and enemies this close, with damage being dealt = a fight
PRESSURE_M = 30           # 2+ enemies this close to one of your objectives = it's under attack
AT_NEED_M = 60
FARMING_FAR_M = 100


def objective_spots(m: Match) -> list[tuple[int, str, tuple[float, float], float]]:
    """(owner team, kind, position, time destroyed or inf) for every objective. The map is
    point-symmetric, so objectives that never died are filled in by mirroring the other side's."""
    spots = {}
    for o in m.objective_kills:
        spots[(o.team, round(o.pos[0]), round(o.pos[1]))] = (o.team, o.kind, o.pos, o.t)
    for (team, x, y), (_, kind, pos, _) in list(spots.items()):
        key = (1 - team, -x, -y)
        if not any(abs(k[1] - key[1]) < 300 and abs(k[2] - key[2]) < 300 and k[0] == key[0] for k in spots):
            spots[key] = (1 - team, kind, (-pos[0], -pos[1]), float("inf"))
    return list(spots.values())


@dataclass
class Need:
    t: int
    kind: str                     # "fight" or "objective"
    pos: tuple[float, float]
    what: str


def needs_at(m: Match, team: int, t: int, spots) -> list[Need]:
    """Places your team needed help at time t."""
    out = []
    pos = {q.slot: q.at(t, tol=1) for q in m.players.values()}
    alive = {s for s, v in pos.items() if v and v[3] and m.players[s].alive_at(t)}
    allies = [m.players[s] for s in alive if m.players[s].team == team]
    enemies = [m.players[s] for s in alive if m.players[s].team != team]
    for a in allies:
        close = [e for e in enemies if dist_m(pos[a.slot], pos[e.slot]) <= SKIRMISH_M]
        active = any(q.damage_out.get(s) for q in [a, *close] for s in range(t - 2, t + 1))
        if close and active:
            out.append(Need(t, "fight", pos[a.slot][:2], f"{a.hero} fighting {len(close)} enemy hero(es)"))
    for owner, kind, opos, dead_t in spots:
        if owner != team or t >= dead_t or kind == "Patron":
            continue
        n = sum(1 for e in enemies if dist_m(pos[e.slot], opos) <= PRESSURE_M)
        if n >= 2:
            out.append(Need(t, "objective", opos, f"{n} enemies at your {kind}"))
    return out


def presence_when_needed(m: Match, p: Player):
    """Per sampled moment where someone else on your team needed help:
    'there' (within 60 m), 'busy' (fighting elsewhere) or 'farming' (100 m+ away, no enemy near)."""
    spots = objective_spots(m)
    samples = []
    for t in range(LANING_END, m.duration, NEED_STEP_S):
        me = p.at(t, tol=1)
        if not me or not me[3] or not p.alive_at(t):
            continue
        needs = needs_at(m, p.team, t, spots)
        needs = [n for n in needs if not n.what.startswith(p.hero + " ")]
        if not needs:
            continue
        d = min(dist_m(me, n.pos) for n in needs)
        _, enemies = m.near(p, t, 40)
        state = "there" if d <= AT_NEED_M else ("busy" if enemies else ("farming" if d >= FARMING_FAR_M else "near"))
        samples.append((t, state, d, min(needs, key=lambda n: dist_m(me, n.pos))))
    return samples


def needed_metrics(m: Match, p: Player) -> dict:
    s = presence_when_needed(m, p)
    if len(s) < 6:
        return {}
    return {"needed_present": sum(1 for x in s if x[1] == "there") / len(s),
            "needed_farming": sum(1 for x in s if x[1] == "farming") / len(s)}


def needed_findings(m: Match, p: Player):
    """Stretches where teammates needed you and you were farming far away, and it cost something
    (a teammate died there, or the objective fell)."""
    from .analysis import Finding
    samples = presence_when_needed(m, p)
    episodes, cur = [], []
    for x in samples:
        if x[1] == "farming" and (not cur or x[0] - cur[-1][0] <= NEED_STEP_S * 2):
            cur.append(x)
        else:
            if cur:
                episodes.append(cur)
            cur = [x] if x[1] == "farming" else []
    if cur:
        episodes.append(cur)
    found = []
    for ep in episodes:
        t0, t1 = ep[0][0], ep[-1][0] + NEED_STEP_S
        spots = [x[3].pos for x in ep]
        dead = [q.hero for q in m.team(p.team) if q.slot != p.slot
                for d in q.deaths if t0 <= d.t <= t1 + 5 and d.pos and min(dist_m(d.pos, s) for s in spots) <= AT_NEED_M]
        lost = [o.kind for o in m.objective_kills if o.team == p.team and t0 <= o.t <= t1 + 10
                and min(dist_m(o.pos, s) for s in spots) <= AT_NEED_M]
        if not dead and not lost:
            continue
        cost = ", ".join([f"{h} died" for h in dead] + [f"lost the {k}" for k in lost])
        dist = statistics.median(x[2] for x in ep)
        found.append(Finding(p.slot, p.team, t0, "not_where_needed", 1.0 + 0.6 * len(dead) + 0.8 * len(lost),
                             f"{fmt_t(t0)}–{fmt_t(t1)}: your team needed you and you were farming {dist:.0f} m away",
                             f"{ep[0][3].what}; result: {cost}"))
    return found
