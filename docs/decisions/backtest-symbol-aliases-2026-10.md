# Backtest symbol aliases: price renamed members under their current conId (KAN-119)

**Measured 2026-10-17** against the paper Gateway (clientId 77/78, read-only,
outside every job's IB window) and the 2026-10-13 weekly refresh
`output/backtest_multi_20261013_114528.json`. Artifacts under `output/` were
read and never written. No database was touched.

Rubric: when the coverage figure says a membership-day is priced, is it priced
with *that member's* prices?

## Result

| | Membership-days excluded | Share | State |
|---|---:|---:|---|
| 2026-10-13 refresh, as fetched | 142,331 of 1,265,341 | **11.25%** | BLOCKED |
| same bars, with the aliases and the unpriced table | 111,972 of 1,265,341 | **8.85%** | BLOCKED |

The 33 aliases recover **32,016** membership-days. Three tickers that IB
prices as a *different company* for part of their membership lose **1,655**
days they should never have counted (the second finding below). The figure is
`scripts/run_backtest.py`'s own fetch path (`fetch_bars_from_ib` with
`aliases=BACKTEST_ALIASES, unpriced=BACKTEST_UNPRICED`) for the 36 changed
tickers, merged onto the artifact's bars and measured by
`backtest.membership.measure_coverage` over the artifact's own sessions. The
"before" figure reproduces the artifact's recorded `excluded_pct` exactly. The
script is `assets/kan-119/remeasure.py`; its output is `remeasure.json`.

Still BLOCKED, as the ticket expected. The residual, by category:

| Category | Names | Days | Share of all membership-days |
|---|---:|---:|---:|
| Acquired (IB: Error 200) | 72 | 65,674 | 5.19% |
| Delisted per IB (symbol search lists it only on `VALUE`) | 13 | 22,709 | 1.79% |
| Renamed, alias refused (below) | 19 | 12,238 | 0.97% |
| Same ticker, IB history cut-off | 7 | 4,753 | 0.38% |
| Failed | 6 | 3,043 | 0.24% |
| IB series is another company: unpriced | 3 | 1,799 | 0.14% |
| Different entity under the ticker | 4 | 1,668 | 0.13% |
| Aliased (the 3 sessions IB has no bar for in most series) | 26 | 82 | 0.01% |
| Isolated missing session | 6 | 6 | 0.00% |

## Finding 1: a third of the "delisted" exclusion was renames

IB knows a renamed company only by its current symbol, so `FB`, `ANTM`, `BK`
returned Error 200 and counted as if delisted. `backtest/symbol_aliases.py`
now maps each verified membership ticker to the conId it trades under today.
Bars are fetched by conId and stored under the membership ticker, so universe,
sector and membership logic never see the new name. They stop at `until`, the
first snapshot listing the new ticker, so no session prices one conId under
two tickers.

Admission, per alias (`assets/kan-119/verify.py`, `verify.json`):

- IB's conId and long name for the current symbol (`probe.py`);
- `until` equals the first snapshot that lists the new ticker, and no
  snapshot lists both (pinned by a test, for every entry);
- the conId's history covers the old ticker's member sessions (every admitted
  alias prices all but the same three sessions);
- continuity: the largest daily move inside the old ticker's membership is a
  normal move, and the series is smooth at the change and at every spin-off
  date checked (ARNC 2020-04-01, UTX 2020-04-03, DLPH 2017-12-05,
  LB 2021-08-03, FBHS 2022-12-15, WYN 2018-06-01, HCP 2016-11-01). **IB
  adjusts its history for spin-offs but not for cash special dividends**, which
  is what refused DPS and NLOK.

Moves above 25% inside a window were each a market-wide day (2020-03-24,
2020-11-09) or a known company event (SYMC 2018-05-11), never the ticker
change.

### Refused

| Ticker | Why |
|---|---|
| `CBS`, `VIAC` | IB's `PARA` is conId 880582719, **Banzai International**, not Paramount; `PSKY` is Error 200 |
| `DISCA`, `DISCK` | `WBD` is Error 200, listed only on `VALUE` |
| `CDAY` | `DAY` is on `VALUE` |
| `COG`, `XEC` | `CTRA` is on `VALUE` |
| `TSO`, `YHOO` | successor (`ANDV`, `AABA`) is Error 200 |
| `DPS` | `KDP` falls 82% at the merger (2018-07-09 123.66 → 22.19): the special dividend, unadjusted |
| `NLOK` | `GEN` falls 40% inside the window (2020-01-31 → 02-03): the special dividend, unadjusted |
| `ADS` | `BFH`: HMDS returns no data for every member session |
| `DWDP` | the membership file uses `DD` for E. I. du Pont before 2017; the window also spans the Dow and Corteva separations |
| `UA C` | `UA` (class C) returns no bars; the file's `UA` is another share class that quarter |
| `Q` | `Q` is Qnity today, correct for its 2025-26 membership. The 2017 Quintiles IMS quarter would need `IQV` spliced in front, refused (Finding 2) |
| `BHI` | `BKR` history begins at the 2017 merger |
| `PX`, `STI` | merged into another company's series (Linde plc; BB&T → Truist) |
| `CHK` | equity cancelled in the 2020 bankruptcy; `EXE` is the new equity |

