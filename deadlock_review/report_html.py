"""Visual HTML report.

`render_html(...)` returns a page fragment (title, styles, content) that works as a published
page as is; `full_document(fragment)` wraps it for opening locally in a browser.
Everything is static: charts are inline SVG built here, no scripts or external data.
"""

from __future__ import annotations

import html
import statistics

from .analysis import Finding
from .match import Match, fmt_t, lane_of
from .report import TEAM_NAMES, PERCENT_KEYS
from .analysis import visit_people

E = html.escape

GROUPS = [
    ("Rotations", ["needed_present", "needed_farming", "fight_arrival_s", "objectives_defended",
                   "objectives_taken_present", "lane_changes_per10"]),
    ("Fighting", ["fight_presence", "fight_damage_ratio", "deaths_per10", "isolated_death_share",
                  "ally_distance_m"]),
    ("Ultimate", ["ult_per10", "ult_enemies_caught", "ult_allies_close", "ult_idle_s"]),
    ("Laning & farm", ["lane_deaths", "souls_per_min_10", "souls_per_min_20", "lane_ganks", "lane_time_away"]),
]

CSS = """
/* Layout: one reading column; a wide match timeline and a map lead, details follow in cards. */
:root {
  --bg: #111417; --surface: #181c20; --raise: #1f242a; --line: #2c333a;
  --fg: #e9e6df; --muted: #9ba0a3; --brass: #d2ad62; --street: #39414a;
  --amber: #ec9440; --sapphire: #5b97e6;
  --good: #63b882; --bad: #e86b5c; --warn: #d9b44a;
  --display: "Big Shoulders Display", "Arial Narrow", Impact, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", Menlo, monospace;
  color-scheme: dark;
}
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) {
  --bg: #eceff2; --surface: #ffffff; --raise: #f5f7f9; --line: #d3d9df;
  --fg: #191d21; --muted: #59616a; --brass: #87651e; --street: #c9d0d8;
  --amber: #c06612; --sapphire: #2b67bb;
  --good: #2c8650; --bad: #c13f2c; --warn: #9c760c; color-scheme: light; } }
:root[data-theme="light"] {
  --bg: #eceff2; --surface: #ffffff; --raise: #f5f7f9; --line: #d3d9df;
  --fg: #191d21; --muted: #59616a; --brass: #87651e; --street: #c9d0d8;
  --amber: #c06612; --sapphire: #2b67bb;
  --good: #2c8650; --bad: #c13f2c; --warn: #9c760c; color-scheme: light; }

body { background: var(--bg); color: var(--fg); font: 15px/1.55 var(--body); }
.wrap { max-width: 1080px; margin: 0 auto; padding-block: 28px 64px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px));
  display: grid; gap: 40px; }
h1, h2, h3 { font-family: var(--display); font-weight: 800; letter-spacing: .01em; text-wrap: balance; margin: 0; line-height: 1.05; }
h2 { font-size: 1.9rem; text-transform: uppercase; }
h3 { font-size: 1.25rem; text-transform: uppercase; }
p { margin: 0; }
.eyebrow { font: 500 .75rem/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }
.muted { color: var(--muted); }
.num { font-family: var(--mono); font-variant-numeric: tabular-nums; }
section { display: grid; gap: 16px; min-width: 0; }
.sec-head { display: grid; gap: 6px; }
.sec-head p { color: var(--muted); max-width: 68ch; }

/* hero */
.hero { display: grid; gap: 18px; padding-bottom: 28px; border-bottom: 1px solid var(--line); }
.hero h1 { font-size: clamp(2.6rem, 8vw, 4.6rem); text-transform: uppercase; }
.hero h1 .res-win { color: var(--good); } .hero h1 .res-loss { color: var(--bad); }
.stats { display: flex; flex-wrap: wrap; gap: 10px 32px; }
.stat { display: grid; gap: 2px; }
.stat b { font: 600 1.5rem/1.1 var(--mono); font-variant-numeric: tabular-nums; }
.stat span { font: 500 .7rem/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.verdict { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }
.vcol { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; display: grid; gap: 8px; align-content: start; min-width: 0; }
.vcol h3 { font-size: 1rem; }
.vcol ul { margin: 0; padding-left: 18px; display: grid; gap: 4px; }
.vcol.bad h3 { color: var(--bad); } .vcol.good h3 { color: var(--good); }

/* charts */
.chart { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 12px; overflow-x: auto; }
.chart svg { display: block; width: 100%; min-width: 640px; height: auto; }
.chart svg text { fill: var(--muted); font: 11px var(--mono); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; font-size: .82rem; color: var(--muted); }
.legend svg.key, .chart .legend svg.key, .map .legend svg.key { display: inline-block; width: 14px; height: 14px; min-width: 0; max-width: none; margin: 0 6px 0 0; vertical-align: -2px; }
.legend i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px; }
.two { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; align-items: start; }
@media (max-width: 820px) { .two { grid-template-columns: 1fr; } }
.map svg { min-width: 0; max-width: 560px; margin: 0 auto; }

/* mistakes */
ol.mistakes { list-style: none; margin: 0; padding: 0; display: grid; gap: 10px; counter-reset: m; }
ol.mistakes li { counter-increment: m; display: grid; grid-template-columns: 2.2rem minmax(0, 1fr) 7rem; gap: 4px 14px; align-items: start;
  background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; }
ol.mistakes li::before { content: counter(m); font: 800 1.6rem/1 var(--display); color: var(--brass); }
.m-title { font-weight: 600; }
.m-detail { color: var(--muted); font-size: .9rem; grid-column: 2; }
.impact { grid-column: 3; grid-row: 1 / span 2; display: grid; gap: 4px; justify-items: end; }
.impact .bar { width: 100%; height: 6px; background: var(--raise); border-radius: 3px; overflow: hidden; }
.impact .bar i { display: block; height: 100%; background: var(--bad); }
ol.mistakes.good li { border-left: 3px solid var(--good); }
ol.mistakes.bad li { border-left: 3px solid var(--bad); }
ol.mistakes.good li::before { color: var(--good); }
ol.mistakes.good .impact .bar i { background: var(--good); }
.ranked .col { display: grid; gap: 16px; align-content: start; min-width: 0; }
.map.big svg { max-width: 760px; }
.map.big .legend { justify-content: center; margin-top: 10px; }
.impact span { font: 500 .72rem/1 var(--mono); color: var(--muted); }
@media (max-width: 560px) { ol.mistakes li { grid-template-columns: 2rem minmax(0, 1fr); }
  .impact { grid-column: 2; grid-row: auto; justify-items: start; } .impact .bar { width: 8rem; } }
ul.wins { margin: 0; padding: 0; list-style: none; display: grid; gap: 8px; }
ul.wins li { border-left: 3px solid var(--good); padding: 4px 0 4px 12px; }
ul.wins li b { font-weight: 600; } ul.wins li span { color: var(--muted); font-size: .9rem; display: block; }

/* comparison */
.cmp { display: grid; gap: 22px; }
.cmp-group { display: grid; gap: 2px; }
.cmp-group h3 { margin-bottom: 6px; color: var(--brass); }
.cmp-row { display: grid; grid-template-columns: minmax(0, 1.5fr) 5.5rem minmax(140px, 1.6fr) 8.5rem; gap: 14px; align-items: center;
  padding: 8px 0; border-top: 1px solid var(--line); }
.cmp-row .lab { min-width: 0; } .cmp-row .lab small { display: block; color: var(--muted); font-size: .78rem; }
.cmp-row .lab small.explain { color: var(--fg); opacity: .8; margin-top: 2px; }
.cmp-row .you { font: 600 1rem var(--mono); font-variant-numeric: tabular-nums; text-align: right; }
.track { position: relative; height: 14px; border-radius: 7px; background: linear-gradient(90deg,
  color-mix(in srgb, var(--bad) 28%, transparent) 0 20%, var(--raise) 20% 80%, color-mix(in srgb, var(--good) 28%, transparent) 80% 100%); }
.track.style { background: var(--raise); }
.track::after { content: ""; position: absolute; left: 50%; top: -3px; bottom: -3px; width: 1px; background: var(--muted); opacity: .6; }
.track i { position: absolute; top: 50%; box-sizing: border-box; width: 16px; height: 16px; margin: -8px 0 0 -8px; border-radius: 50%; border: 2px solid var(--bg); }
.pill { font: 600 .72rem/1 var(--mono); letter-spacing: .04em; padding: 5px 7px; border-radius: 4px; text-align: center; white-space: nowrap; }
.cmp-row .pill { justify-self: end; max-width: 100%; }
.pill.bad { background: color-mix(in srgb, var(--bad) 18%, transparent); color: var(--bad); }
.pill.good { background: color-mix(in srgb, var(--good) 18%, transparent); color: var(--good); }
.pill.mid { background: var(--raise); color: var(--muted); }
.pill.warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); }
@media (max-width: 640px) { .cmp-row { grid-template-columns: minmax(0, 1fr) auto; }
  .cmp-row .track { grid-column: 1 / -1; } .cmp-row .pill { grid-column: 2; grid-row: 1; justify-self: end; }
  .cmp-row .you { grid-column: 1; grid-row: 1; display: none; } }

/* fights */
.fsum { display: flex; flex-wrap: wrap; gap: 12px 40px; }
.fsum div { display: grid; gap: 2px; }
.fsum b { font: 600 1.8rem/1 var(--mono); } .fsum b small { font-size: 1.1rem; color: var(--muted); }
.fsum span { font: 500 .7rem/1.2 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.fights { display: grid; gap: 6px; }
.fight { display: grid; grid-template-columns: 5.5rem 8.5rem minmax(0, 1fr) 11rem; gap: 6px 18px; align-items: center;
  background: var(--surface); border: 1px solid var(--line); border-left: 4px solid var(--muted); border-radius: 6px; padding: 10px 14px; }
.fight.won { border-left-color: var(--good); } .fight.lost { border-left-color: var(--bad); }
.f-when { display: grid; gap: 4px; } .f-when b { font-size: 1.05rem; }
.res { font: 600 .68rem/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; }
.res.won { color: var(--good); } .res.lost { color: var(--bad); } .res.even { color: var(--muted); }
.pips { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; }
.pips i { width: 8px; height: 8px; border-radius: 50%; }
.pips em { font-style: normal; font-size: .82rem; color: var(--fg); margin: 0 6px; }
.f-bar { min-width: 0; display: grid; gap: 3px; }
.tug { position: relative; height: 14px; border-radius: 6px; overflow: hidden; background: var(--raise); }
.tug i { position: absolute; top: 0; bottom: 0; }
.tug .ours { left: 0; opacity: .85; } .tug .theirs { opacity: .55; }
.tug .you { left: 0; z-index: 1; background: var(--fg); border-right: 2px solid var(--surface); }
.you-lab { color: var(--fg); font-weight: 600; }
.note { font-size: .85rem; }
.tug .souls { width: 3px; margin-left: -1.5px; background: var(--warn); z-index: 2; }
.tug-lab { display: flex; justify-content: space-between; font: .7rem var(--mono); color: var(--muted); }
.f-you { display: grid; gap: 2px; justify-items: start; }
.rate { font-size: .85rem; } .rate b { font-size: 1rem; }
.rate.low b, .rate.low { color: var(--bad); } .rate.high b { color: var(--good); } .rate.ok b { color: var(--brass); }
.rate.none { color: var(--muted); font-style: italic; }
.you-sub { font-size: .72rem; }
@media (max-width: 720px) { .fight { grid-template-columns: 4.5rem minmax(0, 1fr); }
  .fight .pips { grid-column: 2; grid-row: 1; justify-self: end; } .f-bar { grid-column: 1 / -1; } .f-you { grid-column: 1 / -1; } }

/* objectives */
.objs { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; }
.obj { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 14px; align-items: start;
  background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; }
.badge { width: 64px; height: 64px; border-radius: 8px; border: 2px solid; display: grid; place-items: center;
  background: var(--raise); overflow: hidden; }
.badge svg { width: 54px; height: 54px; }
.badge.patron img { width: 100%; height: 100%; object-fit: cover; }
.obj-body { display: grid; gap: 4px; min-width: 0; }
.obj-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.obj-head h3 { font-size: 1.15rem; }
.obj-meta { font-size: .75rem; color: var(--muted); letter-spacing: .04em; }
.obj-story { font-size: .88rem; line-height: 1.45; }
.obj-you { display: flex; align-items: center; gap: 8px; font-size: .84rem; font-weight: 600; margin-top: 2px; }
.obj-you i { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
.obj.good .obj-you { color: var(--good); } .obj.good .obj-you i { background: var(--good); }
.obj.bad .obj-you { color: var(--bad); } .obj.bad .obj-you i { background: var(--bad); }
.obj.warn .obj-you { color: var(--warn); } .obj.warn .obj-you i { background: var(--warn); }
.obj.mid .obj-you { color: var(--muted); }
.obj.lost { background: color-mix(in srgb, var(--bad) 18%, var(--surface)); border-color: color-mix(in srgb, var(--bad) 40%, var(--line)); }
.obj.taken { background: color-mix(in srgb, var(--good) 18%, var(--surface)); border-color: color-mix(in srgb, var(--good) 40%, var(--line)); }
.obj .badge { background: var(--surface); }
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }

/* hero faces */
.faces { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); margin: 6px 0 4px; grid-column: 2;
  border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
.faces .half { display: grid; gap: 6px; align-content: start; padding: 7px 10px 9px; min-width: 0; }
.faces .half.mine { background: color-mix(in srgb, var(--c-mine) 10%, transparent); }
.faces .half.theirs { background: color-mix(in srgb, var(--c-theirs) 10%, transparent); border-left: 1px solid var(--line); }
.faces .half-lab { font: 600 .62rem/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.faces .side { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; }
.faces .nobody { font-size: .78rem; color: var(--muted); font-style: italic; }
.face { position: relative; width: 32px; height: 32px; flex: none; display: inline-block; --c: var(--muted); }
.face.t0 { --c: var(--amber); } .face.t1 { --c: var(--sapphire); }
.face i { display: block; width: 100%; height: 100%; border-radius: 50%; background: var(--raise) center / cover no-repeat;
  box-shadow: 0 0 0 2px var(--c); }
.face.me i { box-shadow: 0 0 0 2px var(--c), 0 0 0 5px var(--brass); }
.face.dead i { filter: grayscale(1) brightness(.5); }
.face.dead::after { content: ""; position: absolute; right: -5px; bottom: -5px; width: 16px; height: 16px; border-radius: 50%;
  border: 2px solid var(--surface); box-sizing: border-box;
  background: var(--bad) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'%3E%3Cpath d='M2.5 2.5l5 5M7.5 2.5l-5 5' stroke='white' stroke-width='1.8' stroke-linecap='round'/%3E%3C/svg%3E") center / 9px no-repeat; }
.face.mini { width: 16px; height: 16px; margin-right: 8px; vertical-align: -4px; }
.face.mini i { box-shadow: 0 0 0 2px var(--c); }
.face.mini.me i { box-shadow: 0 0 0 2px var(--c), 0 0 0 4px var(--brass); }
.face.mini.dead::after { width: 11px; height: 11px; right: -4px; bottom: -4px; background-size: 7px; border-width: 1.5px; }
.faces-key { margin-top: 4px; }
.obj .faces { grid-column: auto; margin: 6px 0 4px; }
.obj .faces .side { gap: 6px; }
.obj .face { width: 26px; height: 26px; }

/* counter items */
.ctrs { display: grid; grid-template-columns: repeat(auto-fill, minmax(min(100%, 480px), 1fr)); gap: 12px; }
.ctr { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 14px 16px; display: grid; gap: 12px; align-content: start; }
.ctr.missing { border-left: 4px solid var(--bad); } .ctr.answered { border-left: 4px solid var(--good); }
.ctr.teamcov { border-left-color: var(--muted); }
.ctr-head { display: flex; justify-content: space-between; align-items: start; gap: 10px; flex-wrap: wrap; }
.ctr-head h3 { font-size: 1.35rem; margin-top: 4px; }
.ctr-head .pill { white-space: normal; text-align: left; }
.ctr-why { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 12px; align-items: start; }
.ctr-why .side { display: flex; flex-wrap: wrap; gap: 6px; max-width: 120px; padding-top: 2px; }
.ctr-why p { font-size: .9rem; color: var(--fg); }
.ctr-items { display: grid; grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); gap: 8px; }
.it { display: grid; justify-items: center; gap: 4px; text-align: center; padding: 8px 4px; border-radius: 6px;
  background: var(--raise); border: 1px solid var(--line); --slot: var(--muted); }
.it.weapon { --slot: var(--amber); } .it.vitality { --slot: var(--good); } .it.spirit { --slot: #a77be0; }
.it .ico { width: 44px; height: 44px; border-radius: 8px; background: color-mix(in srgb, var(--slot) 22%, var(--surface)) center / 72% no-repeat;
  border: 2px solid var(--slot); }
.it b { font-size: .76rem; line-height: 1.2; font-weight: 600; }
.it small { font-size: .68rem; color: var(--muted); }
.it.owned { border-color: var(--brass); box-shadow: 0 0 0 1px var(--brass); }
.it { position: relative; }
.it em { font-style: normal; font-size: .64rem; line-height: 1.2; color: var(--muted); }
.it .best { position: absolute; top: -8px; left: 50%; transform: translateX(-50%); white-space: nowrap;
  font: 600 .6rem/1 var(--mono); letter-spacing: .06em; text-transform: uppercase; padding: 3px 6px; border-radius: 3px;
  background: var(--good); color: var(--bg); }
.ctr-items { padding-top: 6px; }
.ctr-have { display: grid; gap: 6px; font-size: .84rem; }
.ctr-have .lab { font: 600 .62rem/1 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--muted); }
.have-list { display: flex; flex-wrap: wrap; gap: 8px 14px; }
.have { display: inline-flex; align-items: center; gap: 8px; }
.have .face { width: 24px; height: 24px; }
.have small { color: var(--muted); }
.it .top { font: 600 .62rem/1.2 var(--mono); color: var(--brass); }
.it .top.none { color: var(--muted); font-weight: 500; }
.role { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 12px; font-size: .88rem; }
.it.owned small { color: var(--brass); font-weight: 600; }

/* tables */
.tbl { overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; }
table { border-collapse: collapse; width: 100%; font-size: .88rem; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--line); white-space: nowrap; }
th { font: 500 .7rem/1.2 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }
tr:last-child td { border-bottom: 0; }
td.num, th.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
.ratio { display: inline-grid; grid-template-columns: 70px auto; gap: 8px; align-items: center; }
.ratio .bar { height: 6px; background: var(--raise); border-radius: 3px; position: relative; overflow: hidden; }
.ratio .bar i { position: absolute; inset: 0 auto 0 0; }
.ratio .bar::after { content: ""; position: absolute; left: 50%; top: 0; bottom: 0; width: 1px; background: var(--muted); }
details { background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 10px 14px; }
details + details { margin-top: 8px; }
summary { cursor: pointer; font-weight: 600; }
summary:focus-visible { outline: 2px solid var(--brass); outline-offset: 3px; }
details ul { margin: 8px 0 0; padding-left: 18px; color: var(--muted); font-size: .9rem; display: grid; gap: 3px; }
.t0 { color: var(--amber); } .t1 { color: var(--sapphire); }
footer { color: var(--muted); font-size: .8rem; border-top: 1px solid var(--line); padding-top: 16px; }
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@700;800'
         '&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">')


def _team_var(team: int) -> str:
    return "var(--amber)" if team == 0 else "var(--sapphire)"


def _fmt_val(key: str, v: float) -> str:
    if v is None:
        return "—"
    if key in PERCENT_KEYS:
        return f"{v:.0%}"
    if key.endswith("_m"):
        return f"{v:.0f} m"
    if key.endswith("_s"):
        return f"{v:.0f} s"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.0f}"


# ------------------------------------------------------------------ timeline
def _timeline(m: Match, p, fights, extras) -> str:
    W, PADL, PADR = 1000, 54, 12
    top, ch = 16, 150                       # soul-difference area
    rows = ["Fights", "Ultimate", "Deaths", "Objectives"]
    row_y0, row_h = top + ch + 30, 26
    H = row_y0 + row_h * len(rows) + 26
    dur = m.duration
    X = lambda t: PADL + (W - PADL - PADR) * t / dur
    ts = list(range(0, dur + 1, 15))
    diffs = [m.team_networth(p.team, t) - m.team_networth(1 - p.team, t) for t in ts]
    lim = max(1000, max(abs(d) for d in diffs) * 1.1)
    Y = lambda d: top + ch / 2 - (ch / 2) * d / lim
    pts = " ".join(f"{X(t):.1f},{Y(d):.1f}" for t, d in zip(ts, diffs))
    zero = Y(0)
    area = f"{X(0):.1f},{zero:.1f} {pts} {X(ts[-1]):.1f},{zero:.1f}"
    me, them = _team_var(p.team), _team_var(1 - p.team)
    s = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Match timeline">',
         '<defs><clipPath id="above"><rect x="0" y="0" width="%d" height="%.1f"/></clipPath>'
         '<clipPath id="below"><rect x="0" y="%.1f" width="%d" height="%d"/></clipPath></defs>' % (W, zero, zero, W, H)]
    # grid
    for t in range(0, dur + 1, 300):
        s.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{top}" y2="{H - 20}" stroke="var(--line)" stroke-width="1"/>'
                 f'<text x="{X(t):.1f}" y="{H - 6}" text-anchor="middle">{t // 60}:00</text>')
    step = 5000 if lim > 12000 else (2000 if lim > 5000 else 1000)
    for v in range(-int(lim // step) * step, int(lim) + 1, step):
        if v == 0:
            continue
        s.append(f'<text x="{PADL - 8}" y="{Y(v) + 4:.1f}" text-anchor="end">{v / 1000:+.0f}k</text>'
                 f'<line x1="{PADL}" x2="{W - PADR}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="var(--line)" stroke-dasharray="2 4"/>')
    s.append(f'<polygon points="{area}" fill="{me}" fill-opacity=".35" clip-path="url(#above)"/>'
             f'<polygon points="{area}" fill="{them}" fill-opacity=".35" clip-path="url(#below)"/>'
             f'<polyline points="{pts}" fill="none" stroke="var(--fg)" stroke-width="1.6"/>'
             f'<line x1="{PADL}" x2="{W - PADR}" y1="{zero:.1f}" y2="{zero:.1f}" stroke="var(--muted)"/>'
             f'<text x="{PADL - 8}" y="{zero + 4:.1f}" text-anchor="end">0</text>'
             f'<text x="{PADL + 6}" y="{top + 12}" style="fill:{me}">your team ahead</text>'
             f'<text x="{PADL + 6}" y="{top + ch - 4}" style="fill:{them}">enemy ahead</text>')
    # rows
    for i, name in enumerate(rows):
        y = row_y0 + i * row_h
        s.append(f'<text x="{PADL - 8}" y="{y + 15}" text-anchor="end">{name}</text>'
                 f'<rect x="{PADL}" y="{y + 3}" width="{W - PADL - PADR}" height="{row_h - 6}" fill="var(--raise)" rx="3"/>')
    yF, yU, yD, yO = (row_y0 + i * row_h for i in range(4))
    for f in fights:
        col = "var(--muted)" if f.winner is None else ("var(--good)" if f.winner == p.team else "var(--bad)")
        res = "even" if f.winner is None else ("won" if f.winner == p.team else "lost")
        x0, x1 = X(f.deaths[0][0] - 3), X(f.end + 3)
        s.append(f'<rect x="{x0:.1f}" y="{yF + 6}" width="{max(4, x1 - x0):.1f}" height="{row_h - 12}" rx="2" fill="{col}">'
                 f'<title>Fight {fmt_t(f.deaths[0][0])}–{fmt_t(f.end)}: {res}, deaths {f.losses.get(0, 0)}/{f.losses.get(1, 0)}</title></rect>')
    for (t, cd, notes) in (extras.get("ult") or {}).get(p.slot, []):
        end = min(dur, t + (cd or 0))
        s.append(f'<rect x="{X(t):.1f}" y="{yU + 10}" width="{max(1, X(end) - X(t)):.1f}" height="{row_h - 20}" fill="var(--muted)" fill-opacity=".45">'
                 f'<title>On cooldown {fmt_t(t)}–{fmt_t(end)}</title></rect>'
                 f'<path d="M{X(t):.1f},{yU + 4} l5,9 h-3 l3,9 l-8,-11 h3 z" fill="var(--brass)"><title>Ultimate at {fmt_t(t)}</title></path>')
    for d in p.deaths:
        x = X(d.t)
        killer = m.players.get(d.killer_slot)
        s.append(f'<g stroke="var(--bad)" stroke-width="2.4" stroke-linecap="round"><title>Died at {fmt_t(d.t)}'
                 f'{" to " + E(killer.hero) if killer else ""}</title>'
                 f'<line x1="{x - 5:.1f}" y1="{yD + 8}" x2="{x + 5:.1f}" y2="{yD + 18}"/><line x1="{x + 5:.1f}" y1="{yD + 8}" x2="{x - 5:.1f}" y2="{yD + 18}"/></g>')
    for o in m.objective_kills:
        x, lost = X(o.t), o.team == p.team
        col = "var(--bad)" if lost else "var(--good)"
        s.append(f'<rect x="{x - 4:.1f}" y="{yO + 9}" width="8" height="8" transform="rotate(45 {x:.1f} {yO + 13})" fill="{col}">'
                 f'<title>{fmt_t(o.t)} {"lost" if lost else "took"} {o.kind}</title></rect>')
    s.append("</svg>")
    return "".join(s)


# ----------------------------------------------------------------------- map
def _key(kind: str, col: str = "") -> str:
    """Legend symbol drawn the same way as on the map."""
    body = {
        "death": '<path d="M3,3 L11,11 M11,3 L3,11" stroke="var(--bad)" stroke-width="2.6" stroke-linecap="round"/>',
        "kill": '<circle cx="7" cy="7" r="4.5" fill="var(--good)"/>',
        "obj": f'<rect x="2" y="2" width="10" height="10" fill="{col}" stroke="{col}" stroke-width="2"/>',
        "dead": f'<rect x="2" y="2" width="10" height="10" fill="var(--bg)" stroke="{col}" stroke-width="2"/>',
    }[kind]
    return f'<svg class="key" viewBox="0 0 14 14" width="14" height="14" aria-hidden="true">{body}</svg>'


def _map(m: Match, p) -> str:
    """Your deaths, kills and every objective on the real minimap (from the assets API).
    Map coordinates: left = 0.5 + x / 2R, top = 0.5 - y / 2R, so team 0's base is at the bottom."""
    import base64
    from .rotations import objective_spots
    S = 520
    try:
        R, png = m.api.map_image()
        img = base64.b64encode(png).decode()
    except Exception:
        R, img = 10752.0, None
    X = lambda x: S * (0.5 + x / (2 * R))
    Y = lambda y: S * (0.5 - y / (2 * R))
    s = [f'<svg viewBox="0 0 {S} {S}" role="img" aria-label="Minimap with your deaths, kills and objectives">',
         f'<rect x="0" y="0" width="{S}" height="{S}" rx="8" fill="var(--bg)"/>']
    if img:
        s.append(f'<mask id="streets" style="mask-type:alpha" maskUnits="userSpaceOnUse" x="0" y="0" width="{S}" height="{S}">'
                 f'<image href="data:image/png;base64,{img}" x="0" y="0" width="{S}" height="{S}"/></mask>'
                 f'<rect x="0" y="0" width="{S}" height="{S}" fill="var(--street)" mask="url(#streets)"/>')
    else:
        for lx in (-7500, 0, 7500):
            s.append(f'<line x1="{X(lx):.1f}" y1="{Y(-9600):.1f}" x2="{X(lx):.1f}" y2="{Y(9600):.1f}" '
                     'stroke="var(--street)" stroke-width="14" stroke-linecap="round"/>')
    for team, y in ((1, 16), (0, S - 8)):
        name = TEAM_NAMES.get(team, f"team {team}").split(" (")[0]
        who = "your base" if team == p.team else "enemy base"
        s.append(f'<text x="10" y="{y}" style="fill:{_team_var(team)}">{E(name)} · {who}</text>')
    for owner, kind, pos, dead_t in objective_spots(m):
        x, y = X(pos[0]), Y(pos[1])
        col = _team_var(owner)
        fill = "var(--bg)" if dead_t != float("inf") else col
        r = {"Guardian": 5, "Walker": 7, "Base Guardian": 4, "Shrine": 6, "Patron": 9}.get(kind, 5)
        when = f"destroyed {fmt_t(dead_t)}" if dead_t != float("inf") else "standing at the end"
        s.append(f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" fill="{fill}" stroke="{col}" stroke-width="2">'
                 f'<title>{"Your" if owner == p.team else "Enemy"} {kind}, {when}</title></rect>')
    for q in m.players.values():
        for d in q.deaths:
            if d.killer_slot == p.slot and d.pos:
                s.append(f'<circle cx="{X(d.pos[0]):.1f}" cy="{Y(d.pos[1]):.1f}" r="5" fill="var(--good)" stroke="var(--bg)" stroke-width="1.5">'
                         f'<title>You killed {E(q.hero)} at {fmt_t(d.t)}</title></circle>')
    for d in p.deaths:
        if not d.pos:
            continue
        x, y = X(d.pos[0]), Y(d.pos[1])
        s.append(f'<g stroke-linecap="round"><title>You died at {fmt_t(d.t)}</title>'
                 f'<path d="M{x - 6:.1f},{y - 6:.1f} L{x + 6:.1f},{y + 6:.1f} M{x + 6:.1f},{y - 6:.1f} L{x - 6:.1f},{y + 6:.1f}" stroke="var(--bg)" stroke-width="6"/>'
                 f'<path d="M{x - 6:.1f},{y - 6:.1f} L{x + 6:.1f},{y + 6:.1f} M{x + 6:.1f},{y - 6:.1f} L{x - 6:.1f},{y + 6:.1f}" stroke="var(--bad)" stroke-width="3"/></g>'
                 f'<text x="{x + 9:.1f}" y="{y + 4:.1f}" style="fill:var(--fg);paint-order:stroke;stroke:var(--bg);stroke-width:3px">{fmt_t(d.t)}</text>')
    s.append("</svg>")
    return "".join(s)


