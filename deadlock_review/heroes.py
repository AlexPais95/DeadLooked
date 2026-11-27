"""Hero-specific knowledge the generic rules can't infer from the data.

ult_radius_m       how close enemies must be to count as "caught" by the ultimate
ult_kind           "aoe" (wants many enemies, team follow-up), "target" (single target),
                   "self" (self-buff / mobility), "ally" (cast on a teammate)
escape_slot        which ability slot (signature1-4) is the hero's escape, if any
key_items          items whose timing matters most for this hero
"""

DEFAULT = {"ult_radius_m": 12, "ult_kind": "aoe", "escape_slot": None, "key_items": []}

HEROES = {
    "Dynamo": {
        "ult_radius_m": 10,
        "ult_kind": "aoe",
        "escape_slot": "signature2",  # Quantum Entanglement
        "key_items": ["Mystic Expansion", "Rapid Recharge", "Warp Stone", "Unstoppable", "Refresher"],
        "tips": {
            "ult_no_allies": "Singularity only wins fights if your team can hit the stunned enemies. "
                             "Warp Stone/Quantum in when allies are within ~15 m, or bring them with Quantum's alt-cast.",
            "ult_held": "With Singularity ready, stay with your team - a Dynamo with ult up decides the next fight.",
        },
    },
    "Wraith": {
        "ult_radius_m": 12,
        "ult_kind": "target",
        "escape_slot": "signature2",  # Project Mind
        "key_items": ["Tesla Bullets", "Ricochet", "Mercurial Magnum", "Spiritual Overflow", "Dispel Magic"],
        "tips": {
            "isolated_death": "Wraith is fragile - keep Project Mind for escaping and fight from behind your frontline.",
        },
    },
    "Ivy": {
        "ult_radius_m": 20,
        "ult_kind": "ally",
        "escape_slot": None,
        "key_items": ["Titanic Magazine", "Tesla Bullets", "Superior Duration", "Healing Tempo"],
        "tips": {
            "isolated_death": "Ivy is weak alone - stay tethered to a teammate and use Air Drop to rotate, not to dive alone.",
        },
    },
}


_AUTO: dict = {}


def _meters(v) -> float | None:
    import re
    m = re.match(r"\s*([\d.]+)\s*m?\s*$", str(v.get("value") if isinstance(v, dict) else v))
    return float(m.group(1)) if m else None


def auto_cfg(api, hero: str) -> dict:
    """Settings for any hero, from the ability data (assets API):
    ult_radius_m  the ultimate's largest radius property (Radius, ImpactRadius, VacuumRadius, ...)
    ult_kind      "target" for unit-targeted ultimates, "aoe" when it has a radius, otherwise "self"
    escape_slot   the first of abilities 1-3 that moves you or hides you (movement flag, or teleport,
                  dash, invisible, untargetable in its description)"""
    import re
    if hero in _AUTO:
        return _AUTO[hero]
    out = {}
    try:
        h = next(x for x in api.heroes() if x["name"] == hero)
        items = {i.get("class_name"): i for i in api.items()}
    except Exception:
        _AUTO[hero] = out
        return out
    slots = h.get("items") or {}

    def desc(i):
        d = i.get("description")
        return re.sub(r"<[^>]+>|\s+", " ", d.get("desc", "") if isinstance(d, dict) else str(d or ""))
    ult = items.get(slots.get("signature4"), {})
    radii = [_meters(v) for k, v in (ult.get("properties") or {}).items()
             if re.search(r"Radius$", k) and not re.search(r"Min|Spotted|Bullet|Initial", k)]
    radii = [r for r in radii if r and 1 < r <= 30]          # > 30 m is a travel range, not an area
    beh = " ".join(ult.get("behaviours") or [])
    if "UNIT_TARGET" in beh:
        out["ult_kind"] = "target"
    elif re.search(r"\ball(y|ies)\b|teammate", desc(ult), re.I) and not radii:
        out["ult_kind"] = "ally"
    elif radii and max(radii) >= 5:
        out["ult_kind"] = "aoe"
    elif re.search(r"\benemies\b", desc(ult), re.I):
        out["ult_kind"] = "aoe"                                  # hits several enemies (Goo Ball)
        radii = [10.0]
    elif radii:
        out["ult_kind"] = "target"                               # tiny radius: hits one hero (Assassinate)
    else:
        out["ult_kind"] = "self"
    if radii and out["ult_kind"] == "aoe":
        out["ult_radius_m"] = max(radii)

    def escape_score(i):
        d = desc(i)
        if re.search(r"teleport|untargetable|(becom\w*|turn|go)\s+invisible", d, re.I):
            return 3
        if "MOVEMENT" in " ".join(i.get("behaviours") or []):
            return 2
        return 1 if re.search(r"\bdash|\bleap", d, re.I) else 0
    scored = [(escape_score(items.get(slots.get(sl), {})), sl) for sl in ("signature1", "signature2", "signature3")]
    best = max(scored, key=lambda x: x[0])
    if best[0] > 0:
        out["escape_slot"] = best[1]
    _AUTO[hero] = out
    return out


def cfg(hero: str, api=None) -> dict:
    """Hand-written settings (with tips) where we have them, otherwise worked out from the ability data."""
    out = dict(DEFAULT)
    if hero not in HEROES and api is not None:
        out.update(auto_cfg(api, hero))
    out.update(HEROES.get(hero, {}))
    return out
