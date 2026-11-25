# deadlock-review

Finds the biggest mistakes in a Deadlock match, for one player and for each team, using
the public [Deadlock API](https://api.deadlock-api.com): match metadata plus SQL queries
against the match replay.

## Usage

```bash
pip install -r requirements.txt
python -m deadlock_review 143558839 --hero Dynamo
```

The report is written to `reports/<match_id>.md`.

| Option | Meaning |
|---|---|
| `--hero NAME` / `--account ID` | Whose mistakes to focus on (otherwise only team-level issues and a short list per player) |
| `--no-replay` | Use match metadata only (works after the replay has expired, but skips all position-based checks) |
| `--no-benchmarks` | Skip comparing item timings and ability order with top-tier players |
| `--min-badge N` | Rank used for benchmarks (default 90 = Phantom and above) |
| `--out FILE` | Where to write the report |

Each match costs **4 replay queries**. The API allows 200 per hour per IP, and results are
cached in `.cache/`, so re-running a report is free.

## What it checks (v0.1)

**Team**
- Team fights (3+ deaths within 15 s of each other), who won them, and whether the winner took an objective within 90 s
- Objectives lost while 2+ players were dead
- Soul difference over time

**Each player**
- Deaths: alone (no ally within 30 m), outnumbered, killed during an enemy ultimate, died with escape or ultimate available, objectives lost while dead
- Missing a team fight (more than 60 m from every death in it), worse when the ultimate was ready
- Ultimate quality: enemies caught, allies close enough to follow up, kills and losses in the next 15 s
- 3+ deaths before 10:00
- Item timings and the first-maxed ability compared with top-tier players of the same hero

Findings are ranked by an **impact** score: a hand-made estimate of how much the mistake
cost the team (death timer length, objectives lost while dead, fights lost).

Hero-specific settings (ultimate radius, escape ability, key items, tips) live in
`deadlock_review/heroes.py`. Dynamo, Wraith and Ivy are configured; other heroes use defaults.

## How it works

1. `api.py` fetches metadata, hero/item assets and benchmark stats, and runs replay SQL jobs (submit, poll, download the zstd NDJSON result).
2. `match.py` lines everything up in game time:
   - replay ticks are converted to game seconds by fitting kill ticks against the metadata death times,
   - hero pawns are matched to players through their controller's hero ID,
   - positions use `cell * 512 - 32768 + vec` in game units (39.37 units ≈ 1 m),
   - ability casts come from `ImportantAbilityUsedEvent`.
3. `analysis.py` runs the rules and produces findings.
4. `report.py` writes the Markdown report.

## Limitations

- Ultimate availability uses the **base cooldown**, ignoring cooldown reduction, so "ultimate ready" is conservative.
- Only abilities in `ImportantAbilityUsedEvent` are visible. Most ultimates and key abilities are there, but not every cast.
- Positions are sampled once per second.
- Abilities that move a hero (e.g. Doorman's Hotel Guest) make positions misleading; deaths during an enemy ultimate are labelled as such instead of "isolated".
- Impact scores are heuristics, not measured win probability.

## Roadmap

- **v2 – top-tier baselines:** per hero, collect Phantom+ matches and measure normal ranges (distance to teammates, enemies caught per ultimate, farm per minute, item and ability timings). Flag deviations instead of fixed thresholds.
- **v3 – win probability:** train a win-probability model on match states (time, soul difference, objectives, players alive, ultimates ready) from many matches, then score every event by how much it changed the team's chance of winning (Win Probability Added). This is the approach behind xPetu's Coachless for League of Legends. It replaces the hand-made impact score and gives an objective ranking of mistakes for each player and for the team.
