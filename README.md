# deadlock-review

Finds the biggest mistakes in a Deadlock match, for one player and for each team, using
the public [Deadlock API](https://api.deadlock-api.com): match metadata plus SQL queries
against the match replay.

## Usage

```bash
pip install -r requirements.txt
python -m deadlock_review 143558839 --hero Dynamo --html
```

The report is written to `reports/<match_id>.md`. With `--html` you also get `reports/<match_id>.html`: a visual version with a match timeline (soul difference, fights, ultimate cooldowns, deaths, objectives), a map of your deaths, kills and objectives, ranked mistakes and big plays (ultimates that won exchanges, multi-kills, objectives taken with you there, standout fight damage), comparison bars against top-tier players, and fight and objective tables. It is a single file with no scripts; open it in any browser.

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
- **Items to answer the enemy team** (`counters.py`): anti-heal when an enemy healed more per minute than 80% of players in the collected top-tier games; spirit or bullet resist when 60%+ of the damage you took was that type (replay damage types); spirit or bullet shred when 60%+ of your damage was that type and enemies built 25%+ of the matching resist; silence, disarm or stun for their carry (the richer enemy with the most damage to your team). Items are picked from the item assets by what they do (property values and descriptions), so the lists follow patches. Options are ranked for your role. The role is what your hero is meant to do at top tier, from 30 days of Phantom+ hero averages (souls, hero damage, damage taken, max health, compared across all heroes) and the healing/barrier the hero gives teammates in the collected games: carry, frontliner or support. The report also says how you played it this game. Your damage type (spirit, gun, hybrid) decides which items work at all; each item is classed by how it delivers its effect (your spirit damage, your bullets, melee, an active, or passive). Supports get cheaper items, actives and team-wide items first; frontliners get vitality and close-range items; carries get bigger scaling items; items costing more than a quarter of your souls when the need was clear rank lower. Within that, items are ranked by how often top-tier players of your hero buy them against the same enemies (`item-stats` with `enemy_hero_ids`), shown with the typical buy time. Needs you already answered count less: resist you already had from items, and anti-heal, shred or crowd control a teammate already bought (shown with their portrait, item and time). Each need says whether and when you bought an answer; clear needs you never answered are listed as mistakes

Findings are ranked by **Win Probability Added**: how much each one changed your team's chance of winning (see below).

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

## Item value above expectation (first step towards v3)

```bash
python -m deadlock_review.winprob --matches 5000
```

Raw item win rates mislead: teams that are already winning rarely buy counter items, and teams that
are behind buy them and still lose more often, so counters look bad and late "luxury" items look
great. Following xPetu's approach (judge a decision against the chance of winning at the moment it
was made, i.e. Win Probability Added), this:

1. collects recent Phantom+ matches through the bulk metadata endpoint (winner, item purchase times,
   each player's souls every few minutes; no replay queries),
2. fits a win-probability model: P(win) from the team's soul lead, one logistic regression per
   5-minute window (checked on held-out matches: predicted 20-30% at 20:00 → 28% actual, 90-100% at
   30:00 → 98%),
3. scores every purchase as result minus win chance at the moment of purchase, averaged per item
   (and per item and hero): **wins per 100 above expected**.

Example (5,000 matches): Inhibitor wins 54.7% raw but was bought at 57.3% expected (−2.5);
Spirit Burn 55.6% raw vs 55.4% expected (+0.2); Bullet Resilience 51.7% raw vs 48.4% (+3.3).
The counter-item cards show this number, treat differences inside the 95% margin as "no clear
effect", and use clear effects in the ranking. It is still not proof of cause (who buys an item
also differs), but it removes the biggest bias: being ahead or behind when it was bought.

## Win Probability Added for every event (v3)

`wpa.py` uses the same model, extended with objectives and deaths: a team's chance of winning from
its soul lead (souls every 10 s from the replay), Guardians / Walkers / Base Guardians / Shrines /
the Patron's first phase taken minus lost, and heroes alive minus theirs. One logistic model for the
whole game: objectives have one value each, souls and heroes alive change value with game time.
Fitted on 5,000 Phantom+ matches (held-out Brier 0.176; soul lead alone 0.181).

What it learned, from an even game: +1,000 souls is worth +4% early and +1.5% after 35 min; one more
hero alive ~0 early and +7% at 35 min; a Guardian +1%, a Walker +5%, a Base Guardian +4%, a Shrine +8%,
the Patron's first phase +15% (on top of the souls they give). A death of your team typically costs
under 1% before 20 min and 4-9% after 25 min (souls, objectives and deaths that usually follow).

Every finding is then scored by how much it moved your team's win chance, without counting any
swing twice. Deaths and objectives less than 20 s apart form one episode; its real swing (5 s before
the first event to 15 s after the last) is measured once and split between its events in proportion
to their typical value at that minute (a death: what deaths usually cost then; an objective: the
model's value for it), so the credits add up exactly to the real swing. Findings then take credits:

| Finding | Credit |
|---|---|
| Death | that death's share |
| Objective lost / taken | that objective's share |
| Ultimate | shares of the deaths, within 15 s, of the heroes it caught and your nearby teammates |
| Multi-kill | shares of your kills not already counted in an ultimate ("counted in ..." otherwise) |
| Missed fight | the fight's swing, shared between the teammates who weren't there |
| Fight damage | no claim of its own; the fight's swing is shown as context |

Each side claims an event once (an enemy's death can be their mistake and your play, but not two
of your plays). Mistakes and big plays are ranked by it ("-9.2% win chance"). Findings without a clean window
("3+ deaths before 10:00", "farming while needed") are converted from their old impact score with
this match's ratio between impact and measured swings, and marked as estimates. The report adds a
win-chance chart with the biggest swings and each fight's swing. Limits: the model knows souls, objectives and heroes alive but not items, ultimates ready or positions;
near the end of a game the win chance is close to 100%, so late plays show small numbers.

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
- **v3 – win probability (done: `winprob.py` + `wpa.py`):** train a win-probability model on match states (time, soul difference, objectives, players alive, ultimates ready) from many matches, then score every event by how much it changed the team's chance of winning (Win Probability Added). This is the approach behind xPetu's Coachless for League of Legends. It replaces the hand-made impact score and gives an objective ranking of mistakes for each player and for the team.