# --------------------------------------------------------------- comparison
STYLE_NOTES = {
    "lane_changes_per10": "How often you moved to a different lane and stayed there 20 s or more.",
    "lane_time_away": "Share of the laning phase you spent outside your own lane (roaming, ganking, camps).",
}


def _comparison(rows: list[dict], hero: str) -> str:
    by = {r["key"]: r for r in rows}
    out = ['<div class="cmp">']
    for name, keys in GROUPS:
        items = [by[k] for k in keys if k in by]
        if not items:
            continue
        out.append(f'<div class="cmp-group"><h3>{name}</h3>')
        for r in items:
            bt, style = r["better_than"], r["better_than"] is None
            pos = r["pct"] if style else bt
            if style:
                word = "less than most" if pos < 0.25 else ("more than most" if pos > 0.75 else "typical")
                pill, col = f'<span class="pill mid">{word}</span>', "var(--muted)"
            elif bt < 0.2:
                pill, col = f'<span class="pill bad">worse than {1 - bt:.0%}</span>', "var(--bad)"
            elif bt >= 0.8:
                pill, col = f'<span class="pill good">better than {bt:.0%}</span>', "var(--good)"
            else:
                pill, col = f'<span class="pill mid">better than {bt:.0%}</span>', "var(--brass)"
            out.append(
                f'<div class="cmp-row"><div class="lab">{E(r["label"])}'
                + (f'<small class="explain">{E(STYLE_NOTES.get(r["key"], ""))} Playstyle: no right or wrong value, shown for comparison.</small>' if style else "")
                + f'<small>top-tier median {_fmt_val(r["key"], r["p50"])}'
                f' · middle half {_fmt_val(r["key"], r["p25"])}–{_fmt_val(r["key"], r["p75"])}</small></div>'
                f'<div class="you">{_fmt_val(r["key"], r["you"])}</div>'
                f'<div class="track{" style" if style else ""}" title="{"higher than" if style else "better than"} {pos:.0%} of top-tier {E(hero)} games">'
                f'<i style="left:{pos * 100:.1f}%;background:{col}"></i></div>{pill}</div>')
        out.append("</div>")
    out.append("</div>")
    return "".join(out)


