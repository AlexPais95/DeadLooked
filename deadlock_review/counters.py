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
class Need:
    key: str
    title: str
    why: str
    strength: float                      # 0..1, how clearly this match calls for it
    since: float | None                  # game time the need was clear (for "buy by")
    heroes: list = field(default_factory=list)   # (hero, team, died, is_you) for pictures
    items: list = field(default_factory=list)    # item dicts from the assets API
    bought: list = field(default_factory=list)   # (name, time bought, time sold or None)


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
def _healing_population(raw_path: str, cache_dir: str) -> tuple[float, ...]:
    """Healing per minute of every player in the collected top-tier games (sorted)."""
    from .api import Api
    p = Path(raw_path)
    if not p.exists():
        return ()
    ids = sorted({json.loads(line)["match_id"] for line in p.read_text().splitlines() if line})
    api = Api(cache_dir, verbose=False)
    vals = []
    for mid in ids:
        try:
            meta = api.metadata(mid)
        except Exception:
            continue
        minutes = max(1.0, meta["duration_s"] / 60)
        for pl in meta.get("players", []):
            st = (pl.get("stats") or [{}])[-1]
            vals.append((st.get("player_healing") or 0) / minutes)
    return tuple(sorted(vals))


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


def counter_needs(m: Match, p: Player, raw_path: str = "baselines/raw.jsonl") -> list[Need]:
    groups = item_groups(list(m.items_by_id.values()))
    enemies = [q for q in m.players.values() if q.team != p.team]
    allies = [q for q in m.players.values() if q.team == p.team]
    minutes = max(1.0, m.duration / 60)
    needs: list[Need] = []

    # 1. anti-heal: enemies who healed a lot compared with top-tier players
    pop = _healing_population(str(Path(raw_path).resolve()), str(m.api.cache))
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
            items = groups[kind] + [i for i in groups["stun"] if i not in groups[kind]][:2]
            why = (f"{carry.hero} was their carry: {carry.net_worth:,} souls and {share_team:.0%} of their damage to your team "
                   f"({sp:.0%} spirit). {'Silence' if kind == 'silence' else 'Disarm'} or stun them when they go in.")
            needs.append(Need(f"shutdown_{kind}", f"Shut down {carry.hero}", why,
                              min(1.0, 0.3 + share_team), None, [_person(carry, p)], items, _bought(m, p, items)))
    needs.sort(key=lambda n: -n.strength)
    return needs


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
