# Data sources – $0/month stack

Everything below is free. Checked on 25 Oct 2026. The paid-provider research
from v0.1 is archived at the end; those adapters are still in the code but
disabled by default.

| Purpose | Source | Cost | Status in code | Verified |
|---|---|---|---|---|
| Pre-match sharp prices (Pinnacle + EU books) | The-Odds-API v4, free tier | $0, 500 credits/month | `odds_quota.py`, on when `THE_ODDS_API_KEY` is set | Docs checked (below); no key used here |
| Live stats Tier 1 (primary) | ESPN public site API (unofficial) | $0 | `espn.py`, **on** | **Yes.** 7 scoreboards + 3 summaries, all HTTP 200 |
| Live stats Tier 2 (budgeted) | API-Football free tier | $0, 100 calls/day | `api_football_free.py`, on only with `API_FOOTBALL_KEY` | Docs checked; no key used here |
| Live stats Tier 3 (xG) | FotMob match page `__NEXT_DATA__` + search suggest | $0 | `fotmob_page.py`, **off** (opt-in) | **Yes.** Page and suggest both HTTP 200, stats parsed |
| Live stats (legacy) | SofaScore public JSON (undocumented) | $0 | `free_stats_poller.py`, **off** | **No.** One request answered 403 (see below) |
| Live stats (legacy) | FotMob JSON API | $0 | `free_stats_poller.py`, **off** | **No.** Old path 404; new path not checked |
| Live odds (Layer 2) | SofaScore event odds widget (undocumented) | $0 | `aggregator_odds.py`, **off** | No |
| History, calibration, CLV | Football-Data.co.uk CSVs | $0 | `football_data.py`, `scripts/calibrate.py` | **Yes**, E0 2025/26 downloaded (380 matches) |
| Notifications | Telegram Bot API | $0 | `notifier.py` | Code from task 1 |
| Tip channels (optional) | Telethon, your own account, channels you joined | $0 | `auditor.TelethonTipListener` | Not exercised |

## The-Odds-API v4 (free tier)

Source: https://the-odds-api.com/liveapi/guides/v4/

* `GET /v4/sports` and `GET /v4/sports/{sport}/events` **do not count**
  against the quota. QuantBet uses `/events` to find upcoming kick-offs.
* `GET /v4/sports/{sport}/odds` costs **markets × regions**. With
  `bookmakers=`, every group of up to 10 bookmakers counts as one region.
  QuantBet's default (`h2h,totals` and 6 books: pinnacle, betfair_ex_eu,
  williamhill, unibet_eu, marathonbet, onexbet) costs **2 credits**.
* Responses with no data are not charged.
* Every response carries `x-requests-remaining`, `x-requests-used` and
  `x-requests-last`. QuantBet stores them in SQLite and uses them as the source
  of truth for the monthly budget.
* `/scores` costs 1 credit, or 2 with `daysFrom`. QuantBet does not use it
  for live state.
* Historical odds endpoints are on paid plans only, so they are not used.
  Football-Data.co.uk replaces them.
* Event-level odds (`/events/{id}/odds`) cost unique markets returned ×
  regions. QuantBet does not use them by default.
* There is no European `bet365` key (only `bet365_au`). Bet365 prices are
  therefore never fetched from this API.
* Pinnacle prices are scraped by the vendor from Pinnacle's public site.
  Featured pre-match markets update about every 60 s. With the free tier's
  credit budget, QuantBet's snapshot of a fixture can be **up to 3 hours
  old**. It is a prior for team strength, not a live sharp line.

Budget arithmetic: (500 − 20 reserve) / 2 = **240 odds calls per month**,
about 8 a day. Each call covers a whole sport (all its fixtures), so 4 leagues
at one refresh per match-day window is sustainable.

## ESPN public site API (Tier 1, primary)

Base (`QB_ESPN_BASE`): `https://site.api.espn.com/apis/site/v2/sports/soccer`.
Leagues: `eng.1, esp.1, ger.1, ita.1, fra.1, uefa.champions, uefa.europa`.
There is no key and no documentation, and it is not an official public API:
it can change or be withdrawn, and automated use may be against ESPN's terms.

