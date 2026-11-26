"""Win Probability Added for the events of one match.

Uses the model from winprob.py (soul lead, objectives by tier and heroes alive, fitted on recent
top-tier matches) to compute a team's chance of winning at any second of the reviewed match, then
scores events by how much that chance moved:

  Every swing is measured once per episode (deaths and objectives less than 20 s apart) and split
  between the events in it in proportion to their typical value (see Credits), so nothing is counted
  twice. Then:
  death        that death's credit
  objective    that objective's credit
  multi-kill   the credits of the deaths you got
  ultimate     the credits of the deaths, within 15 s, of the heroes it caught and of you and your
               nearby teammates
  missed fight the fight's swing, shared between the teammates who weren't there
  fight damage no win-chance claim of its own (shown as context)

Findings without a clean window (e.g. "3 deaths before 10:00") get an estimate: their old impact
score converted with this match's ratio between impact scores and measured swings.
"""

from __future__ import annotations

import math
import statistics

from .match import Match, fmt_t
from .winprob import TIER_BY_ID, features, load, win_prob_x

DEATH_AFTER_S = 45


class MatchWP:
    def __init__(self, m: Match, model: dict):
        self.m, self.model = m, model
        self.hero_model = _hero_model()
        self.objectives = [(o["destroyed_time_s"], o["team"], TIER_BY_ID.get(o.get("team_objective_id")))
                           for o in m.meta.get("objectives", []) if o.get("destroyed_time_s")]
        self.objectives = [o for o in self.objectives if o[2]]
        self.deaths = [(d.t, d.duration, q.team) for q in m.players.values() for d in q.deaths]

    def alive_diff(self, team: int, t: float) -> int:
        dead = {0: 0, 1: 0}
        for dt, dur, tm in self.deaths:
            if dt <= t < dt + dur:
                dead[tm] += 1
        return dead[1 - team] - dead[team]

    def wp(self, team: int, t: float) -> float:
        t = max(0.0, min(float(self.m.duration), t))
        lead = (self.m.team_networth(team, int(t)) - self.m.team_networth(1 - team, int(t))) / 1000
        x = features(lead, self.objectives, team, t, self.alive_diff(team, t))
        if self.hero_model:
            return self._wp_heroes(team, t, x)
        return win_prob_x(self.model, t, x)

    def _wp_heroes(self, team: int, t: float, x) -> float:
        """Same game state, but souls valued per hero and each hero's strength over time
        (hero_value.py, fitted on 40,000 top-tier matches)."""
        from .winprob import _terms
        hm = self.hero_model
        tn = t / 1800
        full = _terms(t, x)
        row = [full[i] for i in hm["base_keep"]]
        col = {h: i for i, h in enumerate(hm["hero_ids"])}
        other = 0.0
        extra = [0.0] * (4 * len(hm["hero_ids"]))
        for q in self.m.players.values():
            sign = 1 if q.team == team else -1
            v = sign * self.m.networth_at(q, t) / 1000
            i = col.get(q.hero_id)
            if i is None:
                other += v
                continue
            extra[4 * i] += sign
            extra[4 * i + 1] += sign * tn
            extra[4 * i + 2] += v
            extra[4 * i + 3] += v * tn
        z = sum(b * v for b, v in zip(hm["coef"], row + [other, other * tn] + extra))
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))

    def swing(self, team: int, t0: float, t1: float) -> float:
        return self.wp(team, t1) - self.wp(team, t0)

    def curve(self, team: int, step: int = 10) -> list[tuple[int, float]]:
        return [(t, self.wp(team, t)) for t in range(0, self.m.duration + 1, step)]


def _hero_model() -> dict | None:
    import json
    from pathlib import Path
    p = Path("baselines/hero_soul_value.json")
    if not p.exists():
        return None
    return (json.loads(p.read_text()) or {}).get("model")


def load_model() -> dict | None:
    v = load()
    return v.get("model") if v and (v.get("model") or {}).get("pooled") else None


CLUSTER_GAP_S = 20     # events closer than this share one swing
BEFORE_S, AFTER_S = 5, 15