## Finding 2: three members were graded on another company's prices

This one was not in the ticket, and it is the more serious of the two: an
excluded day is a bias the coverage figure reports, a wrong series is not.

| Ticker | What the backtest priced | Days |
|---|---|---:|
| `PARA` | Banzai International (conId 880582719). Its split-adjusted closes ran 98,000 → 130 across Paramount's 2022-04..2025-07 membership, so any book holding "PARA" lost 99.9% | 877 |
| `IR` | Gardner Denver's Ingersoll Rand Inc from its 2017-05-12 listing (21.10), while the member was Ingersoll-Rand plc, now `TT` (68.28 that day) | 726 |
| `AA` | Alcoa Corp's when-issued and regular-way bars from 2016-10-18, while the member was Alcoa Inc (now `HWM`) | 52 |

`BACKTEST_UNPRICED` drops those bars (PARA and AA entirely, IR before
2020-04-01, the first snapshot listing `TT`), so the days count as excluded.
Splicing the old company's conId in front (IR ← TT, Q ← IQV) was built,
measured and refused: a position held across the seam books the gap between
two companies as a return (TT 82.59 on 2020-03-31, IR 23.79 on 2020-04-01).
HWM is not aliased for AA for the same reason as DPS: it falls 34% at the
2016-11-01 separation.

Scanned for more: every ticker in the 10-13 artifact for a daily move above
40% inside its membership. Beyond PARA and AA, the hits are real events (the
March 2020 crash and rebound, PCG, SBNY, GL 2024-04-11, FI 2025-10-29, DXCM,
EPAM, CNC, BIIB) plus two one-bar glitches that are not KAN-119's:
`CMCSA` 2017-02-17 (−50% then +101% the next session, the 2:1 split) and
`RJF` 2021-09-22 (+52%, the 3:2 split). Both are follow-ups.

The "different entity" names (`FOX`, `FOXA`, `DOW`, `DD`) are correct as they
stand: IB serves the current company from its own listing, and the earlier
company's days (Twenty-First Century Fox, Dow Chemical, E. I. du Pont) are
excluded, not mispriced.

## Finding 3: the "same ticker" group is not a fetch problem

The ticket's odd group (AVB, EQR, PEP, XEL, …) splits two ways, neither
fixable from the fetch:

- **Delisted per IB.** `reqMatchingSymbols` lists AVB, LEG, SEE, K, EA, HOLX,
  IPG, CTRA, DAY, QRVO and WBD (USD) only on the pseudo-exchange `VALUE`, and
  finds nothing for EQR or Paramount Skydance (`probe3.py`). IB serves no
  history on `VALUE` (backtest-baseline.md, 2026-08-19). The membership
  file still lists AVB and EQR on 2026-08-16. Whatever the corporate event, IB
  treats them as gone.
- **History cut-off.** PEP, XEL, PFG, UAL and NWL are served only from
  late 2017 or 2018, COO from 2023-09-26, ODFL from 2024-05-07.
  `probe2.py` requested every 1-year chunk and retried each empty one as twelve
  1-month requests. All of them came back empty, so this is where IB's history
  for the current conId begins, not a request that spans a listing transfer
  (the hypothesis this probe was written to test, now rejected).

`SATS` was the one recoverable name in the group: EchoStar renamed to `ECHO`
(conId 47965865), and the membership file switches at 2026-07-01. It is
aliased.

## Not done

- **Re-pinning.** `baseline_pin` and `rung0_baseline_pin` are unchanged. The
  operator decides after the first scheduled refresh with the aliases, Tue
  06:30 SGT, which records the new figure in its own artifact. If that
  artifact is pinned, it needs its own bias acceptance (D20 pattern).
- **The acquired, failed and VALUE names** need a survivorship-free vendor,
  deferred per KAN-110 (2026-10-16).
- **Live trading** is untouched: `make_stock_contract` and every live path
  never read either table, a test pins it, and `run_paper.py`'s fetch passes
  neither.