def _verdict(rows: list[dict]) -> tuple[list[str], list[str]]:
    judged = [r for r in rows if r["better_than"] is not None]
    worst = sorted([r for r in judged if r["better_than"] < 0.2], key=lambda r: r["better_than"])[:4]
    best = sorted([r for r in judged if r["better_than"] >= 0.8], key=lambda r: -r["better_than"])[:4]
    w = [f'{E(r["label"])}: <b class="num">{_fmt_val(r["key"], r["you"])}</b> vs {_fmt_val(r["key"], r["p50"])}' for r in worst]
    b = [f'{E(r["label"])}: <b class="num">{_fmt_val(r["key"], r["you"])}</b> vs {_fmt_val(r["key"], r["p50"])}' for r in best]
    return w, b


# ------------------------------------------------------------------- fights
def _fights(m: Match, p, fights, extras) -> str:
    """Summary strip, then one row per fight: result, kills/deaths, a tug-of-war bar of each
    team's damage (your own part highlighted, souls share marked) and your rating."""
    rows = extras.get("fight_damage") or []
    team_dmg = {(id(ff), team): (dmg, share) for ff, team, dmg, share in (extras.get("team_damage") or [])}
    from .rotations import in_scope
    me_by_fight = {id(r.fight): r for r in rows if r.player.slot == p.slot}

    def counts(f):  # other-lane laning fights count only if you joined them
        r = me_by_fight.get(id(f))
        return in_scope(p, f.deaths[0][0], f.center[0]) or (r is not None and (r.present_s > 0 or r.damage > 0))
    hidden = [f for f in fights if not counts(f)]
    fights = [f for f in fights if counts(f)]
    won = sum(1 for f in fights if f.winner == p.team)
    lost = sum(1 for f in fights if f.winner is not None and f.winner != p.team)
    attended = [r for r in me_by_fight.values() if r.present_s > 0 or r.damage > 0]
    ratios = [r.ratio for r in attended if r.ratio is not None]
    med = statistics.median(ratios) if ratios else None
    me_col, them_col = _team_var(p.team), _team_var(1 - p.team)

    out = ['<div class="fsum">',
           f'<div><b class="num">{won}<small>–{lost}</small></b><span>Fights won–lost'
           f'{f" · {len(fights) - won - lost} even" if len(fights) - won - lost else ""}</span></div>',
           f'<div><b class="num">{len(attended)}<small>/{len(fights)}</small></b><span>Fights you were in</span></div>',
           f'<div><b class="num">{"—" if med is None else f"{med:.0%}"}</b><span>Your damage vs expected (median)</span></div>',
           '</div><div class="fights">']
    for f in fights:
        k, d = f.losses.get(1 - p.team, 0), f.losses.get(p.team, 0)
        res, cls = ("Even", "even") if f.winner is None else (("Won", "won") if f.winner == p.team else ("Lost", "lost"))
        mine_t, theirs_t = team_dmg.get((id(f), p.team)), team_dmg.get((id(f), 1 - p.team))
        r = me_by_fight.get(id(f))
        # tug of war
        if mine_t and theirs_t and (mine_t[0] + theirs_t[0]) > 0:
            tot = mine_t[0] + theirs_t[0]
            ours = mine_t[0] / tot * 100
            you = (r.damage / tot * 100) if r else 0
            souls = mine_t[1] * 100
            bar = (f'<div class="tug" title="Your team {mine_t[0]:,} damage ({ours:.0f}%), enemy {theirs_t[0]:,}. '
                   f'Your team had {souls:.0f}% of the souls.">'
                   f'<i class="ours" style="width:{ours:.1f}%;background:{me_col}"></i>'
                   + (f'<i class="you" style="width:{you:.1f}%"></i>' if you > 0 else "")
                   + f'<i class="theirs" style="left:{ours:.1f}%;width:{100 - ours:.1f}%;background:{them_col}"></i>'
                   f'<i class="souls" style="left:{souls:.1f}%"></i></div>'
                   f'<div class="tug-lab"><span>Your team {ours:.0f}%'
                   + (f' · <b class="you-lab">you {you:.0f}%</b>' if r and r.damage else "")
                   + f'</span><span>enemy {100 - ours:.0f}%</span></div>')
        else:
            bar = '<div class="tug empty"></div><div class="tug-lab"><span class="muted">no damage data</span></div>'
        # you
        if not r or (r.present_s == 0 and r.damage == 0):
            you_html = '<span class="rate none">not there</span>'
        elif r.ratio is None:
            you_html = f'<span class="rate none">{r.damage:,} dmg · too short</span>'
        else:
            rc = "low" if r.ratio < 0.5 else ("high" if r.ratio >= 1.6 else "ok")
            you_html = (f'<span class="rate {rc}"><b class="num">{r.ratio:.0%}</b> of expected</span>'
                        f'<span class="muted num you-sub">{r.damage:,} dmg · {r.present_s}s in fight</span>')
        pips = ('<span class="pips" title="Enemies killed / your team\'s deaths">'
                + "".join(f'<i style="background:{them_col}"></i>' for _ in range(k))
                + '<em class="num">' + f"{k}–{d}" + "</em>"
                + "".join(f'<i style="background:{me_col}"></i>' for _ in range(d)) + "</span>")
        out.append(f'<div class="fight {cls}"><div class="f-when"><b class="num">{fmt_t(f.deaths[0][0])}</b>'
                   f'<span class="res {cls}">{res}</span></div>{pips}<div class="f-bar">{bar}</div>'
                   f'<div class="f-you">{you_html}</div></div>')
    if hidden:
        out.append(f'<p class="muted note">{len(hidden)} early fight(s) in other lanes before 10:00 that you weren\'t in are left out: '
                   'during the laning phase only your own lane counts.</p>')
    out.append('</div><div class="legend"><span><i style="background:var(--fg)"></i>your own damage</span>'
               f'<span><i style="background:{me_col}"></i>your team</span><span><i style="background:{them_col}"></i>enemy team</span>'
               '<span><i style="background:var(--warn)"></i>tick: your team\'s share of souls (even fight = damage meets the tick)</span>'
               '<span>dots: enemies killed – your team\'s deaths</span></div>')
    return "".join(out)


