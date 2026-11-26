"""How often top-tier players are in team fights, by role / hero and game time.

    python -m deadlock_review.expectations

Carries spend a lot of the early and mid game farming side lanes and taking objectives alone,
so a missed fight is not the same mistake for every hero. From the collected top-tier matches
(baselines/raw.jsonl, replays already cached), for every team fight and every player who was alive
for it: was the player in it? Averaged by role (carry / frontliner / support, see counters.hero_roles)
and by hero, in 5-minute windows. Analysis uses this to weight "not in the fight" and "farming while
your team needed you": they count in proportion to how often a top player in your role would have
been there at that time.

Writes baselines/fight_expectations.json.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import Api, ApiError

OUT = Path("baselines/fight_expectations.json")
BUCKET_S = 300
MAX_BUCKET = 8            # 40:00+ in the last window
MIN_SAMPLES_HERO = 40


def _bucket(t: float) -> int:
    return min(MAX_BUCKET, int(t // BUCKET_S))


def build(cache: str = ".cache", raw: str = "baselines/raw.jsonl") -> dict:
    from .analysis import _present_seconds, find_fights
    from .counters import hero_roles
    from .match import Match
    from .rotations import in_scope

    api = Api(cache, verbose=False)
    real = api.demo_query

    def cached_only(match_id, sql, name):      # never spend replay queries here
        cp = api._cache_path(f"demo:{match_id}:{name}:{sql}", ".ndjson")
        if not cp.exists():
            raise ApiError("not cached")
        return real(match_id, sql, name)
    api.demo_query = cached_only

    roles = hero_roles(api)
    ids = sorted({json.loads(line)["match_id"] for line in Path(raw).read_text().splitlines() if line})
    acc: dict = {}
    used = 0
    for mid in ids:
        try:
            m = Match(api, mid)
        except Exception:
            continue
        if not m.replay_ok:
            continue
        used += 1
        for f in find_fights(m):
            t0 = f.deaths[0][0]
            for q in m.players.values():
                if not (q.alive_at(t0 - 5) and q.alive_at(f.end)):
                    continue
                if not in_scope(q, t0, f.center[0]):
                    continue                      # other lanes in the laning phase never count
                there = _present_seconds(m, q, f) > 0 or any(x[1].slot == q.slot for x in f.deaths)
                role = roles.get(q.hero_id, ("Core", ""))[0]
                b = str(_bucket(t0))
                for key in (f"role:{role}", f"hero:{q.hero_id}"):
                    a = acc.setdefault(key, {}).setdefault(b, [0, 0])
                    a[0] += 1
                    a[1] += 1 if there else 0
        # smaller moments where teammates needed help (rotations.presence_when_needed): farming far away?
        from .rotations import presence_when_needed
        for q in m.players.values():
            role = roles.get(q.hero_id, ("Core", ""))[0]
            for t, st, _, _ in presence_when_needed(m, q):
                b = str(_bucket(t))
                for key in (f"farm_role:{role}", f"farm_hero:{q.hero_id}"):
                    a = acc.setdefault(key, {}).setdefault(b, [0, 0])
                    a[0] += 1
                    a[1] += 1 if st == "farming" else 0
    out = {"matches": used, "bucket_s": BUCKET_S, "rates": {}}
    for key, buckets in acc.items():
        out["rates"][key] = {b: {"n": n, "rate": k / n} for b, (n, k) in buckets.items()}
    OUT.write_text(json.dumps(out, indent=1))
    return out


_CACHE = None


def expected_farming(hero_id: int, role: str | None, t: float) -> float | None:
    """Share of 'a teammate needs help' moments in which top-tier players like you were farming 100 m+ away."""
    v = _rate("farm_", hero_id, role, t)
    return v[0] if v else None


def _rate(prefix: str, hero_id: int, role: str | None, t: float):
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(OUT.read_text()) if OUT.exists() else {}
    rates = _CACHE.get("rates", {})
    b = str(_bucket(t))
    r_role = (rates.get(f"{prefix}role:{role}", {}) or {}).get(b) if role else None
    r_hero = (rates.get(f"{prefix}hero:{hero_id}", {}) or {}).get(b)
    if r_hero and r_role:
        w = r_hero["n"] / (r_hero["n"] + MIN_SAMPLES_HERO)
        return w * r_hero["rate"] + (1 - w) * r_role["rate"], "hero"
    r = r_hero or r_role
    return (r["rate"], "hero" if r_hero else "role") if r else None


def expected_presence(hero_id: int, role: str | None, t: float) -> tuple[float, str] | None:
    """(share of fights a top-tier player like you joins at this time, where the number comes from).
    Uses the hero's own rate when there are enough samples, otherwise the role's (smoothed towards it)."""
    global _CACHE
    if _CACHE is None:
        _CACHE = json.loads(OUT.read_text()) if OUT.exists() else {}
    rates = _CACHE.get("rates", {})
    b = str(_bucket(t))
    r_role = (rates.get(f"role:{role}", {}) or {}).get(b) if role else None
    r_hero = (rates.get(f"hero:{hero_id}", {}) or {}).get(b)
    if r_role is None and r_hero is None:
        return None
    if r_hero and r_role:
        w = r_hero["n"] / (r_hero["n"] + MIN_SAMPLES_HERO)     # shrink small hero samples to the role
        return w * r_hero["rate"] + (1 - w) * r_role["rate"], f"top-tier {role.lower()}s and this hero"
    r = r_hero or r_role
    return r["rate"], ("this hero" if r_hero else f"top-tier {role.lower()}s")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review.expectations")
    ap.add_argument("--cache", default=".cache")
    a = ap.parse_args(argv)
    out = build(a.cache)
    print(f"{out['matches']} matches")
    print("share of team fights joined (when alive), by role and game time:")
    print("role        " + "  ".join(f"{b * 5:>2}-{b * 5 + 5:<2}m" for b in range(MAX_BUCKET + 1)))
    for role in ("Carry", "Frontliner", "Support"):
        r = out["rates"].get(f"role:{role}", {})
        print(f"{role:11} " + "  ".join(f"{r[str(b)]['rate']:5.0%}  " if str(b) in r else "   -   " for b in range(MAX_BUCKET + 1)))
    print("samples     " + "  ".join(f"{out['rates'].get('role:Carry', {}).get(str(b), {}).get('n', 0):5}  " for b in range(MAX_BUCKET + 1)))
    print("\nwhen a teammate needed help: share of those moments spent farming 100 m+ away")
    for role in ("Carry", "Frontliner", "Support"):
        r = out["rates"].get(f"farm_role:{role}", {})
        print(f"{role:11} " + "  ".join(f"{r[str(b)]['rate']:5.0%}  " if str(b) in r else "   -   " for b in range(MAX_BUCKET + 1)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
