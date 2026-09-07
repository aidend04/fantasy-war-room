# War Room — Sleeper fantasy football panel

A local dashboard for drafting and managing a Sleeper league using public data only
(Sleeper API, Sleeper/Rotowire projections, Fantasy Football Calculator ADP).
No API keys, no logins, no dependencies beyond Python 3.

## Run

```bash
python3 server.py
```

Then open http://localhost:8765, type your Sleeper username, press Load, and pick a league.
Your username and league are remembered in the browser.

## What each tab does

| Tab | Use it for |
|---|---|
| Draft | Live draft board. Polls picks every 6 s while your draft is running (and watches for the draft to start). Ranks available players by value over replacement (VORP) in *your league's scoring and roster slots*, weighted by your positional need. The **draft adapter** simulates the picks between now and your next turn hundreds of times, using each rival's actual roster needs, to give the probability each player survives and the *wait cost* of passing on each position. Also shows your pick slots, reaches vs ADP, positional runs, and a post-draft recap. |
| Mock | Practice drafts against bots that use your league's scoring, lineup slots and draft order. Pick from the board or take the recommendation, undo, auto-finish, and get a recap with your rank. "Run 25 mocks" plays full drafts on autopilot and reports who you usually land in each round and your average finish. |
| Tiers | Positional tier sheets with tier breaks and replacement level. Drafted/rostered players are crossed out. |
| My Team | Optimal lineup for the selected week vs your current lineup, roster value, drop candidates, bye-week map. |
| Waivers | Free agents ranked by rest-of-season VORP, with the upgrade over your weakest player at that position and Sleeper add/drop trends. |
| Matchup | This week's projected score vs your opponent and a rough win probability; flags points left on either bench. |
| League | Power rankings from every team's best rest-of-season lineup, with positional strength so you can find trade partners. |
| Trade | Tick players on both sides; it scores the trade by the change in each team's best lineup plus bench value. |

## How the numbers are built

- **Points** are recomputed from projected stat lines using the league's own `scoring_settings`, so
  TE premium, 6-pt passing TDs, bonuses, etc. are all respected. Without a league it uses the Scoring selector (half PPR by default).
- **VORP**: every league-wide starting slot (fixed then flex/superflex) is filled with the best available
  player; replacement level is set a few bench spots deeper (RB/WR 1.5 per team, QB/TE 0.5, none for K/DEF).
- **Pick guidance**: the card at the top of the Draft and Mock tabs names the pick and gives the reasons in
  plain language: value over replacement, wait cost, which slot it fills (or whom it upgrades), ADP fairness,
  injury flags, bye overlaps, and the odds he survives to your pick. Three alternatives and a plan B follow.
- **Pick value ("To you")**: a player filling an empty starting slot counts his full value over replacement;
  one who only upgrades a starter counts the upgrade; one who would sit on the bench counts a fraction
  (RB/WR depth more than a third QB). Required slots are forced full in the closing rounds.
- **Draft adapter (live drafts)**: 400 Monte Carlo runs of the picks before yours. Each rival picks from the
  top of the ADP board with probability decaying by ADP rank, tilted by which starters that rival still lacks
  (K/DEF are ignored until the last rounds). Output: P(available at your next pick and the one after) per
  player, expected best value at each position when you are up, and **wait cost** = best now minus expected
  best later. Recommendation = 40% value to you + 60% wait cost (scaled by how much of the value your lineup can use). Before the draft order is set, or in
  auction drafts, it falls back to a normal model on ADP using the Fantasy Football Calculator spread.
- **No league loaded**: the Scoring selector (default half PPR) drives points and ADP format.
- **Rest of season** = sum of Sleeper weekly projections from the selected week through week 18.
- **ADP format** auto-selects 2QB for superflex leagues, else PPR / half / standard from the `rec` setting.

Data is cached in `.cache/` (players 6 h, season projections 1 h, weekly 15 min, draft picks 5 s).
