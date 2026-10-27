# Dry-run paper-trade report – 2026-10-25 (evening)

Generated 2026-10-25 22:32:18 UTC. Nothing was sent to Telegram; no bets placed.

**Sample size is tiny – these numbers say nothing about long-run edge.**

* Alerts logged: 0 (graded 0)
* Record: –
* Flat 1-unit P&L at min odds: +0.00 u over 0 bets
* Quarter-Kelly stake P&L at min odds: +0.00 on 0.00 staked
* Brier (win/half-win vs loss/half-loss, pushes excluded, n=0): –

* Alert rules: v3
* Kick-off window (UTC): 19:00 -> 20:15
* Target matches: Barcelona v Real Madrid (20:00); Udinese v AC Milan (19:45)
* Scoreboard polls: 64
* Run window (UTC): 18:38 -> 22:32
* Target matches (kick-off <= cutoff) seen: 2
* Finished (FT recorded): 2
* HTTP requests: 65
* Refusals (host status -> count): none
* Final results: Barcelona 1-2 Real Madrid (corners 6.0-6.0); Udinese 0-2 AC Milan (corners 5.0-4.0)

| match | min | score | line | fair | min odds | stake | FT goals | FT corners | result | P&L u | P&L $ | FotMob | triggers |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|

## Polling gaps (heartbeat monitor, threshold 180 s)

| kind | start (UTC) | end (UTC) | duration | live matches affected (last known minute) |
|---|---|---|---|---|
| suspend | 2026-10-25 19:24:14 | 2026-10-25 19:27:17 | 3.0 min | none |
| suspend | 2026-10-25 19:44:48 | 2026-10-25 19:54:37 | 9.8 min | Udinese v AC Milan (?') |
| suspend | 2026-10-25 19:54:37 | 2026-10-25 22:30:33 | 155.9 min | Udinese v AC Milan (9'); Barcelona v Real Madrid (?') |

_Gap table regenerated 22:3x UTC after a fix: matches whose play window overlaps a gap are now listed even if the last tick before the gap hadn't seen them live. Both target matches were in play almost entirely inside the 19:54–22:30 suspend; the v3 runner saw only one Udinese state (9') and no Barcelona v Real Madrid state._
