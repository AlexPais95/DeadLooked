"""Counter items: what the enemy team did that an item answers, which items do that, and
whether (and when) you bought one.

Items are picked from the item assets by what they do (property values and description),
not from a hand-kept list, so they follow patches:
  anti-heal       HealAmpReceivePenaltyPercent <= -30 (on enemies)
  spirit resist   vitality items with TechResist >= 15
  bullet resist   vitality items with BulletResist >= 15 (bullet resist also reduces melee)
  spirit shred    TechArmorDamageReduction / MagicResistReduction <= -6
  bullet shred    BulletArmorReduction / BulletResistReduction <= -8
  silence / disarm / stun   from the item description

Needs come from this match:
  healing per minute vs all players in the collected top-tier games (metadata stats),
  damage you took and dealt by type (replay DamageEvent.citadel_type: 1 bullet, 2 spirit, 3 melee, 4 pure),
  enemy items (resist stacking) and the enemy carry's damage type.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .match import Match, Player, fmt_t

BULLET, SPIRIT, MELEE = 1, 2, 3
SELF_PENALTY = {"Cheat Death", "Blood Tribute"}   # their healing penalty applies to you
HEAL_PCT = 0.80        # enemy healing above this share of top-tier players = anti-heal need
TYPE_SHARE = 0.60      # this share of damage of one type = resist / shred need
RESIST_STACK = 25      # % resist from items that counts as "stacked"


@dataclass
class Role:
    damage: str                # "Spirit", "Gun", "Hybrid" (what kind of damage you dealt)
    team_role: str             # "Carry", "Support", "Frontliner", "Core"
    spirit_dmg: float          # share of your hero damage that was spirit
    weapon_dmg: float          # bullet + melee
    melee_dmg: float
    spend: dict                # slot -> share of item souls
    souls_rank: int = 0        # 1 = richest on your team
    dmg_share: float = 0.0     # your share of your team's hero damage
    taken_share: float = 0.0   # your share of the damage your team took
    support_out: float = 0.0   # healing + barrier you gave teammates
    played: str = ""           # what you did this game (team_role is what the hero is meant to do)
    hero_why: str = ""         # why the hero has that role at top tier
    hero: str = ""

    @property
    def name(self) -> str:     # damage type, used by FIT
        return self.damage

    @property
    def frontline(self) -> bool:
        return self.team_role == "Frontliner"

    @property
    def label(self) -> str:
        return f"{self.damage} {self.team_role.lower()}"

    def describe(self) -> str:
        if self.hero_why:
            this = {"Carry": f"#{self.souls_rank} in souls and {self.dmg_share:.0%} of your team's hero damage",
                    "Support": f"{self.support_out:,.0f} healing and barrier to teammates",
                    "Frontliner": f"you took {self.taken_share:.0%} of your team's damage",
                    "Core": f"#{self.souls_rank} in souls and {self.dmg_share:.0%} of your team's hero damage"}[self.played]
            same = "the same" if self.played == self.team_role else f"more like a {self.played.lower()}"
            return (f"Top-tier {self.hero} players play it as a {self.team_role.lower()}: it {self.hero_why}. "
                    f"This game you played it {same}: {this}. {self.spirit_dmg:.0%} of your damage was spirit")
        why = {
            "Carry": f"#{self.souls_rank} in souls on your team and {self.dmg_share:.0%} of its hero damage",
            "Support": f"{self.support_out:,.0f} healing and barrier given to teammates",
            "Frontliner": f"you took {self.taken_share:.0%} of the damage your team took",
            "Core": f"#{self.souls_rank} in souls and {self.dmg_share:.0%} of your team's hero damage",
        }[self.team_role]
        return f"{why}; {self.spirit_dmg:.0%} of your damage was spirit, {self.weapon_dmg:.0%} gun and melee"

    def ranking_note(self) -> str:
        return {
            "Carry": "Carries have the most souls, so bigger scaling items are fine",
            "Support": "Supports have fewer souls, so cheaper items, actives and items that help the whole team come first",
            "Frontliner": "Frontliners fight up close and soak damage, so vitality and close-range items come first",
            "Core": "Items are ranked by how they work with your damage",
        }[self.team_role]


def delivery(item: dict) -> str:
    """How an item delivers its effect: spirit / gun / melee / active / any."""
    d = _desc(item)
    if item.get("is_active_item"):
        return "active"
    # what triggers it, checked before what it affects ("reduces Bullet Resist when you deal spirit damage")
    if re.search(r"(your|deal|deals|dealing)\s+(significant\s+)?spirit damage|enemy takes\s+spirit damage", d, re.I):
        return "spirit"
    if re.search(r"your bullets|bullets (apply|build)|headshot|next bullet|deal additional\s+weapon damage", d, re.I):
        return "gun"
    if re.search(r"\bmelee\b", d, re.I):
        return "melee"
    if item.get("item_slot_type") == "weapon":
        return "gun"          # weapon passives without text proc on bullets (e.g. Spirit Rend)
    return "any"


FIT = {   # role -> delivery -> fit (0 = doesn't work with this build)
    "Spirit": {"spirit": 3, "active": 2, "any": 2, "melee": 1, "gun": 0},
    "Gun": {"gun": 3, "any": 2, "active": 1, "melee": 1, "spirit": 0},
    "Hybrid": {"spirit": 2, "gun": 2, "active": 2, "any": 2, "melee": 1},
}


TEAM_UTILITY = r"allies|ally|nearby enemies|enemies in|area|aura|teammate|friendly"


def fit(item: dict, role: Role | None, budget: float | None = None) -> int:
    """How well an item suits you: damage type first (0 = doesn't work with your build),
    then team role and what you could afford when the need was clear."""
    if role is None:
        return 2
    how = delivery(item)
    f = FIT[role.damage][how]
    if f == 0:
        return 0
    tier, cost, slot = item.get("item_tier") or 3, item.get("cost") or 0, item.get("item_slot_type")
    if role.team_role == "Support":
        f += (1 if tier <= 2 else 0) - (1 if tier >= 4 else 0)
        f += 1 if how == "active" else 0
        f += 1 if re.search(TEAM_UTILITY, _desc(item), re.I) else 0
    elif role.team_role == "Frontliner":
        f += 1 if slot == "vitality" else 0
        f += 1 if how in ("melee", "active") else 0
    elif role.team_role == "Carry":
        f += 1 if tier >= 3 and how in ("spirit", "gun") else 0
    if role.melee_dmg >= 0.3 and how == "melee":
        f += 2
    if budget and cost > 0.25 * budget:
        f -= 1          # too expensive for you at the time the need was clear
    return max(1, f)


def rank_for(items: list[dict], role: Role | None, budget: float | None = None) -> list[dict]:
    """Items that work with your build, best fit first, then cheaper first."""
    good = [i for i in items if role is None or FIT[role.damage][delivery(i)] > 0]
    return sorted(good, key=lambda i: (-fit(i, role, budget), i.get("item_tier") or 9, i.get("cost") or 0))


def player_role(m: Match, p: Player) -> Role | None:
    spend: dict[str, float] = {}
    for it in p.items:
        info = m.items_by_id.get(it.item_id) or {}
        slot = info.get("item_slot_type")
        if slot:
            spend[slot] = spend.get(slot, 0) + (info.get("cost") or 0)
    tot = sum(spend.values()) or 1
    spend = {k: v / tot for k, v in spend.items()}
    sp = wp = ml = None
    if m.replay_ok:
        enemies = [q for q in m.players.values() if q.team != p.team]
        dealt = _damage_by_type(m, [p], enemies, "dmg_dealt_by_type")
        dtot = sum(dealt.values()) or 1
        sp = sum(v for (s, ct), v in dealt.items() if ct == SPIRIT) / dtot
        wp = sum(v for (s, ct), v in dealt.items() if ct in (BULLET, MELEE)) / dtot
        ml = sum(v for (s, ct), v in dealt.items() if ct == MELEE) / dtot
    if sp is None:   # no replay: judge by item spend
        sp, wp, ml = spend.get("spirit", 0), spend.get("weapon", 0), 0.0
    damage = "Spirit" if sp >= 0.6 else ("Gun" if wp >= 0.6 else "Hybrid")

    # team role from the end-of-game stats, compared with your teammates
    def final(q):
        pl = next((x for x in m.meta.get("players", []) if x.get("player_slot") == q.slot), {})
        return (pl.get("stats") or [{}])[-1]
    team = m.team(p.team)
    stats = {q.slot: final(q) for q in team}
    tot = lambda k: sum((st.get(k) or 0) for st in stats.values()) or 1
    me = stats[p.slot]
    rank = 1 + sum(1 for q in team if q.net_worth > p.net_worth)
    dmg_share = (me.get("player_damage") or 0) / tot("player_damage")
    taken_share = (me.get("player_damage_taken") or 0) / tot("player_damage_taken")
    support = (me.get("teammate_healing") or 0) + (me.get("teammate_barriering") or 0)
    taken_rank = 1 + sum(1 for q in team if (stats[q.slot].get("player_damage_taken") or 0) > (me.get("player_damage_taken") or 0))
    if support >= 2500:
        team_role = "Support"
    elif rank <= 2 and dmg_share >= 0.22:
        team_role = "Carry"
    elif taken_share >= 0.22 and taken_rank <= 2:
        team_role = "Frontliner"
    elif rank >= 5 and dmg_share < 0.15:
        team_role = "Support"
    else:
        team_role = "Core"
    supposed = hero_roles(m.api).get(p.hero_id)
    if supposed:
        return Role(damage, supposed[0], sp, wp, ml, spend, rank, dmg_share, taken_share, support,
                    played=team_role, hero_why=supposed[1], hero=p.hero)
    return Role(damage, team_role, sp, wp, ml, spend, rank, dmg_share, taken_share, support, played=team_role, hero=p.hero)


@dataclass
class Need:
    key: str
    title: str
    why: str
    strength: float                      # 0..1, how clearly this match calls for it
    since: float | None                  # game time the need was clear (for "buy by")
    heroes: list = field(default_factory=list)   # (hero, team, died, is_you) for pictures
    items: list = field(default_factory=list)    # item dicts from the assets API
    bought: list = field(default_factory=list)   # (name, time bought, time sold or None)
    covered: list = field(default_factory=list)  # teammates who already had an answer: (hero, team, item, time)
    own: str = ""                                # what you already had towards it (stats/items)
    top: dict = field(default_factory=dict)      # item id -> (share of top-tier games, avg buy time, win rate)
    top_n: int = 0                               # top-tier games behind those numbers


def _num(v):
    try:
        return float(v.get("value") if isinstance(v, dict) else v)
    except (TypeError, ValueError):
        return 0.0


def _desc(item) -> str:
    d = item.get("description")
    d = d.get("desc", "") if isinstance(d, dict) else str(d or "")
    return re.sub(r"<[^>]+>|\s+", " ", d)


def _prop(item, name) -> float:
    return _num((item.get("properties") or {}).get(name))


def item_groups(items: list[dict]) -> dict[str, list[dict]]:
    shop = [i for i in items if i.get("type") == "upgrade" and i.get("shopable")]
    g: dict[str, list[dict]] = {k: [] for k in
                                ("anti_heal", "spirit_resist", "bullet_resist", "spirit_shred", "bullet_shred",
                                 "silence", "disarm", "stun")}
    for i in shop:
        d, slot = _desc(i), i.get("item_slot_type")
        if _prop(i, "HealAmpReceivePenaltyPercent") <= -30 and i["name"] not in SELF_PENALTY:
            g["anti_heal"].append(i)
        if slot == "vitality" and _prop(i, "TechResist") >= 15:
            g["spirit_resist"].append(i)
        if slot == "vitality" and _prop(i, "BulletResist") >= 15:
            g["bullet_resist"].append(i)
        if min(_prop(i, "TechArmorDamageReduction"), _prop(i, "MagicResistReduction")) <= -6:
            g["spirit_shred"].append(i)
        if min(_prop(i, "BulletArmorReduction"), _prop(i, "BulletResistReduction")) <= -8:
            g["bullet_shred"].append(i)
        # effects on yourself (Unstoppable, Reactive Barrier, Divine Barrier), not on enemies
        own = re.search(r"become\s+immune|immune to (Stun|stun)|when you are|you become|Unstoppable|Remove all", d)
        if not own and re.search(r"\bSilenc(e|es|ing)\b", d) and not re.search(r"movement-based", d):
            g["silence"].append(i)
        if not own and re.search(r"\bDisarm", d):
            g["disarm"].append(i)
        if not own and re.search(r"\b[Ss]tun", d) and not re.search(r"your ultimate", d):
            g["stun"].append(i)
    for k in g:
        g[k].sort(key=lambda i: (i.get("item_tier") or 9, i.get("cost") or 0))
    return g


@lru_cache(maxsize=1)
def _population(raw_path: str, cache_dir: str) -> tuple[tuple[float, ...], dict]:
    """From the collected top-tier games: everyone's healing per minute (sorted), and per hero
    the healing + barrier given to teammates per minute (list)."""
    from .api import Api
    p = Path(raw_path)
    if not p.exists():
        return (), {}
    ids = sorted({json.loads(line)["match_id"] for line in p.read_text().splitlines() if line})
    api = Api(cache_dir, verbose=False)
    vals, support = [], {}
    for mid in ids:
        try:
            meta = api.metadata(mid)
        except Exception:
            continue
        minutes = max(1.0, meta["duration_s"] / 60)
        for pl in meta.get("players", []):
            st = (pl.get("stats") or [{}])[-1]
            vals.append((st.get("player_healing") or 0) / minutes)
            support.setdefault(pl["hero_id"], []).append(
                ((st.get("teammate_healing") or 0) + (st.get("teammate_barriering") or 0)) / minutes)
    return tuple(sorted(vals)), support


def _healing_population(raw_path: str, cache_dir: str) -> tuple[float, ...]:
    return _population(raw_path, cache_dir)[0]


def hero_roles(api, raw_path: str = "baselines/raw.jsonl", min_badge: int = 90) -> dict[int, tuple[str, str]]:
    """What each hero is normally played as at top tier: {hero_id: (role, why)}.

    From hero averages over the last 30 days of top-tier games (souls, hero damage, damage taken,
    max health; compared across all heroes) and the healing/barrier they give teammates in the
    collected games. Carry = souls + damage; Frontliner = damage taken + health;
    Support = low souls and damage, or heals/shields the team."""
    import statistics
    try:
        stats = [h for h in api.hero_stats(min_badge) if h.get("matches", 0) >= 200]
    except Exception:
        return {}
    _, support = _population(str(Path(raw_path).resolve()), str(api.cache))
    rows = {h["hero_id"]: {"nw": h["total_net_worth"] / h["matches"], "dmg": h["total_player_damage"] / h["matches"],
                           "taken": h["total_player_damage_taken"] / h["matches"], "hp": h["total_max_health"] / h["matches"]}
            for h in stats}
    if len(rows) < 5:
        return {}
    z = {}
    for k in ("nw", "dmg", "taken", "hp"):
        vals = [r[k] for r in rows.values()]
        mu, sd = statistics.mean(vals), statistics.pstdev(vals) or 1
        for hid, r in rows.items():
            z.setdefault(hid, {})[k] = (r[k] - mu) / sd
    out = {}
    for hid, zz in z.items():
        heal = statistics.median(support[hid]) if len(support.get(hid, [])) >= 3 else 0
        scores = {"Carry": zz["nw"] + zz["dmg"], "Frontliner": zz["taken"] + zz["hp"],
                  "Support": -zz["nw"] - 0.5 * zz["dmg"] + (2.5 if heal >= 40 else 0)}
        role = max(scores, key=scores.get)
        why = {"Carry": "gets more souls and deals more damage than most heroes",
               "Frontliner": "takes more damage and has more health than most heroes",
               "Support": (f"heals or shields teammates, about {heal:.0f} per minute" if heal >= 40
                           else "gets fewer souls and deals less damage than most heroes")}[role]
        out[hid] = (role, why)
    return out


def _pct(sorted_vals, v) -> float | None:
    if not sorted_vals:
        return None
    below = sum(1 for x in sorted_vals if x < v)
    return below / len(sorted_vals)


def _stats(m: Match, q: Player) -> list[dict]:
    pl = next((x for x in m.meta.get("players", []) if x.get("player_slot") == q.slot), {})
    return pl.get("stats") or []


def _person(q: Player, you: Player) -> tuple:
    return (q.hero, q.team, False, q.slot == you.slot)


def _damage_by_type(m: Match, attackers: list[Player], victims: list[Player], name: str) -> dict:
    """{(attacker slot, type): damage} from the replay."""
    ent = {q.entity: q for q in m.players.values() if q.entity is not None}
    a_ids = ",".join(str(q.entity) for q in attackers if q.entity is not None)
    v_ids = ",".join(str(q.entity) for q in victims if q.entity is not None)
    if not a_ids or not v_ids:
        return {}
    sql = (f"SELECT entindex_attacker AS a, citadel_type AS ct, SUM(damage) AS dmg FROM DamageEvent "
           f"WHERE entindex_attacker IN ({a_ids}) AND entindex_victim IN ({v_ids}) GROUP BY 1, 2")
    out = {}
    for r in m.api.demo_query(m.match_id, sql, name):
        q = ent.get(r["a"])
        if q:
            out[(q.slot, r["ct"])] = out.get((q.slot, r["ct"]), 0) + (r["dmg"] or 0)
    return out


def _bought(m: Match, p: Player, group: list[dict]) -> list[tuple]:
    ids = {i["id"] for i in group}
    return [(it.name, it.t, it.sold_t) for it in sorted(p.items, key=lambda it: it.t) if it.item_id in ids]


def _resist_from_items(m: Match, q: Player, prop: str) -> float:
    total = 0.0
    for it in q.items:
        if it.sold_t:
            continue
        total += max(0.0, _prop(m.items_by_id.get(it.item_id) or {}, prop))
    return total


def counter_needs(m: Match, p: Player, raw_path: str = "baselines/raw.jsonl", role: Role | None = None) -> list[Need]:
    raw_groups = item_groups(list(m.items_by_id.values()))
    budget_at = lambda t: m.networth_at(p, int(t if t else m.duration * 0.6))
    groups = {k: rank_for(v, role, budget_at(None)) for k, v in raw_groups.items()}
    enemies = [q for q in m.players.values() if q.team != p.team]
    allies = [q for q in m.players.values() if q.team == p.team]
    minutes = max(1.0, m.duration / 60)
    needs: list[Need] = []

    # 1. anti-heal: enemies who healed a lot compared with top-tier players
    pop = _population(str(Path(raw_path).resolve()), str(m.api.cache))[0]
    healers = []
    for q in enemies:
        st = _stats(m, q)
        heal = (st[-1].get("player_healing") or 0) if st else 0
        pct = _pct(pop, heal / minutes) if pop else None
        if pct is not None and pct >= HEAL_PCT:
            since = next((s["time_stamp_s"] for s in st if s.get("time_stamp_s")
                          and _pct(pop, (s.get("player_healing") or 0) / max(1, s["time_stamp_s"] / 60)) >= HEAL_PCT), None)
            healers.append((q, heal, pct, since))
    if healers:
        healers.sort(key=lambda x: -x[1])
        top = healers[0]
        why = "; ".join(f"{q.hero} healed {heal:,.0f} ({heal / minutes:,.0f}/min, more than {pct:.0%} of players in top-tier games)"
                        for q, heal, pct, _ in healers[:3])
        since_heal = min((h[3] for h in healers if h[3]), default=None)
        groups["anti_heal"] = rank_for(raw_groups["anti_heal"], role, budget_at(since_heal))
        needs.append(Need("anti_heal", "Anti-heal", why, min(1.0, (top[2] - HEAL_PCT) / (1 - HEAL_PCT) * 0.6 + 0.4 + 0.1 * (len(healers) - 1)),
                          min((h[3] for h in healers if h[3]), default=None),
                          [_person(q, p) for q, *_ in healers[:4]], groups["anti_heal"], _bought(m, p, groups["anti_heal"])))

    if not m.replay_ok:
        return needs

    # 2. resist: what kind of damage you took
    taken = _damage_by_type(m, enemies, [p], "dmg_taken_by_type")
    tot = sum(taken.values()) or 1
    spirit = sum(v for (s, ct), v in taken.items() if ct == SPIRIT)
    weapon = sum(v for (s, ct), v in taken.items() if ct in (BULLET, MELEE))
    for key, share, cts, label in (("spirit_resist", spirit / tot, (SPIRIT,), "spirit"),
                                   ("bullet_resist", weapon / tot, (BULLET, MELEE), "bullet and melee")):
        if share >= TYPE_SHARE:
            by = sorted(((sum(v for (s, ct), v in taken.items() if s == q.slot and ct in cts), q) for q in enemies),
                        key=lambda x: -x[0])
            by = [(d, q) for d, q in by if d > 0.1 * tot * share]
            why = (f"{share:.0%} of the damage you took was {label}: "
                   + ", ".join(f"{q.hero} {d:,.0f}" for d, q in by[:3]))
            needs.append(Need(key, "Spirit resist" if key == "spirit_resist" else "Bullet resist", why,
                              min(1.0, (share - TYPE_SHARE) / (1 - TYPE_SHARE) * 0.6 + 0.4), None,
                              [_person(q, p) for _, q in by[:3]], groups[key], _bought(m, p, groups[key])))

    # 3. shred: your damage type vs the resist the enemies built
    dealt = _damage_by_type(m, [p], enemies, "dmg_dealt_by_type")
    dtot = sum(dealt.values()) or 1
    my_spirit = sum(v for (s, ct), v in dealt.items() if ct == SPIRIT) / dtot
    my_weapon = sum(v for (s, ct), v in dealt.items() if ct in (BULLET, MELEE)) / dtot
    for key, mine, prop, label in (("spirit_shred", my_spirit, "TechResist", "spirit"),
                                   ("bullet_shred", my_weapon, "BulletResist", "bullet")):
        if mine < TYPE_SHARE:
            continue
        stacked = sorted(((_resist_from_items(m, q, prop), q) for q in enemies), key=lambda x: -x[0])
        stacked = [(r, q) for r, q in stacked if r >= RESIST_STACK]
        if stacked:
            why = (f"{mine:.0%} of your damage was {label}, and {len(stacked)} enemies built {label} resist: "
                   + ", ".join(f"{q.hero} +{r:.0f}%" for r, q in stacked[:4]))
            needs.append(Need(key, "Spirit shred" if key == "spirit_shred" else "Bullet shred", why,
                              min(1.0, 0.35 + 0.15 * len(stacked)), None,
                              [_person(q, p) for _, q in stacked[:4]], groups[key], _bought(m, p, groups[key])))

    # 4. the enemy carry: most damage to your team among their two richest
    team_taken = _damage_by_type(m, enemies, allies, "dmg_to_team_by_type")
    rich = sorted(enemies, key=lambda q: -q.net_worth)[:2]
    dmg_of = lambda q: sum(v for (s, ct), v in team_taken.items() if s == q.slot)
    carry = max(rich, key=dmg_of) if rich else None
    if carry and dmg_of(carry) > 0:
        cd = dmg_of(carry)
        sp = sum(v for (s, ct), v in team_taken.items() if s == carry.slot and ct == SPIRIT) / cd
        share_team = cd / (sum(dmg_of(q) for q in enemies) or 1)
        if share_team >= 0.25:
            kind = "silence" if sp >= 0.5 else "disarm"
            items = rank_for(groups[kind] + [i for i in groups["stun"] if i not in groups[kind]], role, budget_at(None))
            why = (f"{carry.hero} was their carry: {carry.net_worth:,} souls and {share_team:.0%} of their damage to your team "
                   f"({sp:.0%} spirit). {'Silence' if kind == 'silence' else 'Disarm'} or stun them when they go in.")
            needs.append(Need(f"shutdown_{kind}", f"Shut down {carry.hero}", why,
                              min(1.0, 0.3 + share_team), None, [_person(carry, p)], items, _bought(m, p, items)))
    _covered_and_top(m, p, needs, role, raw_groups, allies)
    needs.sort(key=lambda n: -n.strength)
    return needs


TEAM_WIDE = {"anti_heal": "anti_heal", "spirit_shred": "spirit_shred", "bullet_shred": "bullet_shred"}


def _covered_and_top(m: Match, p: Player, needs: list[Need], role, raw_groups, allies) -> None:
    """Lower a need's importance when you or teammates already answered it, and rank its items by
    how often top-tier players of your hero buy them against these enemies."""
    for n in needs:
        # teammates who already had an answer (their anti-heal/shred/shutdown affects the same enemies)
        group_ids = {i["id"] for i in n.items} | ({i["id"] for i in raw_groups.get(TEAM_WIDE.get(n.key, ""), [])})
        if n.key.startswith("shutdown"):
            group_ids |= {i["id"] for k in ("silence", "disarm", "stun") for i in raw_groups[k]}
        for q in allies:
            if q.slot == p.slot or n.key in ("spirit_resist", "bullet_resist"):
                continue  # resist only protects whoever owns it
            got = sorted((it for it in q.items if it.item_id in group_ids), key=lambda it: it.t)
            if got:
                n.covered.append((q.hero, q.team, got[0].name, got[0].t))
        if n.key in ("spirit_resist", "bullet_resist"):
            # resist is personal: what you already had from items
            prop = "TechResist" if n.key == "spirit_resist" else "BulletResist"
            have = [(it.name, _prop(m.items_by_id.get(it.item_id) or {}, prop)) for it in p.items if not it.sold_t]
            have = [(nm, v) for nm, v in have if v > 0]
            total = sum(v for _, v in have)
            if total:
                n.own = f"You already had +{total:.0f}% {'spirit' if prop == 'TechResist' else 'bullet'} resist from " + \
                        ", ".join(f"{nm} ({v:+.0f}%)" for nm, v in have)
                n.strength *= 0.85 if total < 25 else 0.65
        n.strength *= max(0.4, 1 - 0.3 * len(n.covered))

        # what top-tier players of your hero buy against these enemies
        enemy_ids = [q.hero_id for q in m.players.values() if q.team != p.team
                     and q.hero in {h[0] for h in n.heroes}]
        try:
            hero_games = next((h["matches"] for h in m.api.hero_stats(90) if h["hero_id"] == p.hero_id), 0)
            overall = m.api.item_stats_vs(p.hero_id, 90)
            vs = m.api.item_stats_vs(p.hero_id, 90, enemy_ids) if enemy_ids else overall
        except Exception:
            continue
        if not hero_games or not overall or not vs:
            continue
        scale = max(x["matches"] for x in vs) / max(x["matches"] for x in overall)
        games = hero_games * scale            # estimated top-tier games of your hero vs these enemies
        n.top_n = int(games)
        by = {x["item_id"]: x for x in vs}
        for i in n.items:
            x = by.get(i["id"])
            if x and x["matches"]:
                n.top[i["id"]] = (x["matches"] / games, x.get("avg_buy_time_s") or 0, x["wins"] / x["matches"])
        budget = m.networth_at(p, int(n.since or m.duration * 0.6))

        def score(i):
            rate = n.top.get(i["id"], (0, 0, 0))[0]
            return fit(i, role, budget) + (2 if rate >= 0.10 else 1 if rate >= 0.03 else 0)
        n.items.sort(key=lambda i: (-score(i), -n.top.get(i["id"], (0,))[0], i.get("item_tier") or 9))


def counter_findings(m: Match, p: Player, needs: list[Need]):
    """Mistakes for clear needs you never answered."""
    from .analysis import Finding
    out = []
    for n in needs:
        if n.bought or n.strength < 0.5:
            continue
        out.append(Finding(p.slot, p.team, n.since or m.duration, "missing_counter", 1.0 + n.strength,
                           f"No {n.title.lower()} item" if not n.key.startswith("shutdown") else f"Nothing to {n.title.lower()}",
                           n.why + ". Options: " + ", ".join(i["name"] for i in n.items[:4]),
                           people=tuple(n.heroes), short=n.why))
    return out