**Verified 25 Oct 2026** with an honest User-Agent. All 10 requests returned
200 (7 scoreboards, 3 summaries: live Inter v Fiorentina, FT Bayern v Arsenal,
FT Inter v Shakhtar with a red card). Trimmed copies are in `tests/fixtures/espn_*.json`.

| Endpoint | Fields used |
|---|---|
| `/{league}/scoreboard` | `leagues[0].{name,slug}`, `events[].{id,date}`, `events[].status.{clock,displayClock,period,type.{name,state,completed}}`, `competitions[0].competitors[].{homeAway,score,team.{id,displayName},statistics[].{name,displayValue}}` (wonCorners, totalShots, shotsOnTarget, possessionPct, foulsCommitted…), `competitions[0].details[].{type.text,redCard,team.id,clock}` |
| `/{league}/summary?event={id}` | `boxscore.teams[].{homeAway,team.displayName,statistics[]}` (foulsCommitted, yellowCards, redCards, offsides, wonCorners, saves, possessionPct, totalShots, shotsOnTarget, blockedShots, …), `header.competitions[0].{status,competitors[].score}`, `keyEvents[].{type.type,clock.displayValue,period.number,team.displayName}` (kickoff, goal, yellow-card, red-card, substitution, halftime, start-2nd-half, penalty---scored, end-regular-time), `pickcenter[0]` (DraftKings: `moneyline.{home,draw,away}.{open,close,live}.odds`, `total.{over,under}.close`, `overUnder`) |

**`/all/scoreboard` (verified 25 Oct 2026, 200, ~870 KB, 100 events across
all competitions).** `leagues` is `[{}]`. The league is in `events[].uid`
(`s:600~l:730~e:401875036`; 700 = eng.1, 740 = esp.1, 720 = ger.1,
730 = ita.1, 710 = fra.1, 775 = UCL, 776 = UEL) and `season.slug`. Live and
finished events carry the same 9 `competitors[].statistics` as the
per-league endpoint; scheduled ones have none. All of today's target fixtures
were present (eng.1 5, esp.1 4, ger.1 2, ita.1 5, fra.1 6). The 100-event
page could in principle truncate on very busy days. The poller then still
falls back per league for live events without stats.

**User-Agent finding:** `...; contact: you@example.com` → 403 Access Denied
(reproduced with curl). `quantbet-signal-engine/0.2 (personal, read-only)` → 200.

Status names seen: `STATUS_SCHEDULED`, `STATUS_FIRST_HALF` (id 25),
`STATUS_HALFTIME`, `STATUS_FULL_TIME` (id 28). ESPN has no xG, dangerous
attacks or shots inside the box, so the synthetic momentum comes from
`delta_tracker.py`.

## API-Football free tier (Tier 2, optional)

`GET https://v3.football.api-sports.io/fixtures?live=all` and
`/fixtures/statistics?fixture={id}`, with header `x-apisports-key`. The free
plan allows 100 requests/day and 10/minute. Daily usage is reported in
`x-ratelimit-requests-remaining`. Free plans are limited in which seasons they
can access, and current-season fixtures were reportedly removed from the free
plan in 2026. QuantBet treats an `errors` payload as "off until tomorrow".
Calls are only made for flagged matches in minute 55–85, with a hard stop at
90 per UTC day (SQLite).

## FotMob match page (Tier 3, opt-in)

