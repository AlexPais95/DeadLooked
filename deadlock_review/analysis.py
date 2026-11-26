"""Rule-based mistake detectors (version 1).

Each rule emits Findings with a rough `impact` score so the report can rank them.
The impact score is a hand-made stand-in for Win Probability Added (WPA); a trained
win-probability model can replace `impact` later without changing the rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .heroes import cfg
from .match import Match, Player, dist_m, fmt_t

FIGHT_GAP_S = 15  # deaths closer than this belong to the same fight
FIGHT_MIN_DEATHS = 3
ABSENT_M = 60  # further than this from a fight = not there
ISOLATED_M = 30  # no ally this close before dying = caught alone
CONVERT_WINDOW_S = 90  # time to turn a won fight into an objective
HIGH_BADGE = 90  # Phantom and above (badge = tier*10 + subtier)


@dataclass
class Finding:
    slot: int | None  # None = team-level
    team: int
    t: float
    kind: str
    impact: float
    title: str
    detail: str = ""
    good: bool = False


@dataclass
class Fight:
    start: int
    end: int
    center: tuple[float, float]
    deaths: list  # (t, Player, Death)
    losses: dict = field(default_factory=dict)  # team -> deaths
    winner: int | None = None
    converted: list = field(default_factory=list)  # objectives taken by winner after the fight


def _cooldown(m: Match, class_name: str) -> float | None:
    info = m.items_by_class.get(class_name) or {}
    v = ((info.get("properties") or {}).get("AbilityCooldown") or {}).get("value")
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _slot_class(m: Match, p: Player, slot_key: str | None) -> str | None:
    if not slot_key:
        return None
    return ((m.hero_assets.get(p.hero_id) or {}).get("items") or {}).get(slot_key)


# ----------------------------------------------------------------------- fights
def find_fights(m: Match) -> list[Fight]:
    deaths = m.all_deaths()
    clusters, cur = [], []
    for d in deaths:
        if cur and d[0] - cur[-1][0] > FIGHT_GAP_S:
            clusters.append(cur)
            cur = []
        cur.append(d)
    if cur:
        clusters.append(cur)

    fights = []
    for c in clusters:
        if len(c) < FIGHT_MIN_DEATHS:
            continue
        cx = sum(d[2].pos[0] for d in c) / len(c)
        cy = sum(d[2].pos[1] for d in c) / len(c)
        f = Fight(start=c[0][0] - 10, end=c[-1][0], center=(cx, cy), deaths=c)
        for _, p, _ in c:
            f.losses[p.team] = f.losses.get(p.team, 0) + 1
        l0, l1 = f.losses.get(0, 0), f.losses.get(1, 0)
        f.winner = 0 if l0 < l1 else 1 if l1 < l0 else None
        if f.winner is not None:
            loser = 1 - f.winner
            f.converted = [o for o in m.objectives
                           if o["team"] == loser and f.end <= o["destroyed_time_s"] <= f.end + CONVERT_WINDOW_S]
        fights.append(f)
    return fights


def _fight_distance(p: Player, f: Fight) -> float | None:
    """Closest the player got to any death of the fight, around the time it happened.

    Deaths in one fight can be spread out (a chase, a split push), so we measure to each
    death rather than to their average position."""
    best = None
    for t_death, victim, death in f.deaths:
        if victim.slot == p.slot or death.killer_slot == p.slot:
            return 0.0
        for t in range(t_death - 8, t_death + 3, 2):
            pos = p.at(t, tol=1)
            if pos and pos[3]:
                d = dist_m(pos, death.pos)
                best = d if best is None else min(best, d)
    return best


def _killer_ult(m: Match, killer: Player | None, t: float):
    """The killer's ultimate if they cast it in the 12 seconds before this death."""
    if not killer:
        return None
    ult = _slot_class(m, killer, "signature4")
    for c in killer.casts:
        if c.ability == ult and t - 12 <= c.t <= t:
            return c
    return None


