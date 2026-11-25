"""Builds a single, time-aligned view of a match from the API metadata and the replay.

Coordinates are game units; METERS converts them (Source units, ~39.37 per meter).
All times are game seconds (the clock players see).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from .api import Api

METERS = 39.37
TICKS_PER_SEC = 64

SQL_IMPORTANT = "SELECT tick, caster, ability_name FROM ImportantAbilityUsedEvent ORDER BY tick"
SQL_KILLS = (
    "SELECT tick, entindex_victim, entindex_attacker, victim_team_number "
    "FROM HeroKilledEvent ORDER BY tick"
)
SQL_CONTROLLERS = (
    'SELECT entity_index, last_value("m_PlayerDataGlobal__m_nHeroID" ORDER BY tick) AS hero, '
    'last_value("m_hHeroPawn" ORDER BY tick) AS pawn, last_value("m_iTeamNum" ORDER BY tick) AS team '
    'FROM CCitadelPlayerController WHERE "m_PlayerDataGlobal__m_nHeroID" > 0 GROUP BY entity_index'
)
SQL_POSITIONS = (
    "SELECT entity_index, CAST(tick / 64 AS INT) AS sec, "
    'last_value("m_lifeState" ORDER BY tick) AS life, last_value("m_iHealth" ORDER BY tick) AS hp, '
    'last_value("CBodyComponent__m_cellX" ORDER BY tick) AS cx, '
    'last_value("CBodyComponent__m_cellY" ORDER BY tick) AS cy, '
    'last_value("CBodyComponent__m_vecX" ORDER BY tick) AS vx, '
    'last_value("CBodyComponent__m_vecY" ORDER BY tick) AS vy '
    "FROM CCitadelPlayerPawn GROUP BY entity_index, CAST(tick / 64 AS INT)"
)


def world_xy(cx, cy, vx, vy) -> tuple[float, float]:
    return cx * 512 - 32768 + vx, cy * 512 - 32768 + vy


def dist_m(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1]) / METERS


def fmt_t(t: float) -> str:
    t = max(0, int(round(t)))
    return f"{t // 60}:{t % 60:02d}"


@dataclass
class Death:
    t: int
    killer_slot: int | None
    pos: tuple[float, float]
    duration: int


@dataclass
class Cast:
    t: float
    ability: str  # class name, e.g. citadel_ability_self_vacuum
    name: str  # display name


@dataclass
class ItemBuy:
    t: int
    item_id: int
    name: str
    tier: int
    sold_t: int | None
    imbued_ability_id: int | None


@dataclass
class Player:
    slot: int
    team: int  # 0 / 1 as in metadata
    hero_id: int
    hero: str
    account_id: int
    kills: int
    deaths_n: int
    assists: int
    net_worth: int
    deaths: list[Death] = field(default_factory=list)
    item_buys: list[tuple[int, str, int, int | None]] = field(default_factory=list)  # (t, name, tier, sold_t)
    ability_ups: list[tuple[int, str]] = field(default_factory=list)  # (t, class_name)
    nw_by_time: dict[int, int] = field(default_factory=dict)
    casts: list[Cast] = field(default_factory=list)
    items: list[ItemBuy] = field(default_factory=list)
    entity: int | None = None
    # hero damage dealt to enemy heroes, per game second
    damage_out: dict[int, int] = field(default_factory=dict)
    # positions: per game second -> (x, y, hp, alive)
    track: dict[int, tuple[float, float, int, bool]] = field(default_factory=dict)

    def at(self, t: float, tol: int = 2):
        s = int(round(t))
        for d in range(tol + 1):
            for c in (s - d, s + d):
                if c in self.track:
                    return self.track[c]
        return None

    def alive_at(self, t: float) -> bool:
        for d in self.deaths:
            if d.t <= t < d.t + d.duration:
                return False
        return True


class Match:
    def __init__(self, api: Api, match_id: int, use_replay: bool = True):
        self.api = api
        self.match_id = match_id
        self.meta = api.metadata(match_id)
        self.duration = self.meta["duration_s"]
        self.winning_team = self.meta["winning_team"]

        heroes = api.heroes()
        self.hero_names = {h["id"]: h["name"] for h in heroes}
        self.hero_assets = {h["id"]: h for h in heroes}
        items = api.items()
        self.items_by_id = {i["id"]: i for i in items}
        self.items_by_class = {i.get("class_name"): i for i in items if i.get("class_name")}
        # ability class name -> hero id (signature1..4)
        self.ability_owner: dict[str, int] = {}
        for h in heroes:
            for k in ("signature1", "signature2", "signature3", "signature4"):
                cn = (h.get("items") or {}).get(k)
                if cn:
                    self.ability_owner[cn] = h["id"]

        self.players: dict[int, Player] = {}
        self._load_metadata()
        self.objectives = sorted(
            [o for o in self.meta.get("objectives", []) if o.get("destroyed_time_s")],
            key=lambda o: o["destroyed_time_s"],
        )
        self.replay_ok = False
        self.replay_error: str | None = None
        if use_replay:
            try:
                self._load_replay()
                self.replay_ok = True
            except Exception as e:  # replay may be expired / unavailable
                self.replay_error = str(e)

    # ------------------------------------------------------------- metadata
    def _load_metadata(self) -> None:
        for p in self.meta["players"]:
            pl = Player(
                slot=p["player_slot"], team=p["team"], hero_id=p["hero_id"],
                hero=self.hero_names.get(p["hero_id"], str(p["hero_id"])),
                account_id=p.get("account_id", 0), kills=p["kills"], deaths_n=p["deaths"],
                assists=p["assists"], net_worth=p["net_worth"],
            )
            for d in p.get("death_details", []):
                pl.deaths.append(Death(
                    t=d["game_time_s"], killer_slot=d.get("killer_player_slot"),
                    pos=(d["death_pos"]["x"], d["death_pos"]["y"]), duration=d.get("death_duration_s", 0),
                ))
            for it in p.get("items", []):
                info = self.items_by_id.get(it["item_id"], {})
                if info.get("type") == "upgrade":
                    pl.items.append(ItemBuy(it["game_time_s"], it["item_id"], info["name"], info.get("item_tier", 0),
                                            it.get("sold_time_s") or None, it.get("imbued_ability_id") or None))
                    pl.item_buys.append((it["game_time_s"], info["name"], info.get("item_tier", 0),
                                         it.get("sold_time_s") or None))
                elif info.get("type") == "ability":
                    pl.ability_ups.append((it["game_time_s"], info.get("class_name", "")))
            for s in p.get("stats", []):
                pl.nw_by_time[s["time_stamp_s"]] = s["net_worth"]
            self.players[pl.slot] = pl

    def team(self, team: int) -> list[Player]:
        return [p for p in self.players.values() if p.team == team]

    def by_hero(self, name: str) -> Player | None:
        for p in self.players.values():
            if p.hero.lower() == name.lower():
                return p
        return None

    def all_deaths(self):
        out = []
        for p in self.players.values():
            for d in p.deaths:
                out.append((d.t, p, d))
        return sorted(out, key=lambda x: x[0])

    # --------------------------------------------------------------- replay
    def _load_replay(self) -> None:
        mid = self.match_id
        kills = self.api.demo_query(mid, SQL_KILLS, "kills")
        ctrls = self.api.demo_query(mid, SQL_CONTROLLERS, "controllers")
        important = self.api.demo_query(mid, SQL_IMPORTANT, "important")
        positions = self.api.demo_query(mid, SQL_POSITIONS, "positions")

        # tick -> game time: least squares fit of kill ticks against metadata death times
        death_times = sorted(d.t for p in self.players.values() for d in p.deaths)
        kt = [k["tick"] for k in kills]
        n = min(len(kt), len(death_times))
        if n >= 2:
            xs, ys = kt[:n], death_times[:n]
            mx, my = sum(xs) / n, sum(ys) / n
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            a = my - b * mx
        else:
            a, b = 0.0, 1 / TICKS_PER_SEC
        self._tick_a, self._tick_b = a, b

        # entity -> player via hero id (heroes are unique per match)
        hero_to_slot = {p.hero_id: p.slot for p in self.players.values()}
        for c in ctrls:
            slot = hero_to_slot.get(c.get("hero"))
            if slot is not None and c.get("pawn"):
                self.players[slot].entity = c["pawn"] & 0x3FFF
        ent_to_slot = {p.entity: p.slot for p in self.players.values() if p.entity is not None}

        for r in positions:
            slot = ent_to_slot.get(r["entity_index"])
            if slot is None or r.get("cx") is None:
                continue
            t = int(round(self.tick_to_t(r["sec"] * TICKS_PER_SEC + TICKS_PER_SEC // 2)))
            x, y = world_xy(r["cx"], r["cy"], r["vx"], r["vy"])
            self.players[slot].track[t] = (x, y, r.get("hp") or 0, r.get("life") == 0)

        # hero-to-hero damage, aggregated per second (needs the entity ids first)
        ents = sorted(ent_to_slot)
        if ents:
            ids = ", ".join(str(e) for e in ents)
            sql = ("SELECT CAST(tick / 64 AS INT) AS sec, entindex_attacker AS a, entindex_victim AS v, "
                   "SUM(damage) AS dmg FROM DamageEvent "
                   f"WHERE entindex_attacker IN ({ids}) AND entindex_victim IN ({ids}) "
                   "AND entindex_attacker <> entindex_victim GROUP BY 1, 2, 3")
            for r in self.api.demo_query(mid, sql, "hero_damage"):
                att, vic = ent_to_slot.get(r["a"]), ent_to_slot.get(r["v"])
                if att is None or vic is None or self.players[att].team == self.players[vic].team:
                    continue
                t = int(round(self.tick_to_t(r["sec"] * TICKS_PER_SEC + TICKS_PER_SEC // 2)))
                dmg = self.players[att].damage_out
                dmg[t] = dmg.get(t, 0) + (r["dmg"] or 0)

        # casts: attribute caster ids to heroes via the ability's owner
        caster_to_slot: dict[int, int] = {}
        for e in important:
            owner = self.ability_owner.get(e["ability_name"])
            if owner in hero_to_slot:
                caster_to_slot.setdefault(e["caster"], hero_to_slot[owner])
        for e in important:
            slot = caster_to_slot.get(e["caster"])
            if slot is None:
                continue
            info = self.items_by_class.get(e["ability_name"], {})
            self.players[slot].casts.append(
                Cast(self.tick_to_t(e["tick"]), e["ability_name"], info.get("name", e["ability_name"]))
            )

    def tick_to_t(self, tick: float) -> float:
        return self._tick_a + self._tick_b * tick

    # ------------------------------------------------------------- helpers
    def near(self, who: Player, t: float, radius_m: float):
        """(allies, enemies) alive within radius of `who` at time t, as (player, distance_m)."""
        me = who.at(t)
        if not me:
            return [], []
        allies, enemies = [], []
        for p in self.players.values():
            if p.slot == who.slot:
                continue
            o = p.at(t)
            if not o or not o[3] or not p.alive_at(t):
                continue
            d = dist_m(me, o)
            if d <= radius_m:
                (allies if p.team == who.team else enemies).append((p, d))
        return sorted(allies, key=lambda z: z[1]), sorted(enemies, key=lambda z: z[1])

    def networth_at(self, p: Player, t: int) -> int:
        keys = sorted(p.nw_by_time)
        i = bisect.bisect_right(keys, t) - 1
        if i < 0:
            return p.nw_by_time[keys[0]] if keys else 0
        if i + 1 < len(keys):  # interpolate between snapshots
            t0, t1 = keys[i], keys[i + 1]
            n0, n1 = p.nw_by_time[t0], p.nw_by_time[t1]
            return int(n0 + (n1 - n0) * (t - t0) / (t1 - t0))
        return p.nw_by_time[keys[i]]

    def team_networth(self, team: int, t: int) -> int:
        tot = 0
        for p in self.team(team):
            keys = sorted(p.nw_by_time)
            i = bisect.bisect_right(keys, t) - 1
            if i >= 0:
                tot += p.nw_by_time[keys[i]]
        return tot
