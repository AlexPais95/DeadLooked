"""Win Probability Added for the events of one match.

Uses the model from winprob.py (soul lead, objectives by tier and heroes alive, fitted on recent
top-tier matches) to compute a team's chance of winning at any second of the reviewed match, then
scores events by how much that chance moved:

  death        win chance from 2 s before to 45 s after (souls, objectives and further deaths that
               followed), shared between all of your team's deaths in that window
  objective    win chance from 2 s before it fell to 10 s after
  fight        from 5 s before the first death to 10 s after the last
  ultimate     from 1 s before the cast to 15 s after
  multi-kill   from 2 s before the first kill to 20 s after the last
  missed fight the fight's swing, shared between the teammates who weren't there

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
        return win_prob_x(self.model, t, features(lead, self.objectives, team, t, self.alive_diff(team, t)))

    def swing(self, team: int, t0: float, t1: float) -> float:
        return self.wp(team, t1) - self.wp(team, t0)

    def curve(self, team: int, step: int = 10) -> list[tuple[int, float]]:
        return [(t, self.wp(team, t)) for t in range(0, self.m.duration + 1, step)]


def load_model() -> dict | None:
    v = load()
    return v.get("model") if v and (v.get("model") or {}).get("pooled") else None


def apply(m: Match, fights, findings, focus_slots) -> MatchWP | None:
    """Set f.wpa (change in the player's team's win chance, -1..1) and f.wpa_how on findings."""
    model = load_model()
    if not model:
        return None
    w = MatchWP(m, model)
    deaths_at = sorted((d.t, q.team) for q in m.players.values() for d in q.deaths)

    def fight_at(t):
        return next((f for f in fights if f.deaths[0][0] - 5 <= t <= f.end + 5), None)

    measured = []
    for f in findings:
        if f.slot is None:
            continue
        p = m.players.get(f.slot)
        team, kind, t = p.team, f.kind, f.t
        val, how = None, ""
        if kind in ("death", "isolated_death", "outnumbered_death"):
            own = [x for x, tm in deaths_at if tm == team and t - 2 <= x <= t + DEATH_AFTER_S]
            val = w.swing(team, t - 2, t + DEATH_AFTER_S) / max(1, len(own))
            how = (f"your team's win chance moved {100 * w.swing(team, t - 2, t + DEATH_AFTER_S):+.1f}% in the {DEATH_AFTER_S}s after"
                   + (f", shared between {len(own)} deaths" if len(own) > 1 else ""))
        elif kind in ("good_ult", "ult_no_followup", "ult_weak"):
            val = w.swing(team, t - 1, t + 15)
            how = "win chance from the cast to 15s after"
        elif kind == "multi_kill":
            val = w.swing(team, t - 2, t + 20)
            how = "win chance from the first kill to 20s after"
        elif kind in ("objective_taken", "missed_defense"):
            val = w.swing(team, t - 2, t + 10)
            how = "win chance from just before the objective fell to 10s after"
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
                val = sw / 6
                how = f"the fight swung your team's win chance {100 * sw:+.1f}% (your sixth of it)"
        if val is not None:
            f.wpa, f.wpa_how = val, how
            if f.impact:
                measured.append(abs(val) / f.impact)
    # the rest: convert the impact score with this match's measured ratio
    k = statistics.median(measured) if measured else 0.01
    for f in findings:
        if getattr(f, "wpa", None) is None and f.slot is not None:
            sign = 1 if f.good else -1
            f.wpa, f.wpa_how = sign * f.impact * k, "estimated from the impact score (no clean before/after window)"
    return w


def _was_in(m: Match, q, fight) -> bool:
    from .analysis import _present_seconds
    return _present_seconds(m, q, fight) > 0 or any(x[1].slot == q.slot for x in fight.deaths)


def pct(v: float) -> str:
    return f"{100 * v:+.1f}%"