# ---------------------------------------------------------------- ultimates
def _num(v) -> float:
    try:
        return float(str(v).rstrip("m"))
    except (TypeError, ValueError):
        return 0.0


def ability_cooldown_at(m: Match, p: Player, cls: str, t: float) -> tuple[float | None, list[str]]:
    """Cooldown of an ability at time t: base cooldown, minus ability upgrades owned
    (e.g. Doorman Hotel Guest T1 -20s), times cooldown reduction from items owned.
    Imbued items (Compress Cooldown) only count when imbued into this ability.
    Item reductions are stacked multiplicatively (conservative if the game adds them)."""
    base = _cooldown(m, cls)
    if base is None:
        return None, []
    info = m.items_by_class.get(cls) or {}
    notes = []
    level = sum(1 for tt, cn in p.ability_ups if cn == cls and tt <= t) - 1  # first entry = unlock
    for up in (info.get("upgrades") or [])[:max(0, level)]:
        for pu in up.get("property_upgrades", []):
            if pu.get("name") == "AbilityCooldown" and not pu.get("upgrade_type"):
                base += _num(pu.get("bonus"))
                notes.append(f"upgrade {pu.get('bonus')}s")
    mult = 1.0
    for it in p.items:
        if it.t > t or (it.sold_t and it.sold_t <= t):
            continue
        iinfo = m.items_by_id.get(it.item_id) or {}
        cdr = _num(((iinfo.get("properties") or {}).get("CooldownReduction") or {}).get("value"))
        if not cdr:
            continue
        if iinfo.get("imbue") and it.imbued_ability_id != info.get("id"):
            continue
        mult *= 1 - cdr / 100
        notes.append(f"{it.name} -{cdr:g}%")
    return max(1.0, base * mult), notes


def ult_windows(m: Match, p: Player):
    """Casts of the ultimate and the windows when it was ready.

    The cooldown after each cast uses the upgrades and cooldown items owned at that
    moment; a Refresher activation makes the ultimate ready again."""
    ult = _slot_class(m, p, "signature4")
    if not ult:
        return None, [], []
    unlock = next((t for t, cn in p.ability_ups if cn == ult), None)
    casts = sorted(c.t for c in p.casts if c.ability == ult)
    refresher = sorted(c.t for c in p.casts if c.ability == "upgrade_refresher")
    if unlock is None or _cooldown(m, ult) is None:
        return ult, casts, []
    ready, start = [], unlock
    for c in casts + [m.duration]:
        if c > start:
            ready.append((start, c))
        if c >= m.duration:
            break
        cd, _ = ability_cooldown_at(m, p, ult, c)
        start = c + cd
        refresh = next((r for r in refresher if c < r < start), None)
        if refresh is not None:
            start = refresh
    return ult, casts, ready


def _ready_at(windows, t) -> bool:
    return any(a <= t <= b for a, b in windows)


