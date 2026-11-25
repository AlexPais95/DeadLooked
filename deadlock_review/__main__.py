"""Command line: python -m deadlock_review MATCH_ID [--hero NAME | --account ID]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .analysis import HIGH_BADGE, analyze, load_benchmarks
from .api import Api
from .match import Match
from .report import render


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deadlock_review", description="Find the biggest mistakes in a Deadlock match.")
    ap.add_argument("match_id", type=int)
    who = ap.add_mutually_exclusive_group()
    who.add_argument("--hero", help="hero you played, e.g. Dynamo")
    who.add_argument("--account", type=int, help="your Steam account id (SteamID3)")
    ap.add_argument("--no-replay", action="store_true", help="only use match metadata (no replay queries)")
    ap.add_argument("--no-benchmarks", action="store_true", help="skip top-tier item/ability comparisons")
    ap.add_argument("--min-badge", type=int, default=HIGH_BADGE, help="rank used for benchmarks (default 90 = Phantom+)")
    ap.add_argument("--out", help="output markdown file (default reports/<match>.md)")
    ap.add_argument("--cache", default=".cache")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    api = Api(a.cache, verbose=not a.quiet)
    m = Match(api, a.match_id, use_replay=not a.no_replay)
    if m.replay_error:
        print(f"[warn] replay unavailable: {m.replay_error}", file=sys.stderr)

    focus = set()
    if a.hero:
        p = m.by_hero(a.hero)
        if not p:
            print(f"No {a.hero} in this match. Heroes: {', '.join(p.hero for p in m.players.values())}", file=sys.stderr)
            return 2
        focus.add(p.slot)
    elif a.account:
        focus |= {p.slot for p in m.players.values() if p.account_id == a.account}

    bench = {}
    if focus and not a.no_benchmarks:
        bench = load_benchmarks(m, {m.players[s].hero_id for s in focus}, a.min_badge)

    fights, findings, extras = analyze(m, focus, bench)
    md = render(m, fights, findings, focus, extras)
    out = Path(a.out or f"reports/{a.match_id}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"Report written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
