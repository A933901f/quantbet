# Americas overnight slate – Sun 25 Oct 2026 (ESPN poll ~23:37–23:40 UTC)

Source: `site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard?limit=500` (1 request) + per-league scoreboards for
usa.1, mex.1, bra.1, bra.2, arg.1, col.1, chi.1, conmebol.libertadores, conmebol.sudamericana (1 request each), all via
`PoliteFetcher` (honest UA, ≥1.5 s global gap, cache). 0 refusals. Raw payloads: `data/espn_probe/`.

**Important finding:** the default `soccer/all/scoreboard` (no `limit`) returns only **100 events** (93 finished) and on this poll
*omitted* arg.1, col.1 and chi.1 live matches. With `?limit=500` it returned 336 events (19 live, 12 upcoming). v4.3-global uses `limit=500`
(config `QB_ESPN_ALL_LIMIT`) and still falls back to per-league scoreboards for listed leagues.

Stats present = `wonCorners`, `totalShots`, `shotsOnTarget` keys in both competitors' `statistics`. Telemetry = what the
v4.3 Live Stats Integrity Gate would conclude (pre-match events carry zeros; it only applies from minute 30).

| Kick-off UTC | League (slug) | Match | State | Min | Score | Stats keys | Corners / Shots / SOT (h-a) | Telemetry | Mode |
|---|---|---|---|---|---|---|---|---|---|
| 22:00 | Costa Rica (id 4005) (crc.1?) | AD San Carlos v Saprissa | in | 77' | 2-1 | n | – | none | auto-discovered |
| 22:00 | NCAA D-I men (usa.ncaa.m.1) | Coastal Carolina v Old Dominion | in | 63' | 0-0 | n | – | none | auto-discovered |
| 22:00 | NCAA D-I men (usa.ncaa.m.1) | James Madison v Kentucky | in | 58' | 0-0 | n | – | none | auto-discovered |
| 22:00 | NCAA D-I women (usa.ncaa.w.1) | Northwestern v Purdue | in | 64' | 1-3 | n | – | none | auto-discovered |
| 22:00 | NCAA D-I women (usa.ncaa.w.1) | SE Louisiana v Lamar | in | 62' | 0-1 | n | – | none | auto-discovered |
| 22:15 | Argentine Liga Profesional de Fútbol (arg.1) | River Plate v Racing Club | in | 58' | 1-0 | y | C 5-4 / Sh 5-8 / SOT 4-3 | real | listed |
| 22:30 | Paraguay (id 3934) (par.1?) | Sportivo San Lorenzo v Cerro Porteño | in | 47' | 0-2 | y | C 0-0 / Sh 0-0 / SOT 0-0 | FAKE ZEROS | auto-discovered |
| 22:30 | Uruguay (id 680) (uru.1?) | Nacional v Liverpool | in | 45'+4' | 0-1 | n | – | none | auto-discovered |
| 23:00 | Chilean Primera División (chi.1) | Universidad de Concepción v Deportes Limache | in | 36' | 0-1 | y | C 2-0 / Sh 5-1 / SOT 1-1 | real | listed |
| 23:00 | Mexican Liga BBVA MX (mex.1) | Atlas v Puebla | in | 37' | 1-1 | y | C 5-0 / Sh 6-5 / SOT 2-2 | real | listed |
| 23:00 | Mexican Liga de Expansión MX (mex.2) | Tapatío v Club Atlético Morelia | in | 37' | 0-1 | y | C 0-0 / Sh 0-0 / SOT 0-0 | FAKE ZEROS | auto-discovered |
| 23:00 | Peru (id 670) (per.1?) | Deportivo Garcilaso v Los Chankas | in | 24' | 1-0 | y | C 2-1 / Sh 4-1 / SOT 1-0 | real | auto-discovered |
| 23:00 | NCAA D-I men (usa.ncaa.m.1) | Incarnate Word v Houston Christian | in | 29' | 1-0 | n | – | none | auto-discovered |
| 23:00 | NWSL (women) (usa.nwsl) | San Diego Wave FC v Boston Legacy FC | in | 28' | 0-0 | y | C 1-1 / Sh 4-3 / SOT 2-0 | real | auto-discovered |
| 23:10 | Colombian Primera A (col.1) | Atlético Nacional v Independiente Medellín | in | 26' | 0-0 | y | C 2-1 / Sh 1-4 / SOT 1-1 | real | listed |
| 23:15 | Honduras (id 3929) (hon.1?) | UPNFM v Club Deportivo Olimpia | in | 22' | 0-1 | n | – | none | auto-discovered |
| 23:30 | Bolivia (id 620) (bol.1?) | Oriente Petrolero v The Strongest | in | 7' | 0-0 | y | C 0-0 / Sh 0-0 / SOT 0-0 | FAKE ZEROS | auto-discovered |
| 23:30 | Guatemala (id 3928) (gua.1?) | Suchitepequez v Aurora FC | in | 4' | 0-0 | n | – | none | auto-discovered |
| 23:30 | Venezuela (id 3949) (ven.1?) | Deportivo La Guaira v Metropolitanos | in | 7' | 0-0 | y | C 0-0 / Sh 0-0 / SOT 0-0 | FAKE ZEROS | auto-discovered |
| 00:00 (+1d) | Costa Rica (id 4005) (crc.1?) | Alajuelense v Pérez Zeledón | pre | - |  | n | – | none | auto-discovered |
| 00:00 (+1d) | NCAA D-I men (usa.ncaa.m.1) | Central Arkansas v Queens University | pre | - |  | n | – | none | auto-discovered |
| 00:00 (+1d) | NCAA D-I women (usa.ncaa.w.1) | UAB v Charlotte | pre | - |  | n | – | none | auto-discovered |
| 00:06 (+1d) | Liga MX-related comp (id 23541) (mex.? (id 23541)) | Tigres UANL v Monterrey | pre | - |  | y | C 0-0 / Sh 0-0 / SOT 0-0 | pre (zeros) | auto-discovered |
| 00:30 (+1d) | Argentine Liga Profesional de Fútbol (arg.1) | Barracas Central v Estudiantes de Río Cuarto | pre | - |  | y | C 0-0 / Sh 0-0 / SOT 0-0 | pre (zeros) | listed |
| 00:30 (+1d) | Argentine Liga Profesional de Fútbol (arg.1) | Central Córdoba (Santiago del Estero) v Vélez Sarsfield | pre | - |  | y | C 0-0 / Sh 0-0 / SOT 0-0 | pre (zeros) | listed |
| 01:00 (+1d) | Mexican Liga BBVA MX (mex.1) | Querétaro v Cruz Azul | pre | - |  | n | – | none | listed |
| 01:00 (+1d) | MLS (usa.1) | LAFC v LA Galaxy | pre | - |  | n | – | none | listed |
| 01:00 (+1d) | NCAA D-I men (usa.ncaa.m.1) | UCLA v San Diego | pre | - |  | n | – | none | auto-discovered |
| 01:05 (+1d) | Mexican Liga BBVA MX (mex.1) | América v Pachuca | pre | - |  | n | – | none | listed |
| 01:15 (+1d) | Colombian Primera A (col.1) | Deportes Tolima v Deportivo Pasto | pre | - |  | n | – | none | listed |
| 03:10 (+1d) | Mexican Liga BBVA MX (mex.1) | Tijuana v Santos | pre | - |  | n | – | none | listed |