NUMERALS = {"Guardian": "I", "Walker": "II", "Base Guardian": "III", "Shrine": "IV"}


def _join(names) -> str:
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _badge(m: Match, kind: str, owner: int) -> str:
    """Patron: the team's mask from the assets API. Other objectives: a tier badge in the team colour."""
    import base64
    col = _team_var(owner)
    if kind == "Patron":
        try:
            got = m.api.patron_image(owner)
        except Exception:
            got = None
        if got:
            data, mime = got
            return (f'<div class="badge patron" style="border-color:{col}">'
                    f'<img src="data:{mime};base64,{base64.b64encode(data).decode()}" alt="{E(TEAM_NAMES.get(owner, "").split(" (")[0])} Patron"></div>')
    num = NUMERALS.get(kind, "?")
    return (f'<div class="badge" style="border-color:{col}"><svg viewBox="0 0 64 64" aria-hidden="true">'
            f'<path d="M32 4 L56 14 V34 C56 47 45 56 32 60 C19 56 8 47 8 34 V14 Z" fill="{col}" fill-opacity=".18" stroke="{col}" stroke-width="2.5"/>'
            f'<text x="32" y="41" text-anchor="middle" style="fill:{col};font:800 22px var(--display)">{num}</text></svg></div>')


def _objective_story(v, p) -> str:
    if v.lost:
        who = _join(v.enemies) or "The enemy"
        line = f"{who} took it" + (f" against {_join(v.allies)}" if v.allies else ", with nobody defending") + "."
    else:
        team = list(v.allies) + ([p.hero] if v.there else [])
        line = f"{_join(team) or 'Your team'} took it" + (f" against {_join(v.enemies)}" if v.enemies else ", unopposed") + "."
    if v.deaths:
        mine = [h for h, t in v.deaths if t == p.team]
        theirs = [h for h, t in v.deaths if t != p.team]
        bits = []
        if theirs:
            bits.append(f"{_join(theirs)} died")
        if mine:
            bits.append(f"we lost {_join(mine)}")
        text = "; ".join(bits)
        line += " " + text[0].upper() + text[1:] + " there."
    return E(line)