## Per-ticker record

All 168 tickers excluded by either the pinned baseline (2026-09-15) or the
2026-10-13 refresh. Generated by `assets/kan-119/classify.py` from the probe
outputs and `decided.json`. "Acquired" is the corporate record. Its IB evidence
is the Error 200 on the symbol, or the different, later company the symbol
resolves to now.

| Ticker | Category | Days excluded (pinned / 10-13) | IB's own symbol, 2026-10-17 | Evidence |
|---|---|---:|---|---|
| `HES` | acquired | 2,271 / 2,251 | Error 200 | IB: Error 200 |
| `JNPR` | acquired | 2,271 / 2,251 | Error 200 | IB: Error 200 |
| `WBA` | acquired | 2,271 / 2,251 | Error 200 | IB: Error 200 |
| `DFS` | acquired | 2,207 / 2,187 | Error 200 | IB: Error 200 |
| `ANSS` | acquired | 2,073 / 2,073 | Error 200 | IB: Error 200 |
| `MRO` | acquired | 2,085 / 2,065 | Error 200 | IB: Error 200 |
| `WRK` | acquired | 2,021 / 2,001 | Error 200 | IB: Error 200 |
| `CMA` | acquired | 1,957 / 1,937 | Error 200 | IB: Error 200 |
| `PXD` | acquired | 1,957 / 1,937 | Error 200 | IB: Error 200 |
| `ATVI` | acquired | 1,833 / 1,813 | Error 200 | IB: Error 200 |
| `DISH` | acquired | 1,572 / 1,572 | Error 200 | IB: Error 200 |
| `NLSN` | acquired | 1,583 / 1,563 | Error 200 | IB: Error 200 |
| `CTXS` | acquired | 1,520 / 1,500 | Error 200 | IB: Error 200 |
| `CERN` | acquired | 1,456 / 1,436 | Error 200 | IB: Error 200 |
| `PBCT` | acquired | 1,456 / 1,436 | Error 200 | IB: Error 200 |
| `XLNX` | acquired | 1,394 / 1,374 | Error 200 | IB: Error 200 |
| `DRE` | acquired | 1,322 / 1,322 | Error 200 | IB: Error 200 |
| `HBI` | acquired | 1,332 / 1,312 | Error 200 | IB: Error 200 |
| `KSU` | acquired | 1,332 / 1,312 | Error 200 | IB: Error 200 |
| `ALXN` | acquired | 1,268 / 1,248 | Error 200 | IB: Error 200 |
| `INFO` | acquired | 1,196 / 1,196 | 734184075 HARBOR PANAGORA DYC LCC ETF (history from 2024-10-10) | IB: 734184075 HARBOR PANAGORA DYC LCC ETF (history from 2024-10-10) |
| `FLIR` | acquired | 1,204 / 1,184 | Error 200 | IB: Error 200 |
| `VAR` | acquired | 1,204 / 1,184 | Error 200 | IB: Error 200 |
| `ABMD` | acquired | 1,134 / 1,134 | Error 200 | IB: Error 200 |
| `TWTR` | acquired | 1,134 / 1,134 | Error 200 | IB: Error 200 |
| `CXO` | acquired | 1,141 / 1,121 | Error 200 | IB: Error 200 |
| `TIF` | acquired | 1,141 / 1,121 | Error 200 | IB: Error 200 |
| `ETFC` | acquired | 1,080 / 1,060 | Error 200 | IB: Error 200 |
| `NBL` | acquired | 1,080 / 1,060 | Error 200 | IB: Error 200 |
| `CTLT` | acquired | 1,005 / 1,005 | Error 200 | IB: Error 200 |
| `JWN` | acquired | 1,016 / 996 | Error 200 | IB: Error 200 |
| `AGN` | acquired | 952 / 932 | Error 200 | IB: Error 200 |
| `RTN` | acquired | 952 / 932 | Error 200 | IB: Error 200 |
| `CELG` | acquired | 827 / 807 | Error 200 | IB: Error 200 |
| `VIAB` | acquired | 827 / 807 | Error 200 | IB: Error 200 |
| `APC` | acquired | 763 / 743 | 854080544 ARKO PETROLEUM CORP (history from 2026-02-12) | IB: 854080544 ARKO PETROLEUM CORP (history from 2026-02-12) |
| `FL` | acquired | 763 / 743 | Error 200 | IB: Error 200 |
| `RHT` | acquired | 763 / 743 | Error 200 | IB: Error 200 |
| `TSS` | acquired | 763 / 743 | Error 200 | IB: Error 200 |
| `MXIM` | acquired | 693 / 693 | Error 200 | IB: Error 200 |
| `LLL` | acquired | 699 / 679 | Error 200 | IB: Error 200 |
| `ESRX` | acquired | 636 / 616 | Error 200 | IB: Error 200 |
| `NFX` | acquired | 636 / 616 | 898286451 CORGI NFLX 2X DAILY ETF (history from 2026-07-07) | IB: 898286451 CORGI NFLX 2X DAILY ETF (history from 2026-07-07) |
| `SCG` | acquired | 636 / 616 | Error 200 | IB: Error 200 |
| `AET` | acquired | 575 / 555 | Error 200 | IB: Error 200 |
| `CA` | acquired | 575 / 555 | Error 200 | IB: Error 200 |
| `COL` | acquired | 575 / 555 | Error 200 | IB: Error 200 |
| `SRCL` | acquired | 575 / 555 | Error 200 | IB: Error 200 |
| `EVHC` | acquired | 502 / 502 | Error 200 | IB: Error 200 |
| `GGP` | acquired | 512 / 492 | Error 200 | IB: Error 200 |
| `XL` | acquired | 512 / 492 | Error 200 | IB: Error 200 |
| `CSRA` | acquired | 449 / 429 | 910132626 COHEN & STERS RL AST ACT ETF (history from 2026-08-12) | IB: 910132626 COHEN & STERS RL AST ACT ETF (history from 2026-08-12) |
| `MON` | acquired | 449 / 429 | Error 200 | IB: Error 200 |
| `TWX` | acquired | 449 / 429 | Error 200 | IB: Error 200 |
| `WCG` | acquired | 377 / 377 | Error 200 | IB: Error 200 |
| `BCR` | acquired | 385 / 365 | Error 200 | IB: Error 200 |
| `PDCO` | acquired | 385 / 365 | Error 200 | IB: Error 200 |
| `SNI` | acquired | 385 / 365 | Error 200 | IB: Error 200 |
| `LVLT` | acquired | 324 / 304 | Error 200 | IB: Error 200 |
| `ANDV` | acquired | 251 / 251 | Error 200 | IB: Error 200 |
| `RAI` | acquired | 261 / 241 | Error 200 | IB: Error 200 |
| `SPLS` | acquired | 261 / 241 | 845913334 PIMCO US STCK ACTV BOND ETF (history from 2026-01-16) | IB: 845913334 PIMCO US STCK ACTV BOND ETF (history from 2026-01-16) |
| `WFM` | acquired | 261 / 241 | Error 200 | IB: Error 200 |
| `DNB` | acquired | 198 / 178 | Error 200 | IB: Error 200 |
| `MJN` | acquired | 198 / 178 | Error 200 | IB: Error 200 |
| `SWN` | acquired | 198 / 178 | Error 200 | IB: Error 200 |
| `TGNA` | acquired | 198 / 178 | Error 200 | IB: Error 200 |
| `HAR` | acquired | 135 / 115 | Error 200 | IB: Error 200 |
| `LLTC` | acquired | 135 / 115 | Error 200 | IB: Error 200 |
| `SE` | acquired | 135 / 115 | 292735472 SEA LTD-ADR (history from 2017-10-20) | IB: 292735472 SEA LTD-ADR (history from 2017-10-20) |
| `STJ` | acquired | 135 / 115 | Error 200 | IB: Error 200 |
| `LM` | acquired | 73 / 53 | Error 200 | IB: Error 200 |
| `CPGX` | acquired | 10 / 0 | Error 200 | IB: Error 200 |
| `DO` | acquired | 10 / 0 | Error 200 | IB: Error 200 |
| `EMC` | acquired | 10 / 0 | 630724899 GLB X EMRG MRKT GRT CNSUMR (history from 2023-05-15) | IB: 630724899 GLB X EMRG MRKT GRT CNSUMR (history from 2023-05-15) |
| `HOT` | acquired | 10 / 0 | Error 200 | IB: Error 200 |
| `TYC` | acquired | 10 / 0 | Error 200 | IB: Error 200 |
| `BK` | renamed: aliased | 2,458 / 2,438 | Error 200 | BNY conId 44986804 BANK OF NEW YORK MELLON CORP; until 2026-07-01; covers 2435/2438 member sessions; largest daily move 16% (2020-03-13) |
| `ABC` | renamed: aliased | 1,770 / 1,750 | Error 200 | COR conId 1714996 CENCORA INC; until 2023-10-01; covers 1747/1750 member sessions; largest daily move 13% (2016-10-28) |
| `PKI` | renamed: aliased | 1,707 / 1,687 | Error 200 | RVTY conId 6737 REVVITY INC; until 2023-07-01; covers 1684/1687 member sessions; largest daily move 10% (2020-03-16) |
| `RE` | renamed: aliased | 1,572 / 1,572 | Error 200 | EG conId 6477725 EVEREST GROUP LTD; until 2023-10-01; covers 1572/1572 member sessions; largest daily move 14% (2020-03-16) |
| `FBHS` | renamed: aliased | 1,583 / 1,563 | Error 200 | FBIN conId 94078796 FORTUNE BRANDS INNOVATIONS I; until None; covers 1560/1563 member sessions; largest daily move 16% (2020-03-24) |
| `FLT` | renamed: aliased | 1,445 / 1,445 | Error 200 | CPAY conId 691994611 CORPAY INC; until 2024-04-01; covers 1445/1445 member sessions; largest daily move 16% (2020-11-09) |
| `ANTM` | renamed: aliased | 1,456 / 1,436 | Error 200 | ELV conId 173962302 ELEVANCE HEALTH INC; until 2022-07-01; covers 1433/1436 member sessions; largest daily move 17% (2020-03-16) |
| `BLL` | renamed: aliased | 1,456 / 1,436 | Error 200 | BALL conId 5052 BALL CORP; until 2022-07-01; covers 1433/1436 member sessions; largest daily move 12% (2020-03-24) |
| `FB` | renamed: aliased | 1,456 / 1,436 | 794639762 PROSHARES S&P DYNAMIC BUFFER (history from 2025-06-26) | META conId 107113386 META PLATFORMS INC-CLASS A; until 2022-07-01; covers 1433/1436 member sessions; largest daily move 26% (2022-02-03) |
| `GPS` | renamed: aliased | 1,394 / 1,374 | Error 200 | GAP conId 7765 GAP INC/THE; until None; covers 1371/1374 member sessions; largest daily move 30% (2020-03-24) |
| `WLTW` | renamed: aliased | 1,394 / 1,374 | Error 200 | WTW conId 217947855 WILLIS TOWERS WATSON PLC; until 2022-04-01; covers 1371/1374 member sessions; largest daily move 15% (2020-03-24) |
| `LB` | renamed: aliased | 1,268 / 1,248 | 712414332 LANDBRIDGE CORP (history from 2024-06-28) | BBWI conId 502252263 BATH & BODY WORKS INC; until 2021-10-01; covers 1245/1248 member sessions; largest daily move 39% (2020-03-24) |
| `PEAK` | renamed: aliased | 1,067 / 1,067 | Error 200 | DOC conId 389700396 HEALTHPEAK PROPERTIES INC; until 2024-04-01; covers 1067/1067 member sessions; largest daily move 23% (2020-03-16) |
| `MYL` | renamed: aliased | 1,080 / 1,060 | Error 200 | VTRS conId 454859695 VIATRIS INC; until 2021-01-01; covers 1057/1060 member sessions; largest daily move 24% (2019-05-07) |
| `CTL` | renamed: aliased | 1,016 / 996 | Error 200 | LUMN conId 446613345 LUMEN TECHNOLOGIES INC; until 2020-10-01; covers 991/996 member sessions; largest daily move 14% (2020-03-24) |
| `UTX` | renamed: aliased | 952 / 932 | Error 200 | RTX conId 415342104 RTX CORP; until 2020-07-01; covers 929/932 member sessions; largest daily move 16% (2020-03-24) |
| `ARNC` | renamed: aliased | 879 / 879 | Error 200 | HWM conId 403121976 HOWMET AEROSPACE INC; until 2020-07-01; covers 879/879 member sessions; largest daily move 26% (2020-03-24) |
| `HCP` | renamed: aliased | 827 / 807 | Error 200 | DOC conId 389700396 HEALTHPEAK PROPERTIES INC; until 2024-04-01; covers 804/807 member sessions; largest daily move 5% (2018-12-24) |
| `JEC` | renamed: aliased | 827 / 807 | Error 200 | J conId 582332987 JACOBS SOLUTIONS INC; until 2020-01-01; covers 804/807 member sessions; largest daily move 11% (2018-11-20) |
| `SYMC` | renamed: aliased | 827 / 807 | Error 200 | GEN conId 390332324 GEN DIGITAL INC; until 2023-01-01; covers 804/807 member sessions; largest daily move 33% (2018-05-11) |
| `HFC` | renamed: aliased | 755 / 755 | Error 200 | DINO conId 550324178 HF SINCLAIR CORP; until None; covers 755/755 member sessions; largest daily move 28% (2020-11-09) |
| `TMK` | renamed: aliased | 763 / 743 | Error 200 | GL conId 375868349 GLOBE LIFE INC; until 2019-10-01; covers 740/743 member sessions; largest daily move 5% (2018-12-04) |
| `HRS` | renamed: aliased | 699 / 679 | Error 200 | LHX conId 371886040 L3HARRIS TECHNOLOGIES INC; until 2019-07-01; covers 676/679 member sessions; largest daily move 12% (2018-10-15) |
| `KORS` | renamed: aliased | 636 / 616 | Error 200 | CPRI conId 347409760 CAPRI HOLDINGS LTD; until 2019-04-01; covers 613/616 member sessions; largest daily move 22% (2017-08-08) |
| `BHGE` | renamed: aliased | 566 / 566 | Error 200 | BKR conId 281316445 BAKER HUGHES CO; until 2020-01-01; covers 566/566 member sessions; largest daily move 9% (2017-11-06) |
| `LUK` | renamed: aliased | 449 / 429 | Error 200 | JEF conId 318911976 JEFFERIES FINANCIAL GROUP IN; until 2018-07-01; covers 426/429 member sessions; largest daily move 12% (2018-04-09) |
| `WYN` | renamed: aliased | 449 / 429 | Error 200 | TNL conId 469015035 TRAVEL + LEISURE CO; until None; covers 426/429 member sessions; largest daily move 9% (2017-04-26) |
| `CBG` | renamed: aliased | 385 / 365 | Error 200 | CBRE conId 95514904 CBRE GROUP INC - A; until 2018-04-01; covers 362/365 member sessions; largest daily move 8% (2017-02-10) |
| `HCN` | renamed: aliased | 385 / 365 | Error 200 | WELL conId 207739931 WELLTOWER INC; until 2018-04-01; covers 361/365 member sessions; largest daily move 4% (2017-12-19) |
| `PCLN` | renamed: aliased | 385 / 365 | 822733785 PICTET CLEANER PLANET (history from 2025-10-16) | BKNG conId 308728373 BOOKING HOLDINGS INC; until 2018-04-01; covers 361/365 member sessions; largest daily move 14% (2017-11-07) |
| `COH` | renamed: aliased | 324 / 304 | Error 200 | TPR conId 292491487 TAPESTRY INC; until 2018-01-01; covers 301/304 member sessions; largest daily move 15% (2017-08-15) |
| `DLPH` | renamed: aliased | 324 / 304 | Error 200 | APTV conId 748351585 APTIV PLC; until 2018-01-01; covers 301/304 member sessions; largest daily move 11% (2017-05-03) |
| `SATS` | renamed: aliased | 62 / 62 | Error 200 | ECHO conId 47965865 ECHOSTAR CORP; until 2026-07-01; IB ECHO conId prices all 62 member sessions (1y fetch); no move above 10% |
| `AVB` | delisted per IB | 2,510 / 2,510 | Error 200 | symbol search lists AVB only on VALUE (conId 5026974) |
| `EQR` | delisted per IB | 2,510 / 2,510 | Error 200 | Error 200; symbol search for Equity Residential returns nothing |
| `EA` | delisted per IB | 2,490 / 2,470 | Error 200 | Error 200; symbol search lists EA (USD) only on VALUE (conId 268995) |
| `HOLX` | delisted per IB | 2,458 / 2,438 | Error 200 | Error 200; symbol search lists HOLX (USD) only on VALUE (conId 270188) |
| `IPG` | delisted per IB | 2,335 / 2,315 | Error 200 | Error 200; symbol search lists IPG (USD) only on VALUE (conId 8514) |
| `K` | delisted per IB | 2,335 / 2,315 | Error 200 | Error 200; symbol search lists K (USD) only on VALUE (conId 8758) |
| `QRVO` | delisted per IB | 0 / 2,065 | Error 200 | symbol search lists QRVO only on VALUE (conId 179002152) |
| `SEE` | delisted per IB | 1,833 / 1,813 | Error 200 | Error 200; symbol search lists SEE (USD) only on VALUE (conId 4082282) |
| `LEG` | delisted per IB | 1,332 / 1,312 | Error 200 | symbol search lists LEG only on VALUE (conId 9081) |
| `CTRA` | delisted per IB | 1,126 / 1,126 | Error 200 | Error 200; symbol search lists CTRA (USD) only on VALUE (conId 5946) |
| `WBD` | delisted per IB | 0 / 1,074 | Error 200 | Error 200; symbol search lists WBD (USD) only on VALUE (conId 554208351) |
| `DAY` | delisted per IB | 502 / 502 | Error 200 | Error 200; symbol search lists DAY (USD) only on VALUE (conId 315543544) |
| `PSKY` | delisted per IB | 0 / 259 | Error 200 | Error 200; symbol search finds no Paramount Skydance US listing |
| `DISCA` | renamed: refused | 1,456 / 1,436 | Error 200 | successor WBD returns Error 200; symbol search lists WBD only on VALUE (conId 554208351) |
| `DISCK` | renamed: refused | 1,456 / 1,436 | Error 200 | successor WBD returns Error 200 (VALUE); class C converted into a different share line in 2022 |
| `COG` | renamed: refused | 1,332 / 1,312 | Error 200 | successor CTRA returns Error 200; symbol search lists CTRA (USD) only on VALUE (conId 5946) |
| `ADS` | renamed: refused | 1,016 / 996 | Error 200 | successor BFH conId 12362013: HMDS returns no data for any member session (Error 162) |
| `XEC` | renamed: refused | 889 / 869 | Error 200 | merged into Coterra (CTRA), itself on VALUE (Error 200) |
| `CBS` | renamed: refused | 827 / 807 | Error 200 | candidate PARA resolves to conId 880582719 BANZAI INTERNATIONAL INC, not Paramount; PSKY returns Error 200 |
| `STI` | renamed: refused | 827 / 807 | 782964354 SOLIDION TECHNOLOGY INC (history from ?) | merged into BB&T, renamed Truist (TFC); TFC is BB&T's series. IB's STI today is SOLIDION TECHNOLOGY (after the membership) |
| `NLOK` | renamed: refused | 756 / 756 | Error 200 | GEN conId 390332324 prices the window, but falls 40% inside it (2020-01-31 28.42 -> 02-03 17.15, the unadjusted special dividend) |
| `CDAY` | renamed: refused | 626 / 626 | Error 200 | successor DAY returns Error 200; symbol search lists DAY only on VALUE (conId 315543544) |
| `VIAC` | renamed: refused | 567 / 567 | Error 200 | candidate PARA resolves to conId 880582719 BANZAI INTERNATIONAL INC, not Paramount; PSKY returns Error 200 |
| `PX` | renamed: refused | 575 / 555 | Error 200 | merged into Linde plc; LIN conId 617155599 history starts 2018-10-31 |
| `DPS` | renamed: refused | 512 / 492 | Error 200 | KDP conId 324057162 prices the window, but falls 82% at the merger (2018-07-09 123.66 -> 07-10 22.19, the unadjusted special dividend) |
| `DWDP` | renamed: refused | 438 / 438 | Error 200 | DD conId 887235923 is DowDuPont, but the membership file uses DD for E. I. du Pont before 2017; the merger window also spans the Dow and Corteva separations |
| `CHK` | renamed: refused | 385 / 365 | Error 200 | equity cancelled in the 2020 bankruptcy; EXE conId 470458975 is the new equity, history from 2021-02-10 |
| `BHI` | renamed: refused | 261 / 241 | Error 200 | BKR conId 281316445 history starts 2017-07-05 (the BHGE merger); Baker Hughes Inc's own series is not served |
| `TSO` | renamed: refused | 261 / 241 | Error 200 | successor ANDV returns Error 200 (acquired by MPC) |
| `YHOO` | renamed: refused | 198 / 178 | Error 200 | successor AABA returns Error 200 |
| `Q` | renamed: refused | 63 / 63 | 824788107 QNITY ELECTRONICS INC (history from 2025-10-27) | Q is QNITY ELECTRONICS today (conId 824788107, history from 2025-10-27, correct for its 2025-26 membership); the 2017 Quintiles IMS quarter would need IQV spliced in front, refused (see unpriced IR) |
| `UA C` | renamed: refused | 63 / 53 | Error 200 | UA conId 225590118 (UNDER ARMOUR INC-CLASS C) returns no bars; the membership file's UA is a different share class in that quarter |
| `COO` | history cut-off | 1,756 / 1,746 | 685217803 COOPER COS INC/THE (history from 2023-09-26) | current conId served only from 2023-09-26; earlier 1-year and 1-month requests return no data |
| `ODFL` | history cut-off | 1,093 / 1,093 | 272667 OLD DOMINION FREIGHT LINE (history from 1991-10-24) | current conId served only from 2024-05-07; earlier 1-year and 1-month requests return no data |
| `NWL` | history cut-off | 561 / 541 | 10627 NEWELL BRANDS INC (history from ?) | current conId served only from 2018-12-11; earlier 1-year and 1-month requests return no data |
| `UAL` | history cut-off | 497 / 477 | 79498203 UNITED AIRLINES HOLDINGS INC (history from ?) | current conId served only from 2018-09-10; earlier 1-year and 1-month requests return no data |
| `XEL` | history cut-off | 324 / 304 | 10517 XCEL ENERGY INC (history from ?) | current conId served only from 2018-01-02; earlier 1-year and 1-month requests return no data |
| `PEP` | history cut-off | 317 / 297 | 11017 PEPSICO INC (history from ?) | current conId served only from 2017-12-20; earlier 1-year and 1-month requests return no data |
| `PFG` | history cut-off | 315 / 295 | 11032 PRINCIPAL FINANCIAL GROUP (history from ?) | current conId served only from 2017-12-18; earlier 1-year and 1-month requests return no data |
| `SIVB` | failed | 1,260 / 1,260 | Error 200 | bankruptcy / wind-down; IB: Error 200 |
| `FRC` | failed | 1,071 / 1,071 | Error 200 | bankruptcy / wind-down; IB: Error 200 |
| `BBBY` | failed | 261 / 241 | Error 200 | bankruptcy / wind-down; IB: Error 200 |
| `MNK` | failed | 261 / 241 | Error 200 | bankruptcy / wind-down; IB: Error 200 |
| `ENDP` | failed | 135 / 115 | Error 200 | bankruptcy / wind-down; IB: Error 200 |
| `FTR` | failed | 135 / 115 | Error 200 | bankruptcy / wind-down; IB: Error 200 |
| `FOX` | different entity under the ticker | 623 / 603 | 356858007 FOX CORP - CLASS B (history from 2019-03-13) | IB FOX is conId 356858007 FOX CORP - CLASS B from 2019-03-13; Twenty-First Century Fox's days (acquired by Disney) are not served |
| `FOXA` | different entity under the ticker | 623 / 603 | 356858008 FOX CORP - CLASS A (history from 2019-03-13) | IB FOXA is conId 356858008 FOX CORP - CLASS A from 2019-03-13; Twenty-First Century Fox's days (acquired by Disney) are not served |
| `DOW` | different entity under the ticker | 261 / 241 | 356576040 DOW INC (history from 2019-03-20) | IB DOW is conId 356576040 DOW INC from 2019-03-20; Dow Chemical's days (merged into DowDuPont 2017) are not served |
| `DD` | different entity under the ticker | 241 / 221 | 887235923 DUPONT DE NEMOURS INC (history from 2017-09-01) | IB DD is conId 887235923 DUPONT DE NEMOURS INC from 2017-09-01 (DowDuPont); E. I. du Pont's days are not served |
| `IR` | IB series is another company: unpriced | 163 / 143 | 404509190 INGERSOLL-RAND INC (history from 2017-05-12) | IB IR is conId 404509190 INGERSOLL-RAND INC (Gardner Denver), history from 2017-05-12 at 21.10 while the member, Ingersoll-Rand plc (now TT, conId 406709270), traded at 68.28; IR is that company from the first snapshot listing TT |
| `AA` | IB series is another company: unpriced | 21 / 1 | 251962528 ALCOA CORP (history from 2016-10-18) | IB AA is conId 251962528 ALCOA CORP, history from 2016-10-18 (the 2016 separation); the member until 2016 was Alcoa Inc, now HWM, whose series falls 34% at the 2016-11-01 separation, so it is not aliased either |
| `PARA` | IB series is another company: unpriced | 388 / 0 | 880582719 BANZAI INTERNATIONAL INC (history from 2021-02-12) | IB PARA is conId 880582719 BANZAI INTERNATIONAL INC, history from 2021-02-12, closing 98,000 -> 130 (split-adjusted) across Paramount's 2022-04..2025-07 membership; PSKY returns Error 200 |
| `AVGO` | isolated missing session | 1 / 1 | 313130367 BROADCOM INC (history from 2016-02-02) | priced; one membership session without a bar |
| `EXPD` | isolated missing session | 1 / 1 | 269082 EXPEDITORS INTL WASH INC (history from 1984-09-26) | priced; one membership session without a bar |
| `FI` | isolated missing session | 1 / 1 | Error 200 | priced; one membership session without a bar |
| `FITB` | isolated missing session | 1 / 1 | 269318 FIFTH THIRD BANCORP (history from 1980-03-17) | priced; one membership session without a bar |
| `LUMN` | isolated missing session | 1 / 1 | 446613345 LUMEN TECHNOLOGIES INC (history from 2006-11-16) | priced; one membership session without a bar |
| `SLG` | isolated missing session | 1 / 1 | 537789289 SL GREEN REALTY CORP (history from 1997-08-15) | priced; one membership session without a bar |
