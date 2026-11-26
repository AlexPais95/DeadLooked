# deadlock-review

Finds the biggest mistakes in a Deadlock match, for one player and for each team, using
the public [Deadlock API](https://api.deadlock-api.com): match metadata plus SQL queries
against the match replay.

## Usage

```bash
pip install -r requirements.txt
python -m deadlock_review 143558839 --hero Dynamo --html
```

The report is written to `reports/<match_id>.md`. With `--html` you also get `reports/<match_id>.html`: a visual version with a match timeline (soul difference, fights, ultimate cooldowns, deaths, objectives), a map of your deaths, kills and objectives, ranked mistakes, comparison bars against top-tier players, and fight and objective tables. It is a single file with no scripts; open it in any browser.

| Option | Meaning |
|---|---|
| `--hero NAME` / `--account ID` | Whose mistakes to focus on (otherwise only team-level issues and a short list per player) |
| `--no-replay` | Use match metadata only (works after the replay has expired, but skips all position-based checks) |
| `--no-benchmarks` | Skip comparing item timings and ability order with top-tier players |
| `--min-badge N` | Rank used for benchmarks (default 90 = Phantom and above) |
| `--out FILE` | Where to write the report |
| `--html` | Also write the visual HTML report |
| `--wait MIN` | Keep retrying up to MIN minutes while the API fetches a new match from Steam |

Each match costs **6 replay queries**. The API allows 200 per hour per IP, and results are
cached in `.cache/`, so re-running a report is free.

## What it checks (v0.1)

**Team**
- Team fights (3+ deaths within 15 s of each other), who won them, and whether the winner took an objective within 90 s
- Objectives lost while 2+ players were dead
- Soul difference over time

**Each player**
- Deaths: alone (no ally within 30 m), outnumbered, killed during an enemy ultimate, objectives lost while dead, and whether your escape ability and ultimate were ready. For the reviewed player this reads each ability's real cooldown from the replay (`m_flCooldownStart`/`End`), which includes every upgrade, item, imbue and refund, and says when it was last used and when it came back ("Quantum Entanglement was ready for 152s and not used (last used 16:11, 19s cooldown)")
- Missing a team fight (more than 60 m from every death in it), worse when the ultimate was ready
- Ultimate quality: enemies caught, allies close enough to follow up, kills and losses in the next 15 s
- **Team fight damage:** damage to enemy heroes in each fight compared with an *expected* share, so players are judged against teammates in the same fight. The expected share uses the hero's role (damage per soul for that hero across recent matches), souls at the time, and seconds the player was actually in the fight (alive, and within 60 m or trading damage). Under 50% is flagged, 160%+ is highlighted, and short appearances aren't judged
- **Ultimate timeline:** cooldown after each cast from the ultimate's upgrades owned at the time and cooldown items (Compress Cooldown only if imbued into the ultimate), with Refresher resets
- **Rotations:**
  - *Being where you're needed:* every 5 s after 10:00, finds where your team needed help (a teammate trading damage with enemy heroes within 30 m, or 2+ enemies at one of your objectives) and whether you were there (within 60 m), fighting somewhere else, or farming 100 m+ away. Farming stretches that cost a teammate's life or an objective are flagged
  - *Objectives:* for every objective that fell (positions from the replay's `BossKilledEvent`), whether you got within 40 m in the 15 s before; own objectives lost while you were alive, 100 m+ away and not fighting are flagged
  - *Fight arrival:* for fights you had to travel to (60 m+ away 10 s before the first death), seconds until you got there
  - *Laning phase:* kills you were in on in other lanes before 10:00 (ganks), and time spent outside your lane; lanes are the three columns of the map at x = -7500 / 0 / +7500
- 3+ deaths before 10:00
- **Laning phase:** before 10:00 only your own lane counts. Fights and objectives in other lanes aren't held against you (missing them, defending them), unless you joined the fight
- Item timings and the first-maxed ability compared with top-tier players of the same hero

Findings are ranked by an **impact** score: a hand-made estimate of how much the mistake
cost the team (death timer length, objectives lost while dead, fights lost).

Hero-specific settings (ultimate radius, escape ability, key items, tips) live in
`deadlock_review/heroes.py`. Dynamo, Wraith and Ivy are configured; other heroes use defaults.

## Top-tier baselines (v2)

```bash
python -m deadlock_review.collect --heroes Dynamo,Wraith,Ivy --matches 20
```

Finds recent Phantom+ matches with each hero, skips those without a stored replay (checked
for free through the salts endpoint, since most recent matches have none), analyzes the rest (5 queries per match,
throttled to 150 per hour) and stores one row per player in `baselines/raw.jsonl`, so every
match also adds data for the other 11 heroes. `baselines/summary.json` holds percentiles per
hero and measure. When it exists, reports add a **How you compare with top-tier players**
table and flag measures where you did worse than 80% of top-tier games (once at least
15 games are collected for that hero).

Measures: deaths per 10 min, share of deaths with no ally within 30 m, team fights
attended, fight damage vs expected, ultimates per 10 min, enemies caught and allies close
per ultimate, seconds the ultimate sat ready, distance to the nearest ally, souls per
minute at 10 and 20 min, deaths before 10:00, and the rotation measures above. Lane changes
and time out of lane are *style* measures: shown next to top-tier players but never flagged.
After adding measures, `python -m deadlock_review.collect --recompute` re-runs them on every
collected match (cached queries are free).

## How it works

1. `api.py` fetches metadata, hero/item assets and benchmark stats, and runs replay SQL jobs (submit, poll, download the zstd NDJSON result).
2. `match.py` lines everything up in game time:
   - replay ticks are converted to game seconds by fitting kill ticks against the metadata death times, taking out pauses (the game clock stops during a pause while ticks keep running),
   - hero pawns are matched to players through their controller's hero ID,
   - positions use `cell * 512 - 32768 + vec` in game units (39.37 units ≈ 1 m),
   - ability casts come from `ImportantAbilityUsedEvent`.
3. `analysis.py` runs the rules and produces findings.
4. `report.py` writes the Markdown report.

## Limitations

- Ultimate cooldown includes upgrades and cooldown items, but not on-proc effects (e.g. Indomitable's refund). Items bought while the ultimate is on cooldown are applied from the next cast. Item reductions are stacked multiplicatively.
- Fight damage counts damage only. Healing, barriers and crowd control aren't credited yet, so supports can look low in close fights.
- Only abilities in `ImportantAbilityUsedEvent` are visible. Most ultimates and key abilities are there, but not every cast.
- Positions are sampled once per second.
- Abilities that move a hero (e.g. Doorman's Hotel Guest) make positions misleading; deaths during an enemy ultimate are labelled as such instead of "isolated".
- Impact scores are heuristics, not measured win probability.

## Roadmap

- **v2 – top-tier baselines:** per hero, collect Phantom+ matches and measure normal ranges (distance to teammates, enemies caught per ultimate, farm per minute, item and ability timings). Flag deviations instead of fixed thresholds.
- **v3 – win probability:** train a win-probability model on match states (time, soul difference, objectives, players alive, ultimates ready) from many matches, then score every event by how much it changed the team's chance of winning (Win Probability Added). This is the approach behind xPetu's Coachless for League of Legends. It replaces the hand-made impact score and gives an objective ranking of mistakes for each player and for the team.
