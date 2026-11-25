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


def cfg(hero: str) -> dict:
    out = dict(DEFAULT)
    out.update(HEROES.get(hero, {}))
    return out