def _objective_cards(m: Match, p, visits) -> str:
    shown = [v for v in visits if v.in_scope]
    hidden = len(visits) - len(shown)
    lost = [v for v in shown if v.lost and v.alive and v.kind != "Patron"]
    defended = sum(1 for v in lost if v.there)
    far = sum(1 for v in lost if not v.there and not v.busy and (v.closest_m or 0) > 100)
    taken = [v for v in shown if not v.lost and v.alive and v.kind != "Patron"]
    out = ['<div class="fsum">',
           f'<div><b class="num">{defended}<small>/{len(lost)}</small></b><span>Lost objectives you were at</span></div>',
           f'<div><b class="num">{sum(1 for v in taken if v.there)}<small>/{len(taken)}</small></b><span>Taken objectives you were at</span></div>',
           f'<div><b class="num" style="color:var(--bad)">{far}</b><span>Lost while you were far and not fighting</span></div>',
           '</div><div class="objs">']
    for v in shown:
        owner = p.team if v.lost else 1 - p.team
        if not v.alive:
            st, sc = "You were dead", "mid"
        elif v.closest_m is None:
            st, sc = "No position data for you", "mid"
        elif v.there:
            st, sc = "You were there", "good"
        elif v.busy:
            st, sc = f"You were fighting {v.closest_m:.0f} m away", "warn"
        elif v.lost and v.closest_m > 100:
            st, sc = f"You were {v.closest_m:.0f} m away, not fighting", "bad"
        else:
            st, sc = f"You were {v.closest_m:.0f} m away", "mid"
        res = "lost" if v.lost else "taken"
        where = "base" if v.lane == "base" else v.lane
        out.append(f'<article class="obj {res} {sc}">{_badge(m, v.kind, owner)}<div class="obj-body">'
                   f'<div class="obj-head"><h3>{E(v.kind)}<span class="sr"> ({res})</span></h3></div>'
                   f'<div class="obj-meta num">{fmt_t(v.t)} · {E(where)}</div>'
                   + _faces(visit_people(m, p, v), p.team)
                   + f'<div class="obj-you"><i></i>{E(st)}</div></div></article>')
    out.append('</div><div class="legend"><span><i style="background:color-mix(in srgb, var(--good) 45%, var(--surface))"></i>your team took it</span>'
               '<span><i style="background:color-mix(in srgb, var(--bad) 45%, var(--surface))"></i>your team lost it</span></div>')
    out.append(FACES_KEY)
    if hidden:
        out.append(f'<p class="muted note">{hidden} objective(s) in other lanes before 10:00 are left out: '
                   'during the laning phase only your own lane counts.</p>')
    return "".join(out)