`GET https://apigw.fotmob.com/searchapi/suggest?term=Fiorentina&lang=en` returns
200 JSON (`matchSuggest[].options[].payload.{id,homeName,awayName,matchDate}`).
`GET https://www.fotmob.com/match/5749714` returned **200 HTML (~0.9 MB)**
without a redirect. Its `<script id="__NEXT_DATA__">` holds `props.pageProps`
with `general`, `header` (`status.liveTime.{short,long}`,
`numberOfHomeRedCards`), and `content.stats.Periods.All.stats[]`. The groups
are top_stats, shots, expected_goals, passes, defense, duels and discipline,
with keys such as `expected_goals`, `total_shots`, `ShotsOnTarget`,
`shots_inside_box`, `corners`, `red_cards` and `BallPossesion`. It also holds
`content.momentum.main.data[{minute,value}]`. Parsing worked on the first try.
It stays **disabled by default** because it is undocumented page structure and
heavy (0.9 MB per fetch). When enabled, it only runs for flagged matches, at
most every 300 s.

## SofaScore (undocumented, legacy – disabled by default)

Base URL (configurable with `QB_SOFASCORE_BASE`):
`https://api.sofascore.com/api/v1`

| Endpoint | Used for |
|---|---|
| `/sport/football/events/live` | live list, score, status, `time.currentPeriodStartTimestamp`, `time.initial`, injury time |
| `/event/{id}` | event detail (template kept; not polled by default) |
| `/event/{id}/statistics` | `cornerKicks`, `shotsOnGoal`, `totalShotsInsideBox`, `expectedGoals`, `ballPossession`, `redCards` |
| `/event/{id}/graph` | `graphPoints[{minute, value}]` attack momentum. Mean of the last 10 minutes ÷ 100, clipped to ±1 |
| `/event/{id}/incidents` | red and second-yellow cards (`incidentType=card`, `incidentClass=red/yellowRed`), `injuryTime` |
| `/event/{id}/odds/1/all` | Layer 2 odds (fractional values; off by default) |

**Reachability check (25 Oct 2026):** one `GET .../sport/football/events/live`
with an honest User-Agent returned **HTTP 403** with
`{"error":{"code":403,"reason":"Forbidden"}}`. The endpoint exists but refused
an identifiable non-browser client from this server. Per the project rules, no
evasion was attempted: no impersonation, no browser, no proxies. At runtime
QuantBet treats this as a block, backs off and fails over. SofaScore is now
**off by default**. It is a plain client and is never given `impersonate`.

## FotMob JSON API (undocumented, legacy – disabled by default)

Configurable with `QB_FOTMOB_BASE`. The default is
`https://www.fotmob.com/api/data`, with `matches?date=YYYYMMDD` and
`matchDetails?matchId={id}`.

**Reachability check (25 Oct 2026):** the historical path
`https://www.fotmob.com/api/matches?date=20261025` returned **HTTP 404 (HTML)**,
so it has moved. The `/api/data/...` default is a best guess and was **not
verified**. FotMob is also reported to require a signed request header for
some endpoints. QuantBet does not forge it. If FotMob refuses, the fallback
simply yields no data.

Parsed fields: `header.teams[].score` and `header.status.liveTime`
(`"67'"`, `"45+2'"`, `"HT"`, `long` = `mm:ss`), plus `content.stats.Periods.All.stats[].stats[]`
(key/title + `[home, away]`), `content.matchFacts.events.events[]` cards, and
`content.momentum.main.data[]`.

## Football-Data.co.uk (free CSVs)

* URL pattern: `https://www.football-data.co.uk/mmz4281/{yy}{yy+1}/{code}.csv`,
  e.g. `.../2526/E0.csv`. Column notes are in `https://www.football-data.co.uk/notes.txt`.
* Columns used: `FTHG FTAG HS AS HST AST HC AC`, Bet365 `B365H/D/A`,
  `B365>2.5/<2.5`, Pinnacle closing `PSCH/PSCD/PSCA`, `PC>2.5/<2.5`, and
  `AHCh PCAHH PCAHA`.
* **Verified:** E0 2025/26 downloaded with one request (380 matches). The
  calibration run gave:
  * home goals 1.53, away goals 1.22 per match;
  * total corners mean 10.0, variance 10.67;
  * NegBin corner dispersion r ≈ 150, meaning corners are almost Poisson. The
    prior was 14 and was too dispersed;
  * fitted goal correlation ≈ 0 (clipped at 0).