# ----------------------------------------------------------------- the rules
def analyze(m: Match, focus_slots: set[int], benchmarks: dict | None = None):
    findings: list[Finding] = []
    fights = find_fights(m)
    benchmarks = benchmarks or {}

    # ---------- team: fights, conversions, objectives lost while players were dead
    for f in fights:
        if f.winner is not None and not f.converted and m.duration - f.end > 30:
            findings.append(Finding(
                None, f.winner, f.end, "no_conversion", 2.0,
                f"Won the fight at {fmt_t(f.end)} ({f.losses.get(1 - f.winner, 0)} kills vs "
                f"{f.losses.get(f.winner, 0)} deaths) but took no objective within {CONVERT_WINDOW_S}s",
                "A won fight is the moment to push a guardian, walker or the Mid-Boss.",
            ))
    for o in m.objectives:
        team = o["team"]  # owner of the destroyed objective
        t = o["destroyed_time_s"]
        dead = [p for p in m.team(team) if not p.alive_at(t)]
        if len(dead) >= 2:
            findings.append(Finding(
                None, team, t, "objective_lost_dead", 1.0 + 0.5 * len(dead),
                f"Lost an objective at {fmt_t(t)} with {len(dead)} players dead",
                "Dead: " + ", ".join(p.hero for p in dead),
            ))

    # ---------- per player
    for p in m.players.values():
        c = cfg(p.hero)
        tips = c.get("tips", {})
        ult, casts, ready = ult_windows(m, p) if m.replay_ok else (None, [], [])
        ult_logged = bool(casts)
        esc = _slot_class(m, p, c.get("escape_slot"))
        esc_casts = sorted(x.t for x in p.casts if x.ability == esc) if esc else []
        esc_cd = _cooldown(m, esc) if esc else None

        # deaths
        for d in p.deaths:
            obj_lost = [o for o in m.objectives
                        if o["team"] == p.team and d.t <= o["destroyed_time_s"] <= d.t + d.duration]
            impact = 1.0 + d.duration / 30 + 1.5 * len(obj_lost)
            extra = []
            if obj_lost:
                extra.append(f"your team lost {len(obj_lost)} objective(s) while you were dead ({d.duration}s)")
            killer = m.players.get(d.killer_slot)
            kind, title = "death", f"Died at {fmt_t(d.t)}" + (f" to {killer.hero}" if killer else "")
            if m.replay_ok:
                allies, enemies = m.near(p, d.t - 3, ISOLATED_M + 5)
                near_allies = [a for a in allies if a[1] <= ISOLATED_M]
                k_ult = _killer_ult(m, killer, d.t)
                if k_ult:
                    title += f" (killed during {killer.hero}'s {k_ult.name}, cast at {fmt_t(k_ult.t)})"
                    impact += 0.5
                    extra.append(f"keep track of {killer.hero}'s ultimate - stay out of its range while it's ready")
                if k_ult and not enemies:
                    pass  # ultimates like Hotel Guest move you; positions aren't meaningful here
                elif not near_allies:
                    kind, impact = "isolated_death", impact + 1.5
                    title += f" with no ally within {ISOLATED_M} m"
                    extra.append(f"enemies nearby: {', '.join(e[0].hero for e in enemies) or 'none visible'}")
                    if tips.get("isolated_death"):
                        extra.append(tips["isolated_death"])
                elif len(enemies) >= len(near_allies) + 2:
                    kind, impact = "outnumbered_death", impact + 1.0
                    title += f" outnumbered ({len(enemies)} enemies vs {len(near_allies) + 1})"
                if esc_casts and esc_cd:
                    last = max((t for t in esc_casts if t < d.t), default=None)
                    if last is None or d.t - last > esc_cd + 2:
                        extra.append(f"your escape ability ({m.items_by_class.get(esc, {}).get('name', esc)}) "
                                     "was probably available and not used")
                        impact += 0.5
                if ult_logged and _ready_at(ready, d.t) and c["ult_kind"] in ("aoe", "target"):
                    extra.append("you died with your ultimate ready")
                    impact += 0.5
            findings.append(Finding(p.slot, p.team, d.t, kind, impact, title, "; ".join(extra)))

        # absent from fights / ult not used in fights
        if m.replay_ok:
            for f in fights:
                if not (p.alive_at(f.start) and p.alive_at(f.end)):
                    continue
                dmin = _fight_distance(p, f)
                if dmin is None:
                    continue
                lost = f.winner is not None and f.winner != p.team
                ult_up = ult_logged and _ready_at(ready, f.start)
                if dmin > ABSENT_M:
                    impact = 1.5 + (1.5 if lost else 0) + (2.0 if ult_up else 0)
                    title = f"Not in the fight at {fmt_t(f.end)} (closest {dmin:.0f} m)" + (
                        " with ultimate ready" if ult_up else "")
                    res = "your team lost it" if lost else ("your team won it" if f.winner == p.team else "even trade")
                    findings.append(Finding(p.slot, p.team, f.end, "absent_fight", impact, title,
                                            f"{res}; " + (tips.get("ult_held", "") if ult_up else "")))
                elif ult_up and not any(f.start - 5 <= t <= f.end + 5 for t in casts) and c["ult_kind"] == "aoe":
                    findings.append(Finding(p.slot, p.team, f.end, "ult_unused_fight", 2.0 if lost else 1.0,
                                            f"In the fight at {fmt_t(f.end)} but didn't use your ultimate",
                                            "Cooldown calculated from your upgrades and cooldown items."))

        # ultimate quality
        if m.replay_ok and ult_logged:
            name = m.items_by_class.get(ult, {}).get("name", ult)
            for t in casts:
                allies, enemies = m.near(p, t + 1, max(c["ult_radius_m"], 30))
                caught = [e for e in enemies if e[1] <= c["ult_radius_m"]]
                close_allies = [a for a in allies if a[1] <= 15]
                kills = [x for x in m.all_deaths() if t <= x[0] <= t + 15 and x[1].team != p.team]
                losses = [x for x in m.all_deaths() if t <= x[0] <= t + 15 and x[1].team == p.team]
                desc = (f"enemies within {c['ult_radius_m']} m: {', '.join(e[0].hero for e in caught) or 'none'}; "
                        f"allies within 15 m: {', '.join(a[0].hero for a in close_allies) or 'none'}; "
                        f"next 15s: {len(kills)} enemy deaths, {len(losses)} of yours")
                if c["ult_kind"] == "aoe" and len(caught) >= 2 and len(kills) > len(losses):
                    findings.append(Finding(p.slot, p.team, t, "good_ult", 0, f"Good {name} at {fmt_t(t)}",
                                            desc, good=True))
                elif c["ult_kind"] in ("aoe", "target") and caught and not close_allies:
                    findings.append(Finding(p.slot, p.team, t, "ult_no_followup", 2.0 + 0.5 * len(losses),
                                            f"{name} at {fmt_t(t)} with no ally within 15 m",
                                            desc + ". " + tips.get("ult_no_allies", "")))
                elif c["ult_kind"] == "aoe" and len(caught) <= 1:
                    findings.append(Finding(p.slot, p.team, t, "ult_weak", 1.0,
                                            f"{name} at {fmt_t(t)} caught {len(caught)} enemy", desc))

        # early deaths (laning)
        lane_deaths = [d for d in p.deaths if d.t < 600]
        if len(lane_deaths) >= 3:
            findings.append(Finding(p.slot, p.team, lane_deaths[-1].t, "lane_deaths", 1.0 + 0.5 * len(lane_deaths),
                                    f"Died {len(lane_deaths)} times before 10:00",
                                    "Every lane death gives the enemy souls and costs you farm."))

        # benchmarks (items / abilities vs top-tier players of this hero)
        b = benchmarks.get(p.hero_id)
        if b and p.slot in focus_slots:
            findings += _benchmark_findings(m, p, b)

    extras = {"fight_damage": [], "team_damage": [], "ult": {}}
    if m.replay_ok and fights:
        eff = hero_efficiency(m)
        rows, team_rows = fight_damage(m, fights, eff)
        findings += fight_damage_findings(m, rows)
        extras["fight_damage"], extras["team_damage"] = rows, team_rows
    extras["objectives"] = {}
    if m.replay_ok:
        from .rotations import needed_findings, objective_visits, rotation_findings
        for p in m.players.values():
            findings += rotation_findings(m, p) + needed_findings(m, p)
        extras["objectives"] = {s: objective_visits(m, m.players[s]) for s in focus_slots}
    for s in focus_slots:
        p = m.players[s]
        ult, casts, ready = ult_windows(m, p)
        if ult and casts:
            extras["ult"][s] = [(c, *ability_cooldown_at(m, p, ult, c)) for c in casts]
    return fights, findings, extras


