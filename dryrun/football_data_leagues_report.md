# Football-Data league report (v4.3-global)

Season used: 2025/26 (main CSVs) or Season 2025 (extra `new/*.csv`).

| Code | ESPN slug | Matches | Useful columns present | Stat columns MISSING | Goals/match | Corners/match | config corner baseline (Δ vs FD) |
|---|---|---|---|---|---|---|---|
| E1 | eng.2 | 552 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.605 | 10.353 | 10.4 +0.05 |
| E2 | eng.3 | 552 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.63 | 10.06 | None  |
| SP2 | esp.2 | 462 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.632 | 9.433 | None  |
| D2 | ger.2 | 306 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.931 | 10.036 | None  |
| I2 | ita.2 | 380 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.561 | 9.542 | None  |
| F2 | fra.2 | 305 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.541 | 9.249 | None  |
| N1 | ned.1 | 306 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 3.176 | 10.454 | None  |
| P1 | por.1 | 306 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.683 | 9.428 | None  |
| B1 | bel.1 | 311 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.675 | 10.167 | None  |
| T1 | tur.1 | 306 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.654 | 9.444 | None  |
| SC0 | sco.1 | 228 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.781 | 10.026 | None  |
| ARG | arg.1 | 510 | PSCH, PSCH, B365CH | HC, AC, HS, AS, HST, AST | 1.947 | None | 9.6  |
| BRA | bra.1 | 380 | PSCH, PSCH, B365CH | HC, AC, HS, AS, HST, AST | 2.524 | None | 10.5  |
| USA | usa.1 | 540 | PSCH, PSCH, B365CH | HC, AC, HS, AS, HST, AST | 3.017 | None | 9.8  |
| MEX | mex.1 | 336 | PSCH, PSCH, B365CH | HC, AC, HS, AS, HST, AST | 2.905 | None | 9.7  |
| JPN | jpn.1 | 380 | PSCH, PSCH, B365CH | HC, AC, HS, AS, HST, AST | 2.397 | None | 9.6  |
| E0 | eng.1 | 380 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.75 | 9.997 | 10.4 +0.40 |
| SP1 | esp.1 | 380 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.695 | 9.666 | 9.6 -0.07 |
| D1 | ger.1 | 306 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 3.235 | 9.755 | 9.6 -0.16 |
| I1 | ita.1 | 380 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.426 | 8.821 | 9.8 +0.98  ⚠️ >0.8 off |
| F1 | fra.1 | 306 | HS, AS, HST, AST, HC, AC, PSCH, PC>2.5, B365>2.5, B365C>2.5, AvgC>2.5, PSCH, B365CH | none | 2.82 | 9.595 | 9.5 -0.10 |

Extra-league files (`new/ARG.csv` etc.) contain results + closing 1X2/O-U odds only: no corners/shots, so their
corner baselines cannot be checked against Football-Data and stay as configured.