# --------------------------------------------------------------------- page
def _slug(hero: str) -> str:
    return "".join(ch for ch in hero.lower() if ch.isalnum())


def _portrait_css(m: Match) -> str:
    """One CSS class per hero in the match with its portrait, so each image is embedded once."""
    import base64
    rules = []
    for q in m.players.values():
        try:
            got = m.api.hero_portrait(q.hero_id)
        except Exception:
            got = None
        if got:
            data, mime = got
            rules.append(f".face i.h-{_slug(q.hero)}{{background-image:url(data:{mime};base64,{base64.b64encode(data).decode()})}}")
    return "".join(rules)


def _face(h, team, died, you) -> str:
    label = f"{h}{' (you)' if you else ''}{' - died' if died else ''}"
    return (f'<span class="face t{team}{" dead" if died else ""}{" me" if you else ""}" title="{E(label)}" '
            f'role="img" aria-label="{E(label)}"><i class="h-{_slug(h)}"></i></span>')


def _faces(people, my_team: int) -> str:
    """Your side, 'vs', the enemy side. Died = greyed with a red cross; you = gold ring."""
    if not people:
        return ""
    face = _face
    ours = [x for x in people if x[1] == my_team]
    theirs = [x for x in people if x[1] != my_team]
    def half(xs, label, cls):
        body = "".join(face(*x) for x in xs)  # nobody there: the half stays empty
        return f'<div class="half {cls}"><span class="half-lab">{label}</span><div class="side">{body}</div></div>'
    out = ['<div class="faces">', half(ours, "Your team", "mine"), half(theirs, "Enemy", "theirs"), "</div>"]
    return "".join(out)