def _benchmark_findings(m: Match, p: Player, b: dict) -> list[Finding]:
    out = []
    avg_buy = b.get("avg_buy", {})
    seen = set()
    for t, name, tier, _ in p.item_buys:
        if name in seen or tier < 2 or name not in avg_buy:
            continue
        seen.add(name)
        ref = avg_buy[name]
        late = t - ref
        if late > max(240, 0.3 * ref):
            key = name in cfg(p.hero)["key_items"]
            out.append(Finding(p.slot, p.team, t, "late_item", 1.0 + (0.5 if key else 0),
                               f"{name} bought at {fmt_t(t)} - top players get it around {fmt_t(ref)}",
                               "Key item for this hero." if key else ""))
    ref_order = b.get("ability_order")
    if ref_order:
        mine = _first_maxed(p.ability_ups)
        theirs = ref_order.get("first_maxed")
        if mine and theirs and mine != theirs:
            names = m.items_by_class
            out.append(Finding(p.slot, p.team, 0, "ability_order", 1.0,
                               f"You maxed {names.get(mine, {}).get('name', mine)} first; most top players max "
                               f"{names.get(theirs, {}).get('name', theirs)} first",
                               f"Their most common order wins {ref_order.get('win_rate', 0):.1f}% of "
                               f"{ref_order.get('matches', 0)} games."))
    return out