Listed Americas events not in tonight's all-feed because they are **26 Oct** fixtures (not a feed gap): bra.1: Coritiba v Grêmio 2026-10-26T22:30Z; bra.1: Flamengo v Atlético-MG 2026-10-26T23:00Z; bra.2: América Mineiro v Cuiabá 2026-10-26T22:30Z; bra.2: Juventude v Náutico 2026-10-26T23:30Z

Per-league scoreboard extras (not tonight): bra.1 Coritiba v Grêmio 26 Oct 22:30Z, Flamengo v Atlético-MG 26 Oct 23:00Z; bra.2 América Mineiro v Cuiabá 22:30Z, Juventude v Náutico 23:30Z (26 Oct);
conmebol.libertadores / sudamericana: no fixtures tonight (last: 23 Oct / 21 Oct, finished).

Finished earlier today (with stats): arg.1 Aldosivi 1-0 Belgrano, Independiente 0-1 Boca; col.1 Fortaleza 1-1 Cali, Alianza 1-0 Inter Bogotá; chi.1 Ñublense 2-1 Coquimbo, U. La Calera 3-1 U. Católica, Palestino 1-1 Colo Colo.

Notes: Tapatío v Morelia (mex.2, 37') and Sportivo San Lorenzo v Cerro Porteño (Paraguay, 47') carried the statistics keys but
**all zeros** while live — exactly the 'fake zero' case the integrity gate skips. NCAA, Honduras, Costa Rica, Guatemala, Uruguay events
had no statistics at all. League names marked `?` are inferred from teams (the all-feed has no league names; v4.3 reads
`header.league` from the once-per-match summary).