* Bet365 *pre-closing* (`B365H/D/A`, `B365>2.5`, collected before the weekend/midweek) 1X2/O2.5 prices against the Pinnacle *closing* no-vig price
  averaged −4.6 % to −6.7 % EV. Pre-match Bet365 is rarely +EV against the
  close, so the free stack focuses on in-play lags.

## Terms & risk

SofaScore, FotMob and similar sites publish no public API. Their JSON
endpoints are **undocumented and can change or be withdrawn at any time**.
Automated access **may be against those sites' terms of use**. Read the terms
and decide for yourself before enabling them. Every endpoint is configurable,
and each source can be switched off (`QB_ENABLE_SOFASCORE`,
`QB_ENABLE_FOTMOB`). Scraping Flashscore, OddsPortal or any bookmaker is not
implemented, and requests to bet365 domains are refused in code.

---

# Archive – paid providers researched in v0.1 (disabled by default)


All prices below are copied from the cited pages as they read on 25 Oct 2026.
Where a vendor does not publish a price, it says **not published**. Prices
change; re-check before subscribing. Nothing here requires a bookmaker login,
browser automation, or your home IP – all sources are licensed data APIs
called from a server/VPS.

## 1. Bet365 odds (target book)

| Provider | What you get | Price (as published) | Latency / delivery | Source |
|---|---|---|---|---|
| **Odds-API.io** | Bet365 pre-match + in-play, 250+ books, goals / corners / team-totals markets in-play | Solo **$65/mo** (2 bookmakers), Starter **$129** (5), Growth **$239** (10), Pro **$299** (15); 5,000 req/h. WebSocket add-on = 2× plan (Solo+WS **$130**, Starter **$258**, Growth **$478**, Pro **$598**). Annual = 10× monthly. Extra requests +$185 / +$299 / +$409 | WS push stated "<150ms"; REST 100–500 ms; 2 WS connections per key, permessage-deflate. Changelog 23 Oct 2026: Bet365 in-play now from a direct feed reaching REST/WS "within a second" | https://odds-api.io/pricing · https://docs.odds-api.io/guides/websockets · https://docs.odds-api.io/changelog |
| **BetsAPI (b365api)** | Bet365 prematch + in-play (raw Bet365 structures) | Bet365 API **150/mo**, 3,600 req/h; 1-day trial **1.00**, 3-day **5.00**; "Everything API" **300/mo**, 199,999 req/h. Via RapidAPI: Pro **$150/mo** | In-play updates every 3–5 s; REST only (no websocket found) | https://betsapi.com/mm/pricing_table · https://rapidapi.com/b365api-b365api-default/api/betsapi2/pricing |
| **OpticOdds** (ex-OddsJam API) | 200+ books incl. Bet365, SSE streaming (up to 5 sportsbooks per stream, 250 req/15 s streaming limit) | **Not published** (contact sales). Third-party blog: "starting ~$3,000/mo"; community reports ~$5,000/mo per sport – unverified | SSE (not websocket); third-party claims <800 ms | https://opticodds.com/pricing · https://developer.opticodds.com/docs/sse-streaming · https://sportsgameodds.com/blog/comparing-bet365-odds-api-providers |
| The-Odds-API | **No European bet365 key.** Only `bet365_au` (paid plans, AFL/NRL h2h/spreads/totals) | – | – | https://the-odds-api.com/sports-odds-data/bookmaker-apis.html |

Conflict to note: Odds-API.io's directory says "Plans from £49/month" and
"New free API keys are paused" (local-currency rendering), while
https://odds-api.io/sportsbooks/bet365 still mentions a free tier of 100 req/h.
Assume **no free tier** until you see one in your dashboard.

## 2. Sharp reference (Pinnacle / Betfair Exchange)