def _first_maxed(ups: list[tuple[int, str]]) -> str | None:
    counts = {}
    for _, cn in sorted(ups):
        counts[cn] = counts.get(cn, 0) + 1
        if counts[cn] == 4:  # unlock + 3 upgrades
            return cn
    return None


def load_benchmarks(m: Match, hero_ids: set[int], min_badge: int = HIGH_BADGE) -> dict:
    """Item timings and ability order of top-tier players for each hero."""
    out = {}
    for hid in hero_ids:
        try:
            items = m.api.item_stats(hid, min_badge)
            orders = m.api.ability_order_stats(hid, min_badge)
        except Exception as e:  # benchmarks are optional
            print(f"[bench] skipped hero {hid}: {e}")
            continue
        avg_buy = {}
        for r in items:
            info = m.items_by_id.get(r["item_id"])
            if info and r.get("avg_buy_time_s") and r["matches"] >= 100:
                avg_buy[info["name"]] = r["avg_buy_time_s"]
        ref = None
        if orders:
            top = max(orders, key=lambda r: r["matches"])
            ids_to_class = {i["id"]: i.get("class_name") for i in m.items_by_id.values()}
            seq = [(0, ids_to_class.get(a, str(a))) for a in top["abilities"]]
            ref = {"first_maxed": _first_maxed(seq), "matches": top["matches"],
                   "win_rate": 100 * top["wins"] / max(1, top["matches"])}
        out[hid] = {"avg_buy": avg_buy, "ability_order": ref}
    return out


# ------------------------------------------------------------ fight damage
LOW_RATIO = 0.5  # below half of the expected share = clearly underperformed
HIGH_RATIO = 1.6
MIN_PRESENT_S = 5  # less time than this in a fight is too little to judge
MIN_EXPECTED_DMG = 250


@dataclass
class FightDamage:
    fight: Fight
    player: Player
    present_s: int
    damage: int
    expected: float
    ratio: float | None


def hero_efficiency(m: Match) -> dict[int, float]:
    """Damage to heroes per soul of net worth, per hero, over recent matches.

    This is the "how much impact is this hero supposed to have" baseline: a support
    like Ivy deals far less damage per soul than Haze, so it is expected to deal less."""
    try:
        rows = m.api.hero_stats()
    except Exception as e:
        print(f"[bench] hero stats unavailable: {e}")
        return {}
    out = {}
    for r in rows:
        if r.get("total_net_worth"):
            out[r["hero_id"]] = r["total_player_damage"] / r["total_net_worth"]
    return out