FACES_KEY = ('<div class="legend faces-key"><span><span class="face t0 me mini"><i></i></span>gold ring = you</span>'
             '<span><span class="face t1 dead mini"><i></i></span>greyed with a red cross = died there</span>'
             '<span>ring colour = team</span></div>')


def _ranked(items, tone: str, my_team: int = 0) -> str:
    """Numbered cards with an impact bar: mistakes (red) or big plays (green)."""
    top = max((f.impact for f in items), default=1) or 1
    out = [f'<ol class="mistakes {tone}">']
    for f in items:
        out.append(f'<li><div class="m-title">{E(f.title)}</div>'
                   f'<div class="impact"><div class="bar"><i style="width:{f.impact / top * 100:.0f}%"></i></div>'
                   f'<span>{"impact" if tone == "bad" else "value"} {f.impact:.1f}</span></div>'
                   + (_faces(f.people, my_team) if f.people else "")
                   + ((lambda d: f'<div class="m-detail">{E(d)}</div>' if d else "")
                      ((f.short if f.people else f.detail).strip().rstrip(";").strip()))
                   + "</li>")
    out.append("</ol>")
    return "".join(out)


def _item_icon(m: Match, item: dict) -> str:
    import base64
    try:
        got = m.api.item_image(item)
    except Exception:
        got = None
    if not got:
        return '<span class="ico"></span>'
    data, mime = got
    return f'<span class="ico" style="background-image:url(data:{mime};base64,{base64.b64encode(data).decode()})"></span>'


DELIVERY_TAG = {"spirit": "works off your spirit damage", "gun": "works off your bullets", "melee": "works off melee",
                "active": "active: cast it", "any": "passive"}


def _counters(m: Match, p, needs, role=None) -> str:
    from .counters import delivery
    out = ['<div class="ctrs">']
    for n in needs:
        owned = {b[0]: b for b in n.bought}
        items = n.items[:5]   # already ranked for your build
        items += [i for i in n.items if i["name"] in owned and i not in items]
        if n.bought:
            name, t, sold = n.bought[0]
            status = f'<span class="pill good">You bought {E(name)} at {fmt_t(t)}</span>'
            cls = "answered"
        elif n.covered:
            status = '<span class="pill mid">Teammates had it</span>'
            cls = "answered teamcov"
        else:
            status = '<span class="pill bad">You didn\'t buy one</span>'
            cls = "missing"
        level = "Clear need" if n.strength >= 0.6 else ("Worth considering" if n.strength >= 0.3 else "Mostly covered")
        tiles = []
        for k, i in enumerate(items):
            b = owned.get(i["name"])
            best = '<span class="best">best fit</span>' if k == 0 and role is not None else ""
            sub = f"bought {fmt_t(b[1])}" if b else f"T{i.get('item_tier')} · {i.get('cost', 0):,}"
            tp = n.top.get(i["id"])
            top = (f'<span class="top" title="Bought in {tp[0]:.0%} of ~{n.top_n:,} top-tier {E(p.hero)} games against these enemies, '
                   f'on average at {fmt_t(tp[1])}; {tp[2]:.0%} of those games were won">{tp[0]:.0%} of top {E(p.hero)}s<br>~{fmt_t(tp[1])}</span>'
                   if tp and tp[0] >= 0.005 else f'<span class="top none">rare at top tier</span>')
            tiles.append(f'<div class="it {E(i.get("item_slot_type") or "")}{" owned" if b else ""}" '
                         f'title="{E(i["name"])}: tier {i.get("item_tier")}, {i.get("cost", 0):,} souls">'
                         f'{best}{_item_icon(m, i)}<b>{E(i["name"])}</b>'
                         f'<small class="num">{sub}</small>{top}<em>{DELIVERY_TAG[delivery(i)]}</em></div>')
        out.append(f'<article class="ctr {cls}"><div class="ctr-head"><div><span class="eyebrow">{level}</span>'
                   f'<h3>{E(n.title)}</h3></div>{status}</div>'
                   f'<div class="ctr-why"><span class="side">{"".join(_face(*h) for h in n.heroes)}</span>'
                   f'<p>{E(n.why.rstrip("."))}' + (f'. Clear from about {fmt_t(n.since)}.' if n.since else ".") + '</p></div>'
                   + (f'<div class="ctr-have"><span class="lab">You already had</span><p>{E(n.own.replace("You already had ", ""))}</p></div>' if n.own else "")
                   + (f'<div class="ctr-have"><span class="lab">Already on your team</span><div class="have-list">'
                      + "".join(f'<span class="have">{_face(h, t, False, False)}<span>{E(it)} <small class="num">{fmt_t(bt)}</small></span></span>'
                                for h, t, it, bt in n.covered) + '</div></div>' if n.covered else "")
                   + f'<div class="ctr-items">{"".join(tiles)}</div></article>')
    out.append("</div>")
    return "".join(out)