| Provider | Notes | Price (as published) | Source |
|---|---|---|---|
| **The-Odds-API** | `pinnacle` (eu region – "Odds are from public website which may incur a delay"), `betfair_ex_eu`, `betfair_ex_uk`. Update intervals: featured pre-match 60 s, in-play 40 s; exchanges 20 s / 10 s. Cost = markets × regions per call. No websocket found | Starter **FREE** 500 credits/mo; 20K **$30/mo**; 100K **$59/mo**; 5M **$119/mo**; 15M **$249/mo** | https://the-odds-api.com/ · https://the-odds-api.com/sports-odds-data/update-intervals.html |
| **Betfair Exchange API (direct)** | Best free-ish sharp signal; Stream API available. Delayed key free (1–180 s delay). Live key **£499 one-off**, requires KYC + funded account. **Caveat: Betfair states "Read-only access via the Live App Key isn't permitted"** – a pure read-only use of the live key may breach their terms; use the delayed key, or a reseller (The-Odds-API / Odds-API.io), unless Betfair approves your use. Personal use only; commercial use needs approval | Delayed key: free; Live key: £499 | https://support.developer.betfair.com/hc/en-us/articles/115003864531-Are-there-any-costs-associated-with-API-access · https://betfair-developer-docs.atlassian.net/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687105/Application+Keys |
| **Pinnacle API (direct)** | Public API **closed since 23 July 2025**; bespoke access only via api@pinnacle.com / application form | Cost variable – **not published** | https://github.com/pinnacleapi/pinnacleapi-documentation · https://www.pinnacle.ca/en/api |
| Odds-API.io | Has Betfair Exchange and an "ON Sharp" consensus line; **Pinnacle not in its bookmaker list** | see §1 | https://odds-api.io/pricing |

## 3. Live match statistics

| Provider | Notes | Price (as published) | Source |
|---|---|---|---|
| **API-Football** | `/fixtures?live=all`, `/fixtures/statistics` (shots on target, corners, possession…). **No "dangerous attacks"** stat. Live data reported to refresh roughly every 15 s (from search summary, not confirmed on the pricing page) | Free **$0** 100 req/day; Pro **$19** 7,500/day; Ultra **$29** 75,000/day; Mega **$39** 150,000/day; custom up to 1,500,000/day | https://www.api-football.com/pricing |
| **Sportmonks Football API** | In-play livescores with statistics incl. dangerous attacks (type ids in code are best-effort – verify in their docs) | Starter **€29/mo** (€24 yearly) 5 leagues, 2,000 calls/entity/h; Growth **€99** (€79) 30 leagues, 2,500; Pro **€249** (€199) 120 leagues, 3,000; Enterprise custom, 5,000; 14-day trial | https://www.sportmonks.com/football-api/plans-pricing/ |

## Recommended stacks

### A. Cost-effective (≈ $85–$125/mo + optional free tiers)
* **Bet365 + Betfair Exchange:** Odds-API.io **Solo $65/mo** (pick Bet365 and
  Betfair Exchange as your 2 bookmakers, REST polling).
* **Pinnacle reference:** The-Odds-API **Starter (free, 500 credits)** for
  testing, **20K $30/mo** for light use (accept Pinnacle's public-site delay).
* **Live stats:** API-Football **Pro $19** or **Ultra $29**.
* Alternative Bet365 source: BetsAPI Bet365 **150/mo** (try the 1-day trial
  first and confirm the response fields our adapter expects).

### B. Low-latency
* **Bet365 in-play push:** Odds-API.io **Starter + WebSocket $258/mo** (or
  Solo+WS **$130**) – enable `QB_ENABLE_ODDS_API_IO_WS=true`.
* **Sharp:** Betfair Exchange Stream API – but note the live-key
  read-only restriction above; otherwise The-Odds-API exchange odds (10 s
  in-play interval) on the **100K $59** plan.
* **Live stats with momentum (dangerous attacks):** Sportmonks **Growth €99**
  or **Pro €249**.
* Enterprise option: OpticOdds (SSE streaming) – price **not published**.

Rule of thumb: a human executing on a phone adds 10–60 s, so paying for
sub-second feeds only helps if you act on alerts immediately; the
cost-effective stack is the right place to start paper trading.
