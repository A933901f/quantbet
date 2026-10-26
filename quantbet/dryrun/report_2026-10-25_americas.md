# Dry-run paper-trade report – 2026-10-25 (americas)

Generated 2026-10-26 00:33:32 UTC. Nothing was sent to Telegram; no bets placed.

**Sample size is tiny – these numbers say nothing about long-run edge.**

* Alerts logged: 5 (graded 1)
* Record: loss 1
* Flat 1-unit P&L at min odds: -1.00 u over 1 bets (ROI -100.0%)
* Quarter-Kelly stake P&L at min odds: -15.00 on 15.00 staked (ROI -100.0%)
* Brier (win/half-win vs loss/half-loss, pushes excluded, n=1): 0.3391, log-loss 0.8730

* Alert rules: v4.3-global
* Kick-off window (UTC): 21:30 -> 04:00
* Target matches: LAFC v LA Galaxy (01:00); Atlas v Puebla (23:00); River Plate v Racing Club (22:15); Querétaro v Cruz Azul (01:00); América v Pachuca (01:05); Tijuana v Santos (03:10); Barracas Central v Estudiantes de Río Cuarto (00:30); Central Córdoba (Santiago del Estero) v Vélez Sarsfield (00:30); Universidad de Concepción v Deportes Limache (23:00); Atlético Nacional v Independiente Medellín (23:10); Deportes Tolima v Deportivo Pasto (01:15); San Diego Wave FC v Boston Legacy FC (23:00); Tapatío v Club Atlético Morelia (23:00); Nacional v Liverpool (22:30); Deportivo Garcilaso v Los Chankas (23:00); Sportivo San Lorenzo v Cerro Porteño (22:30); Deportivo La Guaira v Metropolitanos (23:30); Oriente Petrolero v The Strongest (23:30); UPNFM v Club Deportivo Olimpia (23:15); AD San Carlos v Saprissa (22:00); Suchitepequez v Aurora FC (23:30); Coastal Carolina v Old Dominion (22:00); James Madison v Kentucky (22:00); Incarnate Word v Houston Christian (23:00); Northwestern v Purdue (22:00); SE Louisiana v Lamar (22:00); Alajuelense v Pérez Zeledón (00:00); Tigres UANL v Monterrey (00:06); UAB v Charlotte (00:00); Central Arkansas v Queens University (00:00)
* Scoreboard polls: 30
* Run window (UTC): 23:50 -> 00:33
* Target matches (kick-off <= cutoff) seen: 30
* Finished (FT recorded): 8
* HTTP requests: 52
* Refusals (host status -> count): none
* Final results: AD San Carlos 2-1 Saprissa (corners None-None); Northwestern 1-3 Purdue (corners None-None); River Plate 1-0 Racing Club (corners 5.0-5.0); Coastal Carolina 1-0 Old Dominion (corners None-None); SE Louisiana 0-1 Lamar (corners None-None); Nacional 0-1 Liverpool (corners None-None); Sportivo San Lorenzo 1-3 Cerro Porteño (corners 0.0-0.0); James Madison 0-0 Kentucky (corners None-None)

| match | min | score | line | fair | min odds | stake | FT goals | FT corners | result | P&L u | P&L $ | FotMob | triggers |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| River Plate v Racing Club | 69 | 1-0 | Over 12.75 Corners | 1.89 | 1.94 | 15 | 1-0 | 5-5 | loss | -1.00 | -15.00 | not_checked |  |
| Atlético Nacional v Independiente Medellín | 41 | 0-1 | Over 11.25 Corners | 1.94 | 2.00 | 5 |  |  | open |  |  | not_checked | momentum_away, activity_high |
| Atlas v Puebla | 64 | 2-2 | Over 11.75 Corners | 2.02 | 2.08 | 10 |  |  | open |  |  | not_checked | fav_chasing |
| Atlas v Puebla | 64 | 2-2 | Over 5.0 Goals | 1.85 | 1.93 | 15 |  |  | open |  |  | not_checked | fav_chasing |
| Atlético Nacional v Independiente Medellín | 55 | 1-1 | Over 3.25 Goals | 1.81 | 1.87 | 10 |  |  | open |  |  | confirmed | activity_high, fav_chasing |

## Polling gaps (heartbeat monitor, threshold 180 s)

| kind | start (UTC) | end (UTC) | duration | live matches affected (last known minute) |
|---|---|---|---|---|
| restart | 2026-10-25 22:31:33 | 2026-10-25 23:50:03 | 78.5 min | 20261025:udinese:milan (?'); Atlas v Puebla (?'); River Plate v Racing Club (90'); Universidad de Concepción v Deportes Limache (?'); Atlético Nacional v Independiente Medellín (?'); San Diego Wave FC v Boston Legacy FC (?'); Tapatío v Club Atlético Morelia (?'); Nacional v Liverpool (?'); Deportivo Garcilaso v Los Chankas (?'); Sportivo San Lorenzo v Cerro Porteño (?'); Deportivo La Guaira v Metropolitanos (?'); Oriente Petrolero v The Strongest (?'); UPNFM v Club Deportivo Olimpia (?'); AD San Carlos v Saprissa (?'); Suchitepequez v Aurora FC (?'); Coastal Carolina v Old Dominion (?'); James Madison v Kentucky (?'); Incarnate Word v Houston Christian (?'); Northwestern v Purdue (?'); SE Louisiana v Lamar (?') |
| suspend | 2026-10-26 00:11:34 | 2026-10-26 00:17:42 | 6.1 min | Atlas v Puebla (49'); Atlético Nacional v Independiente Medellín (45'); Coastal Carolina v Old Dominion (?'); Deportivo Garcilaso v Los Chankas (45'); Deportivo La Guaira v Metropolitanos (29'); Incarnate Word v Houston Christian (?'); James Madison v Kentucky (?'); Nacional v Liverpool (?'); Oriente Petrolero v The Strongest (29'); River Plate v Racing Club (90'); San Diego Wave FC v Boston Legacy FC (45'); SE Louisiana v Lamar (?'); Sportivo San Lorenzo v Cerro Porteño (?'); Suchitepequez v Aurora FC (29'); Tapatío v Club Atlético Morelia (?'); Universidad de Concepción v Deportes Limache (48'); UPNFM v Club Deportivo Olimpia (?'); Alajuelense v Pérez Zeledón (10'); Central Arkansas v Queens University (1'); Tigres UANL v Monterrey (4'); UAB v Charlotte (3') |
| suspend | 2026-10-26 00:17:42 | 2026-10-26 00:25:47 | 8.1 min | Atlas v Puebla (49'); Atlético Nacional v Independiente Medellín (45'); Coastal Carolina v Old Dominion (?'); Deportivo Garcilaso v Los Chankas (45'); Deportivo La Guaira v Metropolitanos (29'); Incarnate Word v Houston Christian (?'); James Madison v Kentucky (?'); Nacional v Liverpool (?'); Oriente Petrolero v The Strongest (29'); River Plate v Racing Club (90'); San Diego Wave FC v Boston Legacy FC (45'); SE Louisiana v Lamar (?'); Sportivo San Lorenzo v Cerro Porteño (?'); Suchitepequez v Aurora FC (29'); Tapatío v Club Atlético Morelia (?'); Universidad de Concepción v Deportes Limache (48'); UPNFM v Club Deportivo Olimpia (?'); Alajuelense v Pérez Zeledón (10'); Central Arkansas v Queens University (1'); Tigres UANL v Monterrey (4'); UAB v Charlotte (3') |