def _fight_window(f: Fight) -> tuple[int, int]:
    return f.deaths[0][0] - 15, f.deaths[-1][0] + 3


def _present_seconds(m: Match, p: Player, f: Fight) -> int:
    """Seconds the player was alive and taking part in the fight: within ABSENT_M of any
    death of the fight, or between their first and last hit on an enemy hero in it
    (long-range heroes fight from further away)."""
    t0, t1 = _fight_window(f)
    hits = [t for t, v in p.damage_out.items() if t0 <= t <= t1 and v > 0]
    h0, h1 = (min(hits), max(hits)) if hits else (None, None)
    n = 0
    for t in range(t0, t1 + 1):
        if not p.alive_at(t):
            continue
        if h0 is not None and h0 <= t <= h1:
            n += 1
            continue
        pos = p.at(t, tol=1)
        if pos and pos[3] and any(dist_m(pos, d.pos) <= ABSENT_M for _, _, d in f.deaths):
            n += 1
    return n


def fight_damage(m: Match, fights: list[Fight], eff: dict[int, float]):
    """Damage each player dealt to enemy heroes in each fight, against an expected share.

    Expected share inside a team = hero efficiency x souls at the time x seconds present,
    normalised so the team's expected total equals what the team actually dealt.
    So a player is judged against teammates in the same fight, adjusted for hero role,
    items (souls) and how long they were alive there. A team-level line compares the
    two teams' damage with their soul totals."""
    rows: list[FightDamage] = []
    team_rows: list[tuple[Fight, int, int, float]] = []  # fight, team, damage, share-of-souls
    avg_eff = (sum(eff.values()) / len(eff)) if eff else 1.0
    for f in fights:
        t0, t1 = _fight_window(f)
        per_team = {}
        for team in (0, 1):
            data = []
            for p in m.team(team):
                present = _present_seconds(m, p, f)
                dmg = sum(v for t, v in p.damage_out.items() if t0 <= t <= t1)
                weight = eff.get(p.hero_id, avg_eff) * m.networth_at(p, f.deaths[0][0]) * present
                data.append((p, present, dmg, weight))
            total_dmg = sum(d[2] for d in data)
            total_w = sum(d[3] for d in data) or 1.0
            for p, present, dmg, weight in data:
                exp = total_dmg * weight / total_w
                ratio = dmg / exp if exp >= MIN_EXPECTED_DMG and present >= MIN_PRESENT_S else None
                rows.append(FightDamage(f, p, present, dmg, exp, ratio))
            per_team[team] = total_dmg
        nw = {team: sum(m.networth_at(p, f.deaths[0][0]) for p in m.team(team)) for team in (0, 1)}
        for team in (0, 1):
            share = nw[team] / max(1, nw[0] + nw[1])
            team_rows.append((f, team, per_team[team], share))
    return rows, team_rows


def fight_damage_findings(m: Match, rows: list[FightDamage]) -> list[Finding]:
    out = []
    for r in rows:
        if r.ratio is None:
            continue
        lost = r.fight.winner is not None and r.fight.winner != r.player.team
        if r.ratio < LOW_RATIO:
            out.append(Finding(
                r.player.slot, r.player.team, r.fight.end, "low_fight_damage", 1.0 + (1.0 if lost else 0),
                f"Low damage in the fight at {fmt_t(r.fight.end)}: {r.damage:,} vs ~{r.expected:,.0f} expected "
                f"({r.ratio:.0%}) over {r.present_s}s in the fight",
                "Expected = your share of your team's damage given your hero, souls and time alive in the fight.",
            ))
        elif r.ratio >= HIGH_RATIO:
            out.append(Finding(
                r.player.slot, r.player.team, r.fight.end, "high_fight_damage", 0,
                f"High damage in the fight at {fmt_t(r.fight.end)}: {r.damage:,} ({r.ratio:.0%} of expected)",
                f"{r.present_s}s in the fight", good=True,
            ))
    return out