def render_html(m: Match, fights, findings: list[Finding], focus_slots: set[int], extras: dict | None = None,
                top_n: int = 8) -> str:
    extras = extras or {}
    focus = [m.players[s] for s in focus_slots if s in m.players]
    if not focus:
        raise ValueError("the HTML report needs a focus player (--hero or --account)")
    p = focus[0]
    won = m.winning_team == p.team
    team_players = sorted(m.team(p.team), key=lambda q: -q.net_worth)
    rank = [q.slot for q in team_players].index(p.slot) + 1
    base = (extras.get("baseline") or {}).get(p.slot) or []
    n_games = max((r["n"] for r in base), default=0)
    worst, best = _verdict(base)
    bad = sorted([f for f in findings if f.slot == p.slot and not f.good], key=lambda f: -f.impact)
    good = [f for f in findings if f.slot == p.slot and f.good]
    max_imp = max((f.impact for f in bad[:top_n]), default=1)

    L = [f"<title>{E(p.hero)} · Match {m.match_id}</title>",
         '<meta name="claude:viewport-fit-sides" content="cover">', FONTS, f"<style>{CSS}{_portrait_css(m)}</style>",
         f'<div class="wrap" style="--c-mine:{_team_var(p.team)};--c-theirs:{_team_var(1 - p.team)}">']

    # hero
    L.append(f'<header class="hero"><span class="eyebrow">Match {m.match_id} · {fmt_t(m.duration)} · '
             f'<span class="t{p.team}">{E(TEAM_NAMES.get(p.team, "team").split(" (")[0])}</span></span>'
             f'<h1>{E(p.hero)} · <span class="{"res-win" if won else "res-loss"}">{"Victory" if won else "Defeat"}</span></h1>'
             '<div class="stats">'
             f'<div class="stat"><b>{p.kills}/{p.deaths_n}/{p.assists}</b><span>K / D / A</span></div>'
             f'<div class="stat"><b>{p.net_worth:,}</b><span>Souls · #{rank} on team</span></div>'
             f'<div class="stat"><b>{len([c for c in (extras.get("ult") or {}).get(p.slot, [])])}</b><span>Ultimates</span></div>'
             f'<div class="stat"><b>{len(fights)}</b><span>Team fights</span></div></div>')
    if worst or best:
        L.append('<div class="verdict">')
        if worst:
            L.append('<div class="vcol bad"><h3>Fix first</h3><ul>' + "".join(f"<li>{x}</li>" for x in worst) + "</ul></div>")
        if best:
            L.append('<div class="vcol good"><h3>Top-tier already</h3><ul>' + "".join(f"<li>{x}</li>" for x in best) + "</ul></div>")
        L.append("</div>")
        L.append(f'<p class="muted">Compared with {n_games} recent {E(p.hero)} games at Phantom rank and above.</p>')
    L.append("</header>")

    # timeline
    L.append('<section><div class="sec-head"><span class="eyebrow">How the game went</span><h2>Match timeline</h2>'
             '<p>Soul difference between the teams, with every fight, your ultimates and their cooldowns, your deaths '
             'and every objective that fell. Hover any mark for details.</p></div>'
             f'<div class="chart">{_timeline(m, p, fights, extras)}</div>'
             '<div class="legend"><span><i style="background:var(--good)"></i>fight won / objective taken</span>'
             '<span><i style="background:var(--bad)"></i>fight lost / objective lost / your death</span>'
             '<span><i style="background:var(--muted)"></i>even fight / ultimate on cooldown</span>'
             '<span><i style="background:var(--brass)"></i>ultimate cast</span></div></section>')

    # map: its own row
    if m.replay_ok:
        L.append('<section><div class="sec-head"><span class="eyebrow">Where it happened</span><h2>Map</h2>'
                 '<p>Your deaths and kills, and every objective, on the game\'s minimap. Hover a mark for details.</p></div>'
                 '<div class="chart map big">' + _map(m, p) + '<div class="legend">'
                 f'<span>{_key("death")}your deaths</span><span>{_key("kill")}your kills</span>'
                 f'<span>{_key("obj", _team_var(p.team))}your objectives</span>'
                 f'<span>{_key("obj", _team_var(1 - p.team))}enemy objectives</span>'
                 f'<span>{_key("dead", "var(--muted)")}destroyed objective</span></div></div></section>')

    # mistakes and big plays, side by side
    plays = sorted(good, key=lambda f: -f.impact)
    L.append('<section><div class="two ranked">'
             '<div class="col"><div class="sec-head"><span class="eyebrow">Ranked by estimated cost to your team</span>'
             '<h2>Biggest mistakes</h2></div>' + _ranked(bad[:top_n], "bad", p.team)
             + (f'<p class="muted note">{len(bad) - top_n} smaller issues are in the Markdown report.</p>' if len(bad) > top_n else "")
             + '</div><div class="col"><div class="sec-head"><span class="eyebrow">Ranked by estimated value to your team</span>'
             '<h2>Big plays</h2></div>'
             + (_ranked(plays[:top_n], "good", p.team) if plays else '<p class="muted">No standout plays found this game.</p>')
             + '</div></div>' + FACES_KEY + '</section>')

    # counter items
    needs = (extras.get("counters") or {}).get(p.slot) or []
    role = (extras.get("role") or {}).get(p.slot)
    if needs:
        L.append('<section><div class="sec-head"><span class="eyebrow">Based on what the enemy team did this game</span>'
                 '<h2>Items to answer them</h2><p>Each card is something the enemy team did that an item counters, with the numbers behind it. '
                 'Cards are less important when you or a teammate already had an answer. Items are ranked for your role and by how often '
                 f'top-tier {E(p.hero)} players buy them against these enemies (and when).</p></div>'
                 + (f'<div class="role"><span class="pill mid">Your role: {E(role.label)}</span>'
                    f'<span class="muted">{E(role.describe())}. {E(role.ranking_note())}. Items that don\'t work with your damage are left out.</span></div>'
                    if role else "")
                 + _counters(m, p, needs, role) + "</section>")

    # comparison
    if base:
        L.append(f'<section><div class="sec-head"><span class="eyebrow">You vs {n_games} top-tier {E(p.hero)} games</span>'
                 '<h2>How you compare</h2><p>The dot shows the share of top-tier games you beat on each measure: '
                 'the left fifth is flagged, the right fifth means top-tier. The centre line is their median. '
                 'Grey dots are playstyle measures with no right or wrong value: they show where you sit '
                 '(fewer, typical or more than top players) without judging it.</p></div>'
                 + _comparison(base, p.hero) + "</section>")

    # fights + objectives
    if fights:
        L.append('<section><div class="sec-head"><span class="eyebrow">Damage to enemy heroes vs what your hero, souls and time in the fight predict</span>'
                 '<h2>Team fights</h2><p>Perfect fights don\'t exist: under 50% of expected is flagged, 160% and up is excellent.</p></div>'
                 + _fights(m, p, fights, extras) + "</section>")
    visits = (extras.get("objectives") or {}).get(p.slot)
    if visits:
        L.append('<section><div class="sec-head"><span class="eyebrow">Rotations</span><h2>Objectives</h2>'
                 '<p>Every objective that fell, who fought over it, and where you were in the 15 seconds before '
                 '("there" = within 40 m).</p></div>'
                 + _objective_cards(m, p, visits) + "</section>")

    # other players
    L.append('<section><div class="sec-head"><span class="eyebrow">Everyone else</span><h2>Other players</h2></div><div>')
    for team in (p.team, 1 - p.team):
        for q in m.team(team):
            if q.slot in focus_slots:
                continue
            qb = sorted([f for f in findings if f.slot == q.slot and not f.good], key=lambda f: -f.impact)[:3]
            tag = "ally" if team == p.team else "enemy"
            L.append(f'<details><summary><span class="t{team}">{E(q.hero)}</span> <span class="muted num">'
                     f'{q.kills}/{q.deaths_n}/{q.assists} · {q.net_worth:,} souls · {tag}</span></summary><ul>'
                     + ("".join(f"<li>{E(f.title)}</li>" for f in qb) or "<li>No major issues found.</li>") + "</ul></details>")
    L.append("</div></section>")
    L.append('<footer>Made by DeadLooked from the public Deadlock API (match data and replay). Positions are sampled '
             'once per second; impact scores are estimates, not measured win probability.</footer></div>')
    return "\n".join(L)


def full_document(fragment: str) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f"{fragment}</html>")  # the fragment's first <div> closes <head> and opens <body>
