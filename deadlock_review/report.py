"""Markdown report."""

from __future__ import annotations

from .analysis import Finding, Fight
from .match import Match, fmt_t

TEAM_NAMES = {0: "Hidden King (team 0)", 1: "Archmother (team 1)"}


def _team_label(m: Match, team: int, focus_team: int | None) -> str:
    base = TEAM_NAMES.get(team, f"team {team}")
    if focus_team is None:
        return base
    return f"{base} - {'your team' if team == focus_team else 'enemy'}"


def render(m: Match, fights: list[Fight], findings: list[Finding], focus_slots: set[int],
           extras: dict | None = None, top_n: int = 8) -> str:
    extras = extras or {}
    focus = [m.players[s] for s in focus_slots if s in m.players]
    focus_team = focus[0].team if focus else None
    L: list[str] = []
    w = L.append

    won = focus_team is not None and m.winning_team == focus_team
    w(f"# Match {m.match_id} review")
    w("")
    w(f"Duration **{fmt_t(m.duration)}** · winner: **{_team_label(m, m.winning_team, focus_team)}**"
      + (f" · you played **{', '.join(p.hero for p in focus)}** ({'win' if won else 'loss'})" if focus else ""))
    if not m.replay_ok:
        w("")
        w(f"> Replay data unavailable ({m.replay_error}). Position-based checks were skipped.")
    w("")

    # ----- scoreboard
    w("## Scoreboard")
    w("")
    w("| Team | Hero | K/D/A | Souls |")
    w("|---|---|---|---|")
    for team in (0, 1):
        for p in sorted(m.team(team), key=lambda p: -p.net_worth):
            mark = " ⬅ you" if p.slot in focus_slots else ""
            w(f"| {team} | {p.hero}{mark} | {p.kills}/{p.deaths_n}/{p.assists} | {p.net_worth:,} |")
    w("")

    # ----- soul difference
    ref_team = focus_team if focus_team is not None else 1
    w(f"## Soul difference ({_team_label(m, ref_team, focus_team)} minus the other team)")
    w("")
    w("| Time | Difference |")
    w("|---|---|")
    times = sorted({t for p in m.players.values() for t in p.nw_by_time})
    for t in times:
        diff = m.team_networth(ref_team, t) - m.team_networth(1 - ref_team, t)
        w(f"| {fmt_t(t)} | {diff:+,} |")
    w("")

    # ----- fights
    if fights:
        w("## Team fights")
        w("")
        w("| Time | Deaths (team 0 / team 1) | Winner | Objective taken after? |")
        w("|---|---|---|---|")
        for f in fights:
            win = "even" if f.winner is None else _team_label(m, f.winner, focus_team)
            conv = "—" if f.winner is None else ("yes" if f.converted else "**no**")
            w(f"| {fmt_t(f.start + 10)}–{fmt_t(f.end)} | {f.losses.get(0, 0)} / {f.losses.get(1, 0)} | {win} | {conv} |")
        w("")

    # ----- team findings
    team_f = sorted([f for f in findings if f.slot is None], key=lambda f: f.t)
    if team_f:
        w("## Team-level issues")
        w("")
        for f in team_f:
            who = _team_label(m, f.team, focus_team)
            w(f"- **{fmt_t(f.t)}** ({who}) {f.title}" + (f" — {f.detail}" if f.detail else ""))
        w("")

    # ----- focus players
    for p in focus:
        pf = [f for f in findings if f.slot == p.slot]
        bad = sorted([f for f in pf if not f.good], key=lambda f: -f.impact)
        good = [f for f in pf if f.good]
        w(f"## Your biggest mistakes - {p.hero}")
        w("")
        w("Ranked by estimated impact (higher = cost your team more). "
          "Impact is a rough heuristic until a win-probability model replaces it.")
        w("")
        for i, f in enumerate(bad[:top_n], 1):
            w(f"{i}. **{f.title}** (impact {f.impact:.1f})" + (f"  \n   {f.detail}" if f.detail else ""))
        if len(bad) > top_n:
            w(f"\n_{len(bad) - top_n} smaller issues not shown._")
        if good:
            w("")
            w("**What went well:**")
            for f in good:
                w(f"- {f.title} — {f.detail}")
        ult_info = extras.get("ult", {}).get(p.slot)
        if ult_info:
            w("")
            w("**Ultimate timeline** (cooldown from your upgrades and cooldown items at the time of each cast):")
            w("")
            w("| Cast | Cooldown | Ready again | Modifiers |")
            w("|---|---|---|---|")
            for t, cd, notes in ult_info:
                ready = fmt_t(t + cd) if cd and t + cd < m.duration else "after the game ended"
                w(f"| {fmt_t(t)} | {cd:.0f}s | {ready} | {', '.join(notes) or 'none'} |")
        w("")

    # ----- comparison with top-tier baselines
    for slot, brows in (extras.get("baseline") or {}).items():
        if not brows:
            continue
        p = m.players[slot]
        n = max(r["n"] for r in brows)
        w(f"## How you compare with top-tier {p.hero} players")
        w("")
        w(f"Your numbers in this game next to recent {p.hero} games at Phantom+ (up to {n} games). "
          "**Better than** = share of those games you beat on that measure; under 20% is flagged.")
        if n < 15:
            w("")
            w(f"> Only {n} top-tier games collected so far - treat these as rough.")
        w("")
        w("| Measure | You | Top tier: 25% / median / 75% | Better than |")
        w("|---|---|---|---|")
        for r in brows:
            share = r["key"].endswith("share") or r["key"] == "fight_presence"
            f = (lambda v: f"{v:.0%}") if share else (lambda v: f"{v:,.2f}" if abs(v) < 10 else f"{v:,.0f}")
            flag = " ⚠" if r["better_than"] < 0.2 else (" ✅" if r["better_than"] >= 0.8 else "")
            w(f"| {r['label']} | {f(r['you'])} | {f(r['p25'])} / {f(r['p50'])} / {f(r['p75'])} | {r['better_than']:.0%}{flag} |")
        w("")

    # ----- team fight damage
    rows = extras.get("fight_damage") or []
    if rows:
        w("## Team fight damage")
        w("")
        w("Damage to enemy heroes during each fight. **In fight** = seconds alive and either within 60 m of the fight or trading damage in it. "
          "**Expected** is the player's share of their team's damage given hero role (damage per soul "
          "for that hero across recent matches), souls at the time and seconds in the fight. "
          "Perfect fights don't exist: under 50% of expected is flagged, 160%+ is highlighted.")
        w("")
        team_rows = extras.get("team_damage") or []
        for f in fights:
            fr = [r for r in rows if r.fight is f]
            if not fr:
                continue
            win = "even" if f.winner is None else _team_label(m, f.winner, focus_team)
            tr = {team: (dmg, share) for ff, team, dmg, share in team_rows if ff is f}
            tot = sum(v[0] for v in tr.values()) or 1
            w(f"**Fight {fmt_t(f.deaths[0][0])}–{fmt_t(f.end)}** — winner: {win}. Damage share: "
              + " · ".join(f"team {team} {tr[team][0]:,} ({tr[team][0] / tot:.0%} of damage, {tr[team][1]:.0%} of souls)"
                           for team in sorted(tr)))
            w("")
            w("| Team | Hero | In fight | Damage | Expected | Rating |")
            w("|---|---|---|---|---|---|")
            for r in sorted(fr, key=lambda r: (r.player.team != (focus_team if focus_team is not None else 0), -r.damage)):
                if r.present_s == 0 and r.damage == 0:
                    rating = "not there"
                elif r.ratio is None:
                    rating = "too short to judge"
                elif r.ratio < 0.5:
                    rating = f"**low ({r.ratio:.0%})**"
                elif r.ratio >= 1.6:
                    rating = f"**high ({r.ratio:.0%})**"
                else:
                    rating = f"ok ({r.ratio:.0%})"
                mark = " ⬅" if r.player.slot in focus_slots else ""
                w(f"| {r.player.team} | {r.player.hero}{mark} | {r.present_s}s | {r.damage:,} | {r.expected:,.0f} | {rating} |")
            w("")

    # ----- everyone else
    w("## Other players - top issues")
    w("")
    for team in (0, 1):
        for p in m.team(team):
            if p.slot in focus_slots:
                continue
            bad = sorted([f for f in findings if f.slot == p.slot and not f.good], key=lambda f: -f.impact)[:3]
            if not bad:
                continue
            w(f"**{p.hero}** ({_team_label(m, team, focus_team)}, {p.kills}/{p.deaths_n}/{p.assists}): "
              + "; ".join(f"{f.title}" for f in bad))
            w("")
    w("---")
    w("_Generated by deadlock-review v0.1 from the public Deadlock API (match metadata + replay). "
      "Distances come from replay positions sampled once per second._")
    return "\n".join(L)