class Credits:
    """Splits every swing in win chance between the events that caused it, so nothing is counted twice.

    Events are deaths and objectives. Events less than 20 s apart form one episode (a fight, or a death
    and the objective that fell right after). For each episode the real swing is measured once (team 0's
    win chance from 5 s before the first event to 15 s after the last). Each event first gets its typical
    value at that minute (deaths: what a death usually costs then, learned from the top-tier matches;
    objectives: the model's value of that objective at the current win chance). The difference between
    the real swing and the sum of typical values is shared out in proportion to their size, so the
    credits add up exactly to the real swing."""

    def __init__(self, w: MatchWP):
        self.w, m = w, w.m
        model = w.model
        deaths_typ = {int(k): v for k, v in (model.get("deaths") or {}).items()}
        beta = dict(zip(model.get("terms", []), model.get("pooled", [])))
        events = []   # (t, key, typical value for team 0)
        for q in m.players.values():
            for d in q.deaths:
                typ = abs(deaths_typ.get(min(9, int(d.t // 300)), -0.02))
                events.append((d.t, ("death", q.slot, d.t), typ if q.team == 1 else -typ))
        for ot, owner, tier in w.objectives:
            pr = w.wp(0, ot - 1)
            typ = pr * (1 - pr) * beta.get(tier, 0.1)
            events.append((ot, ("obj", owner, ot, tier), typ if owner == 1 else -typ))
        events.sort(key=lambda e: e[0])
        self.credit, self.episode = {}, {}
        i = 0
        while i < len(events):
            j = i
            while j + 1 < len(events) and events[j + 1][0] - events[j][0] <= CLUSTER_GAP_S:
                j += 1
            group = events[i:j + 1]
            t0, t1 = group[0][0] - BEFORE_S, group[-1][0] + AFTER_S
            swing = w.wp(0, t1) - w.wp(0, t0)
            typical = sum(e[2] for e in group)
            size = sum(abs(e[2]) for e in group) or 1
            for e in group:
                self.credit[e[1]] = e[2] + (swing - typical) * abs(e[2]) / size
                self.episode[e[1]] = (t0, t1, swing, len(group))
            i = j + 1

    def for_team(self, key, team: int) -> float:
        v = self.credit.get(key, 0.0)
        return v if team == 0 else -v

    def death(self, slot: int, t: float):
        return next((k for k in self.credit if k[0] == "death" and k[1] == slot and abs(k[2] - t) <= 2), None)

    def objective(self, t: float, owner: int | None = None):
        return next((k for k in self.credit if k[0] == "obj" and abs(k[2] - t) <= 3
                     and (owner is None or k[1] == owner)), None)

    def context(self, key, team: int) -> str:
        t0, t1, swing, n = self.episode[key]
        sw = swing if team == 0 else -swing
        if n == 1:
            return f"win chance {100 * sw:+.1f}% from just before to {AFTER_S}s after"
        return f"its share of a {100 * sw:+.1f}% swing ({fmt_t(t0 + BEFORE_S)}–{fmt_t(t1 - AFTER_S)}, {n} deaths/objectives)"


def apply(m: Match, fights, findings, focus_slots) -> MatchWP | None:
    """Set f.wpa (change in the player's team's win chance, -1..1) and f.wpa_how on findings."""
    model = load_model()
    if not model:
        return None
    w = MatchWP(m, model)
    c = Credits(w)
    names = {q.hero: q for q in m.players.values()}

    def fight_at(t):
        return next((f for f in fights if f.deaths[0][0] - 5 <= t <= f.end + 5), None)

    measured = []
    claimed: dict = {}     # event key -> title of the finding that already counts it
    order = {"death": 0, "isolated_death": 0, "outnumbered_death": 0, "objective_taken": 1, "missed_defense": 1,
             "good_ult": 2, "ult_no_followup": 2, "ult_weak": 2, "multi_kill": 3}

    def take(keys, f):
        """Claim events for this finding's team (each side counts an event once: an enemy's death can be
        their mistake and your play, but not two of your plays)."""
        free = [k for k in keys if (k, f.team) not in claimed]
        for k in free:
            claimed[(k, f.team)] = f.title
        shared = sorted({claimed[(k, f.team)] for k in keys if k not in free})
        return free, shared

    for f in sorted(findings, key=lambda f: order.get(f.kind, 9)):
        if f.slot is None:
            continue
        p = m.players.get(f.slot)
        team, kind, t = p.team, f.kind, f.t
        val, how = None, ""
        if kind in ("death", "isolated_death", "outnumbered_death"):
            key = c.death(p.slot, t)
            if key:
                take([key], f)
                val, how = c.for_team(key, team), "your death: " + c.context(key, team)
        elif kind == "multi_kill":
            victims = [(q, d) for q in m.players.values() if q.team != team
                       for d in q.deaths if d.killer_slot == p.slot and t - 1 <= d.t <= t + 15]
            keys = [k for k in (c.death(q.slot, d.t) for q, d in victims) if k]
            share = {c.death(q.slot, d.t): m.damage_share(p.slot, q.slot, d.t)[0] for q, d in victims}
            if keys:
                free, shared = take(keys, f)
                # a kill is worth your share of the damage it took, not the last hit
                val = sum(c.for_team(k, team) * share.get(k, 1.0) for k in free)
                how = ("credited by your share of the damage on each victim ("
                       + ", ".join(f"{q.hero} {m.damage_share(p.slot, q.slot, d.t)[0]:.0%}" for q, d in victims)
                       + "); " + c.context(keys[0], team)
                       + (f"; {len(keys) - len(free)} of the kills already count for: {'; '.join(shared)}" if shared else ""))
        elif kind in ("good_ult", "ult_no_followup", "ult_weak"):
            involved = {names[h].slot for h, *_ in f.people if h in names} | {p.slot}
            keys = [c.death(q.slot, d.t) for q in m.players.values() if q.slot in involved
                    for d in q.deaths if t <= d.t <= t + 15]
            keys = [k for k in keys if k]
            free, shared = take(keys, f)
            if free:
                val = sum(c.for_team(k, team) for k in free)
                how = (f"deaths in the 15s after of the heroes it caught and of your nearby teammates: their share of the swing"
                       + (f" (your own death counts on its own card)" if shared else ""))
            elif kind == "good_ult":
                val, how = 0.0, "nobody died in the 15s after"
        elif kind in ("objective_taken", "missed_defense"):
            key = c.objective(t, owner=team if kind == "missed_defense" else 1 - team)
            if key:
                take([key], f)
                val, how = c.for_team(key, team), "the objective: " + c.context(key, team)
        elif kind == "absent_fight":
            fight = fight_at(t)
            if fight:
                sw = w.swing(team, fight.deaths[0][0] - 5, fight.end + 10)
                missing = max(1, sum(1 for q in m.team(team) if not _was_in(m, q, fight)))
                val = min(0.0, sw) / missing
                how = f"the fight swung your team's win chance {100 * sw:+.1f}%, shared between the {missing} who weren't there"
        elif kind in ("high_fight_damage", "low_fight_damage"):
            fight = fight_at(t)
            if fight:
                sw = w.swing(team, fight.deaths[0][0] - 5, fight.end + 10)
                f.wpa_how = f"context: the fight swung your team's win chance {100 * sw:+.1f}%"
                f.context_only = True
        if val is not None:
            f.wpa, f.wpa_how = val, how
            if f.impact and abs(val) > 0.001:
                measured.append(abs(val) / f.impact)
    # the rest: convert the impact score with this match's measured ratio
    k = statistics.median(measured) if measured else 0.01
    for f in findings:
        if getattr(f, "wpa", None) is None and f.slot is not None:
            sign = 1 if f.good else -1
            ctx = f.wpa_how
            f.wpa = sign * f.impact * k
            f.wpa_how = ("estimated from the impact score; " + ctx) if ctx else \
                        "estimated from the impact score (no clean before/after window)"
    w.credits = c
    return w


def _was_in(m: Match, q, fight) -> bool:
    from .analysis import _present_seconds
    return _present_seconds(m, q, fight) > 0 or any(x[1].slot == q.slot for x in fight.deaths)


def pct(v: float) -> str:
    return f"{100 * v:+.1f}%"
