"""Command line: python -m deadlock_review MATCH_ID [--hero NAME | --account ID]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import json

from .analysis import HIGH_BADGE, Finding, analyze, load_benchmarks
from .metrics import compare_to_baseline
from .api import Api
from .match import Match
from .report import render


def _fmt(v: float) -> str:
    return f"{v:.0%}" if 0 <= v <= 1 and not float(v).is_integer() else f"{v:,.1f}"


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
    ap.add_argument("--baselines", default="baselines/summary.json", help="top-tier baseline summary (from collect)")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--html", action="store_true", help="also write a visual HTML report (reports/<match>.html)")
    ap.add_argument("--wait", type=int, default=0, metavar="MIN",
                    help="keep retrying for up to MIN minutes if the match must still be fetched from Steam")
    a = ap.parse_args(argv)

    api = Api(a.cache, verbose=not a.quiet)
    if a.wait:
        api.busy_retries = max(4, a.wait // 5)
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
    summary_path = Path(a.baselines)
    if focus and summary_path.exists():
        summary = json.loads(summary_path.read_text())
        extras["baseline"] = {s: compare_to_baseline(m, m.players[s], summary, fights, extras.get("fight_damage"))
                              for s in focus}
        extras["baseline_min_badge"] = a.min_badge
        for s, rows in extras["baseline"].items():
            for r in rows:
                if r["better_than"] is not None and r["better_than"] < 0.2 and r["n"] >= 15:
                    p = m.players[s]
                    findings.append(Finding(s, p.team, 0, "below_baseline", 1.5,
                                            f"{r['label']}: {_fmt(r['you'])} - worse than {1 - r['better_than']:.0%} "
                                            f"of top-tier {p.hero} games (their median {_fmt(r['p50'])})"))
    md = render(m, fights, findings, focus, extras)
    out = Path(a.out or f"reports/{a.match_id}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"Report written to {out}")
    if a.html and focus:
        from .report_html import full_document, render_html
        hout = out.with_suffix(".html")
        hout.write_text(full_document(render_html(m, fights, findings, focus, extras)))
        print(f"Visual report written to {hout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
