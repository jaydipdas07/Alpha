# Intraday edge survey v2 — 2026-07-07 ([You]: "fetch the best strategies man or computer has ever created")

Round two of the internet/literature sweep, run AFTER the v1 families closed (F1 0/4, F2
0/36 taker + the maker arc: 5 frozen → 3 reads spent → 0 holdout passes, FW 0/48, F3 0/40
taker, NGE 0/2). The filter this round: mechanisms with a **structural cause** we have NOT
yet tested, implementable on OUR venues (Delta/Binance derivatives at maker economics; Kite
NSE equities/F&O with the SF4 session machinery), free data first. No pre-registrations in
this document; each family below registers separately with its own tiny grid.

## The headline correction from round one

**F1 tested the wrong Zarattini paper.** The independently-replicated profitable spec is
the **5-minute Opening-Range Breakout on "Stocks in Play"** (Zarattini–Barbon–Aziz, SSRN
4729284): a *relative-volume-filtered stock universe* (news-driven attention names, RVOL
high at the open), direction = the first 5-minute candle's sign, breakout entry, **VWAP /
range trailing stop**, EOD flat. QuantConnect's independent replication reports **Sharpe
2.4, beta ≈ 0**; the headline backtest 1,484% vs 169% for passive QQQ (2016–2023, leverage
notes apply). Our F1 ran the *noise-area* index variant instead — a materially weaker form.
Indian-market side-evidence: volume-conditioned momentum beats price momentum by 2–7%/yr
(Pacific-Basin Finance Journal, Indian evidence); RVOL>2-before-10:00 is standard
practitioner "in play" screening.

## Ranked new families (G-series)

**G1 — Stocks-in-Play 5-min ORB on NSE (the replicated spec).** Universe = liquid NSE names
with high opening relative volume (RVOL vs its own trailing average, computed from OUR
minute data); first-5-min candle direction; breakout entry; VWAP-or-range trailing stop;
MIS flat. Needs the WIDE minute universe (ingest running: ~100 names, 2019→2026; the 4-name
store was never enough). **Declared bias:** the v1 universe is TODAY'S liquid names
backfilled — survivorship-tainted; v2 needs point-in-time constituents (index factsheets).
Costs: the real equity-intraday stack (GST fixed in #177). The strongest external evidence
of any family this hunt has touched: published + independently replicated + volume-filter
evidence replicated separately in India.

**G2 — Taker-flow imbalance on BTC/ETH perps (aggTrades), maker execution.** The
microstructure-native family our own data has always supported and we never tested: signed
taker-flow imbalance (the aggTrades `isBuyerMaker` flag we already store) z-scored over a
trailing window; enter WITH the flow (continuation) at 1–5m horizons; **post-only entries
through the #184 fill instrument** (strict trade-through, miss = no trade), reduce-only
exits. Literature: flow imbalance explains a substantial fraction of short-horizon return
variation; VPIN-style toxicity predicts jumps/vol (Easley et al. 2024-era crypto
extensions; RIBAF 2025 "Bitcoin wild moves"). Sign-prediction evidence is weaker than
vol-prediction — the registration keeps the grid tiny and expects most of it to die.
Data: 24mo × 1s × BTC/ETH/alts in the tick store, sealed.

**G3 — The NSE 15:15–15:25 square-off flow.** Every Indian broker force-liquidates retail
MIS books in the 15:10–15:26 window (Zerodha ~15:20 equity; market orders, slippage
documented by the brokers themselves). Mechanism CERTAIN (mechanical, scheduled,
directional-with-the-day's-retail-book); the sign/size on the tape is the open question —
no academic study surfaced (genuinely under-mined). Hypothesis: day-direction-aligned
retail books unwind AGAINST the day's move into the window, reverting 15:25→15:30.
Testable free on the minute universe; entries inside our own no-new-entry gate need care
(the strategy would trade 14:45–15:09 positioning INTO the flow, exiting at 15:05... or a
15:05-entry variant needs a widened session-gate registration — design carefully).

**G4 — Expiry-day max-pain drift (final 90 minutes, NIFTY Tuesdays).** D-1 EOD chain OI
(our own store) → max-pain strike; on expiry days with |spot − MP| ∈ [0.3%, 1.5%] and a
quiet tape, the documented tendency is a drift toward MP concentrated 14:00–15:30.
Evidence MIXED (practitioner-strong, academically weak for indices — "gravity, not a
magnet"); free to test; register narrow and expect modest.

## Noted and deliberately skipped

- **US-session/ETF-window concentration (15:00–16:00 UTC, post-spot-ETF era)**: real
  (6.7% of daily volume vs 4.5% pre-ETF; institutions ~63% of US-hours volume) but the
  2024+ era gives a short sample and the closed hour-window family already swept adjacent
  clocks; revisit as a NEW registration only with a 2024+-era-specific design.
- **FOMC/CPI event drift**: ~0.6pp first-hour jumps, 2.5–3× volume — but 8–12 events/yr
  is power-starved for our DSR gate. Skip.
- **Intraday perp-spot basis MR**: practitioner consensus is fees eat it on Binance
  majors even before our test; cheap to run someday under maker; low prior.
- **HFT market-making / LOB ML (Hawkes, deep OFI)**: needs L2 feeds + latency we don't
  have; out of scope by infrastructure.

## Sources

- Zarattini–Barbon–Aziz, "A Profitable Day Trading Strategy For The U.S. Equity Market" — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284 (+ https://concretumgroup.com/a-profitable-day-trading-strategy-for-the-u-s-equity-market/)
- QuantConnect replication, "Opening Range Breakout for Stocks in Play" — https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/
- "Bitcoin wild moves: order flow toxicity and price jumps" (RIBAF 2025) — https://www.sciencedirect.com/science/article/pii/S0275531925004192
- OFI prediction overview — https://www.emergentmind.com/topics/order-flow-imbalance-prediction
- Momentum/liquidity, Indian evidence — https://www.sciencedirect.com/science/article/abs/pii/S0927538X23002640
- Zerodha square-off timings — https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings (+ broker policy pages: 5paisa, PL Capital, Dhan)
- Max-pain practitioner guides (NiftyTrader/Upstox/StockMojo) — https://www.niftytrader.in/markets/max-pain-in-options-expiry-trading-guide-india/
- Bitcoin overnight/session effects — https://quantpedia.com/how-to-profitably-trade-bitcoins-overnight-sessions/
- FOMC/crypto intraday event risk — https://www.sciencedirect.com/science/article/abs/pii/S1544612326006021
