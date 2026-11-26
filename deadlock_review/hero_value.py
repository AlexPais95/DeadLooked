"""Value of a soul per hero.

    python -m deadlock_review.hero_value

Extends the win probability model (winprob.py) with one extra soul value per hero: the base model
values every soul on a team the same; here each hero gets a correction for souls on that hero,
early and late in the game. The corrections are pulled towards 0 (ridge) so heroes with few games
stay close to average unless the data clearly says otherwise, and each comes with a 95% margin.

The held-out check (fit on 80% of matches, Brier score on the other 20%) says whether the per-hero
values predict results better than one value for everyone.

Writes baselines/hero_soul_value.json.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

import numpy as np

from .api import Api
from .winprob import _terms, state

OUT = Path("baselines/hero_soul_value.json")


def _souls(pl: dict, t: float) -> float:
    pts = [(0, 0)] + [tuple(x) for x in pl["nw"]]
    prev = pts[0]
    for cur in pts[1:]:
        if cur[0] >= t:
            return prev[1] + (cur[1] - prev[1]) * (t - prev[0]) / ((cur[0] - prev[0]) or 1)
        prev = cur
    return pts[-1][1]


BASE_KEEP = (0, 1, 5, 6, 7, 8, 9, 10, 11)   # _terms without the three team soul-lead terms


def _rows(matches, heroes, step):
    """Per row: game state without the team soul lead, then per hero (sign = which team has it):
    present, present x time (how strong the hero is), souls/1k, souls/1k x time (what its souls are worth)."""
    col = {h: i for i, h in enumerate(heroes)}
    X, y = [], []
    for m in matches:
        for t in range(120, m["dur"], step):
            tn = t / 1800
            full = _terms(t, state(m, 0, t))
            base = [full[i] for i in BASE_KEEP]
            other = 0.0      # souls on heroes too rare to get their own value
            extra = np.zeros(4 * len(heroes), dtype=np.float32)
            for pl in m["p"]:
                sign = 1 if pl["t"] == 0 else -1
                v = sign * _souls(pl, t) / 1000
                i = col.get(pl["h"])
                if i is None:
                    other += v
                    continue
                extra[4 * i] += sign
                extra[4 * i + 1] += sign * tn
                extra[4 * i + 2] += v
                extra[4 * i + 3] += v * tn
            X.append(np.concatenate([np.asarray(base + [other, other * tn], dtype=np.float32), extra]))
            y.append(1.0 if m["win"] == 0 else 0.0)
    return np.array(X, dtype=np.float64), np.array(y), len(BASE_KEEP) + 2


def _fit(X, y, nb, ridge_base=1.0, ridge_hero=5.0):
    lam = np.full(X.shape[1], ridge_hero)
    lam[:nb] = ridge_base
    b = np.zeros(X.shape[1])
    for _ in range(60):
        p = 1 / (1 + np.exp(-X @ b))
        H = X.T @ (X * (p * (1 - p))[:, None]) + np.diag(lam)
        st = np.linalg.solve(H, X.T @ (y - p) - lam * b)
        b += st
        if np.abs(st).max() < 1e-8:
            break
    cov = np.linalg.inv(H)
    return b, cov


def _base_rows(matches, step):
    X, y = [], []
    for m in matches:
        for t in range(120, m["dur"], step):
            X.append(_terms(t, state(m, 0, t)))
            y.append(1.0 if m["win"] == 0 else 0.0)
    return np.array(X, dtype=np.float64), np.array(y)


def _brier(b, X, y):
    p = 1 / (1 + np.exp(-X @ b))
    return float(np.mean((p - y) ** 2))


def run(cache: str, store_name: str = "winprob_matches_v4.jsonl", min_games: int = 400, step: int = 150) -> dict:
    store = Path(cache) / store_name
    matches = []
    for line in store.read_text().splitlines():
        try:
            matches.append(json.loads(line))
        except ValueError:
            pass   # a line still being written by the collector
    games: dict[int, int] = {}
    for m in matches:
        for pl in m["p"]:
            games[pl["h"]] = games.get(pl["h"], 0) + 1
    heroes = sorted(h for h, n in games.items() if n >= min_games)
    random.Random(7).shuffle(matches)
    cut = int(len(matches) * 0.8)
    Xtr, ytr, nb = _rows(matches[:cut], heroes, step)
    Xte, yte, _ = _rows(matches[cut:], heroes, step)
    Btr, bytr = _base_rows(matches[:cut], step)
    Bte, byte = _base_rows(matches[cut:], step)
    b0, _ = _fit(Btr, bytr, Btr.shape[1])                      # one soul value for everyone
    # heroes present (strength) + one shared soul value: souls columns summed into "other"
    pres = [nb + 4 * i + k for i in range(len(heroes)) for k in (0, 1)]
    souls_cols = [nb + 4 * i + k for i in range(len(heroes)) for k in (2, 3)]

    def presence_only(X):
        Z = X[:, :nb + 0].copy()
        Z[:, nb - 2] += X[:, [nb + 4 * i + 2 for i in range(len(heroes))]].sum(axis=1)
        Z[:, nb - 1] += X[:, [nb + 4 * i + 3 for i in range(len(heroes))]].sum(axis=1)
        return np.hstack([Z, X[:, pres]])
    b1, _ = _fit(presence_only(Xtr), ytr, nb)
    b2, _ = _fit(Xtr, ytr, nb)
    briers = {"one_soul_value": _brier(b0, Bte, byte),
              "plus_hero_strength": _brier(b1, presence_only(Xte), yte),
              "plus_hero_soul_values": _brier(b2, Xte, yte)}
    # final values on all matches
    X = np.vstack([Xtr, Xte])
    yy = np.concatenate([ytr, yte])
    b, cov = _fit(X, yy, nb)
    names = {h["id"]: h["name"] for h in Api(cache, verbose=False).heroes()}
    w = np.array([games[h] for h in heroes], dtype=float)

    out = {"matches": len(matches), "brier": briers, "heroes": {},
           # the fitted model, used by wpa.py: base terms (BASE_KEEP of winprob._terms), souls on other heroes
           # (+ x time), then per hero: present, present x time, souls/1k, souls/1k x time
           "model": {"hero_ids": heroes, "base_keep": list(BASE_KEEP), "coef": [float(v) for v in b]}}
    for label, minute in (("early", 10), ("mid", 20), ("late", 35)):
        tn = minute * 60 / 1800
        coefs = np.array([b[nb + 4 * i + 2] + b[nb + 4 * i + 3] * tn for i in range(len(heroes))])
        avg = float((coefs * w).sum() / w.sum())        # games-weighted average soul value
        for i, h in enumerate(heroes):
            g = np.zeros(len(b))
            g[nb + 4 * i + 2], g[nb + 4 * i + 3] = 1, tn
            se = float(math.sqrt(max(0.0, g @ cov @ g)))
            row = out["heroes"].setdefault(str(h), {"name": names.get(h, str(h)), "games": games[h]})
            row[label] = {"multiplier": coefs[i] / avg, "margin": 1.96 * se / abs(avg),
                          "win_per_1k": 100 * (1 / (1 + math.exp(-coefs[i])) - 0.5),
                          "avg_win_per_1k": 100 * (1 / (1 + math.exp(-avg)) - 0.5)}
    OUT.write_text(json.dumps(out, indent=1))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review.hero_value")
    ap.add_argument("--cache", default=".cache")
    ap.add_argument("--min-games", type=int, default=400)
    a = ap.parse_args(argv)
    out = run(a.cache, min_games=a.min_games)
    br = out["brier"]
    print(f"{out['matches']:,} matches. Held-out Brier (lower is better): one soul value {br['one_soul_value']:.5f} | "
          f"+ hero strength {br['plus_hero_strength']:.5f} | + per-hero soul values {br['plus_hero_soul_values']:.5f}")
    rows = sorted(out["heroes"].values(), key=lambda r: -r["late"]["multiplier"])
    print(f"{'hero':16} {'games':>6}  {'10 min':>14}  {'20 min':>14}  {'35 min':>14}")
    for r in rows:
        cell = lambda k: f"{r[k]['multiplier']:.2f}x ±{r[k]['margin']:.2f}"
        print(f"{r['name']:16} {r['games']:6}  {cell('early'):>14}  {cell('mid'):>14}  {cell('late'):>14}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
