# Intraday Edge Survey — July 2026

**Status:** research survey (web + literature), commissioned by [You] 2026-07-04 ("find sustainable
intraday strategies — dig into HFTs, banks, traders, their research"). **No pre-registrations made,
no holdout reads spent.** This document is the evidence base for the next round of pre-registered
families; the discipline (pre-register → in-sample DSR gate → one holdout read) is unchanged.

---

## 1. Why the first hunts found nothing — and what that actually tells us

The M3.0→M5.5b campaigns mined **free EOD/1m data with generic price signals** (single-instrument
TA, cross-sectional momentum/reversal, funding carry, basis carry, EOD condors). That is the single
most-arbitraged corner of every market — the signals every textbook publishes, on the data everyone
has, at horizons everyone can trade. The gate rejecting all of it (with in-sample promotes that
*sign-flipped* on the holdout) is the discipline working, not the machine failing.

**What was stopping us, concretely:**
1. **Input class, not method.** We never tested a single *structural* edge — time-of-day flow
   effects, dealer-hedging pressure, expiry mechanics, cross-venue information diffusion,
   auction/settlement behaviour. The documented practitioner edges live almost entirely there.
2. **Two data gaps, both cheap.** Indian intraday minute data (index: now **free** via Kite
   historical; options: ~₹4.2k+GST per index-year from NSE vendors) and crypto tick/1s data
   (**free** — Binance bulk downloads). Nothing we lacked costs real money.
3. **Latency class confusion.** True HFT (sub-ms market making, latency arb, index arb) is a
   colocation game we correctly never entered. But a large documented middle band — **seconds to
   hours** — needs no colocation, and that is exactly our stack's class.

## 2. The edge-class map (what the record actually supports)

### 2.1 Intraday time-series momentum / opening range — strong, multiply-replicated
- **Gao, Han, Li, Zhou (JFE 2018)** — first half-hour return (+ overnight) predicts the last
  half-hour on SPY, R²≈1.6–2.6%; stronger on volatile/high-volume/news days; replicated on 10+
  ETFs and international markets. [ssrn.com/abstract=2440866]
- **Baltussen, Da, Lammers, Martens (JFE 2021)** — the *mechanism*: hedging **short-gamma**
  exposure forces dealers/leveraged-ETF issuers to trade **with** the market late in the day.
  Documented across **60+ futures, 1974–2020**; the effect concentrates on days with **negative
  net gamma exposure (NGE)** and reverts over following days. [ssrn.com/abstract=3760365]
- **Zarattini, Aziz, Barbon (2024)** — tradeable form: "noise-area" bands around the open
  (±14-day average |move-to-minute|, gap-adjusted); breakout at HH:00/HH:30 → hold to close, VWAP
  trail. SPY 2007–2024: **+19.6%/yr net, Sharpe 1.33** (their costs). [ssrn.com/abstract=4824172]
- **Independent replication (Quantitativo)** — ES/NQ futures 2010–2025 with strict costs
  ($2.25/contract + 0.25-tick slip): raw replication **Sharpe 0.91** (+2bps/trade edge,
  significant); tuned variants 1.25–1.67. The *raw* number is the honest one.
- **ORB family (Zarattini/Aziz 2023; +Barbon 2024)** — 5-min opening-range breakout: QQQ 2016–2023
  Sharpe ~1.12 net; "Stocks in Play" (top relative-volume names) Sharpe ~2.4–2.8 claimed;
  **QuantConnect community replication confirmed in-period but found collapse outside 2016–2020**
  and ~17% win rates — heavily regime-dependent. Treat ORB as the weaker cousin; the band/noise
  formulation with the gamma conditioning is the better-motivated core.
- **India status:** no published NIFTY replication surfaced — testing it on free Kite index minute
  data **is** the research question, and the JFE-2021 cross-market breadth makes the prior decent.

### 2.2 Short-dated option premium harvesting — real flow pays it; structure decides survival
- **Who pays:** SEBI's own studies — **91% of ~9.6M individual F&O traders lost money in FY24-25,
  ₹1.06 lakh crore net** (₹1.8L cr FY22–24); US mirror: **Beckmeyer/Branger/Gayda** — retail loses
  ~$350k/day in 0DTE options, ~60% of it transaction costs, buyers worst; market makers carry the
  net-short book and collect. The flow that funds disciplined sellers exists at enormous scale.
- **India post-reform reality (verified 2026-07):** weekly expiries only **NIFTY (NSE, Tuesday)**
  and **SENSEX (BSE, Thursday)**; BANKNIFTY weekly is dead (monthly, last Tuesday). Lots 3×
  (NIFTY 75 ≈ ₹19L notional). Expiry-day short-side **+2% ELM**; calendar-spread margin benefit
  removed on expiry day (May 2026). Intraday delta-equivalent position caps (₹5,000cr net) are
  irrelevant at our size. Practitioner consensus: naive 920 straddles "stopped working" ~2020;
  SL-per-leg + entry-time/vol filters + expiry-day variants are where the ecosystem migrated.
- **Our asset:** the engine's options capability (#161) + **8.9M contract-days of EOD chains
  2016–2026** (gamma/OI conditioning) + a **virgin ~2.1y options holdout**. What's missing for
  *intraday* families is minute-level premiums — the one data purchase worth making.

### 2.3 Cross-venue / cross-asset information diffusion — seconds-scale, our latency class
- Tick-level studies measure **BTC → altcoin lead of 16–118s (mean ≈57s)**; a 2026 Springer paper
  validates lag-trading strategies on high-frequency BTC→alt transmission. Big-venue → small-venue
  lead (Binance → laggards) is the classic mid-freq prop trade; Delta India (our live venue,
  FIU-registered) is structurally a laggard venue with taker ~5.9bps all-in (incl. 18% GST),
  maker ~2.4bps.
- Feasible for us: signal = Binance BTC/majors return over trailing k seconds; act on the lagging
  instrument (alt perp, or Delta's book vs Binance's). Mumbai↔Tokyo RTT ~120–150ms ≪ 57s.
- **Honest caveat:** documented average edges are small; fees decide. This family needs 1s bars
  from free Binance aggTrades and a costs-first design (maker-biased where possible).

### 2.4 Crypto time-of-day / calendar seasonality — free to test, decent priors
- **QuantPedia/Padysák-Vojtko:** BTC 21:00→23:00 UTC long window ≈ **33%/yr at ~21% vol**
  (2015–2022, Gemini hourly); worst hours 03:00–04:00; intraday vs overnight character flips with
  NYSE hours.
- **Concretum (Zarattini's shop):** "Monday Asia Open" — high-freq trend works from Sun ~19:00 ET
  for ~24h; ensemble Sharpe ~1.6 **gross** 2018–2025 vs 0.8 long-only; strengthened post-2020.
- Robot Wealth (prop+education, generally honest about decay) runs crypto **intraday seasonality**
  and **short-term reversal** as live family classes for retail-scale books.
- We have never conditioned anything on hour-of-day/day-of-week. Our 1m crypto store aggregates to
  hourly for free; the panel machinery + holdout floor-seeding (#146) extend directly.

### 2.5 Liquidation-cascade reversion (crypto) — motivated, data-gated
Forced-liquidation clusters overshoot and mean-revert (practitioner + academic microstructure
consensus). Binance's forceOrder stream is free **going forward**; deep history is vendor-paid.
Park until we're recording live (start the recorder early, research later).

### 2.6 What does NOT trickle down to us (explicitly out)
Sub-millisecond market making, cross-exchange latency arbitrage, index/ETF arbitrage, auction
sniping: colocation + fee-tier + queue-position games (Optiver/Jane Street/Jump class). Every
credible source places these behind capex walls we should not attempt. Their existence is why we
*avoid* competing on speed and compete on **structure + discipline** instead.

## 3. Cost stacks that gate design (Budget 2026 verified)
- **NIFTY futures:** STT **0.05% sell-side** (was 0.02%, from 2026-04-01) + stamp 0.002% buy +
  exchange/SEBI/GST ⇒ **~6bps round trip** + ~0.2–0.8bps spread. One-a-day intraday holds
  targeting 20–40bp moves clear this; scalping does not.
- **Index options:** STT **0.15% of premium** sell-side (was 0.1%) + exchange ~0.05% of premium;
  premium-based, so seller-side economics survive; the 2026 hikes shave ~5–10bps of premium.
- **Delta India perp:** taker 0.05%+GST ≈ 5.9bps, maker 0.02%+GST ≈ 2.4bps — matches our
  `costs.yaml crypto_perp` honesty. `costs.yaml` needs the Budget-2026 Indian rates when F1/F5 build.

## 4. Data map (what unlocks what)
| Source | Coverage | Cost | Unlocks |
|---|---|---|---|
| Kite historical API (base sub — **now free**) | minute candles, index/equities/**active** F&O; 60-day chunks; **no expired options**; index minute back to ~2015 | ₹0 | F1 NIFTY intraday momentum/ORB |
| Binance bulk data (data.binance.vision) | aggTrades/klines/funding, full history, ms resolution | ₹0 | F2 seasonality, F3 lead-lag 1s bars |
| Our own options store (#150) | 8.9M contract-days EOD NIFTY/BANKNIFTY 2016–2026 | ₹0 (have it) | NGE/gamma conditioning for F1; EOD structures |
| NSE-vendor options minute packs (GDFL/TradingQnA quote / Stolo 4y full F&O) | 1-min option chains, Jun 2016+ | ~₹4.2k+GST per index-year ⇒ **₹15–35k** for a focused 3–4y, both indices | F5 intraday expiry-day/premium families |
| Binance forceOrder stream (record forward) | liquidations, live only | ₹0 + recorder uptime | F4 later |
| Upstox/Dhan historical APIs | 1-min only ~1–6 months back | ₹0 | not useful for history; fine for live |

## 5. Ranked family shortlist (pre-registration sketches)

**F2 — Crypto hour-of-day/day-of-week seasonality** *(first: zero new infra, fastest verdict)*
Hourly folds on majors (2019→2026), honest Delta costs. Pre-registered grid: window classes
{fixed UTC windows incl. 21–23 UTC, Asia-open Monday, weekend}, long/short/trend-within-window,
vol-targeted. Uses existing panel machinery + floor-seeded holdout seal for new hourly series.

**F1 — NIFTY intraday momentum (noise-area) with gamma conditioning** *(the headline family)*
Kite index minute ingest (60-day chunks to ~2015) → session-aware intraday fold (the tracked SF4
backtester session gate becomes a prerequisite, TEST-1) → noise-area bands (14/90-day), entries at
HH:00/HH:30 breaks, EOD flat, NIFTY-futures cost overlay (~6bps RT); conditioning variants: none /
first-half-hour sign (Gao) / **NGE sign from our own EOD chains** (Baltussen). This is the
strongest-evidenced structural family that fits our stack natively (SCHED-1 gates, M4.5 kite-paper
runway for its paper phase).

**F3 — BTC→alt lead-lag at 1s–1m** *(bigger build, biggest structural moat at our scale)*
Binance aggTrades → 1s bars for BTC + top-liquidity alts; signal = BTC trailing k-sec return
beyond noise threshold; trade lagging alt perp, maker-biased exits; Delta live target. Documented
mean lead ~57s. Costs-first design; expect most of the grid to die — the survivors matter.

**F5 — NIFTY (Tue) / SENSEX (Thu) expiry-day option structures** *(on ₹ approval)*
Minute-premium data pack (₹15–35k) → time-based entries (morning straddle w/ SL-per-leg, afternoon
theta 13:00–15:15, defined-risk variants), regime-weighted to post-Nov-2024 rules, Budget-2026
costs; Greeks gate (#161) already enforces book caps. Counterparty flow is documented and huge;
the naive forms are dead — the gate decides if the disciplined forms clear.

**F4 — Liquidation-cascade reversion** *(start the free recorder now, research later)*

**F6 — Delta-hedged intraday premium / gamma-conditioned vol** *(after F5's data + a survivor)*

## 6. Order of operations + [You] asks
Build order: **F2 → F1 → F3** (all free) with **F4 recorder** started alongside; **F5** the moment
data ₹ is approved. Each family: own pre-registered grid, own trials ledger, one holdout read max;
the ~2.1y options holdout stays virgin until an in-sample survivor earns its read.

**Asks ([You]):**
1. **₹15–35k approval** for the NSE options minute pack (F5) — or explicit defer.
2. **Kite daily token** on a market day so the historical minute ingest (F1) can run (base Kite
   Connect now includes historical — no add-on fee).
3. Nothing else blocks F2/F3/F4 — free data, existing venues.

## 7. Discipline notes (unchanged, sharpened)
Every number above is someone's marketing until it survives *our* fold: pre-registration, honest
Budget-2026/GST costs, DSR against the family's trial count, CPCV embargoes, one sealed holdout
read. The QuantConnect ORB replication (great 2016–2020, collapse elsewhere, 17% win rate) is the
cautionary template — and exactly the failure mode our gate already catches (it killed subtler
mirages in M3.0). Regime honesty beats optimism; the optimism here is that **the documented edge
classes are ones we have never tested, on data we mostly already have.**

### Primary sources
- Gao/Han/Li/Zhou, *Market Intraday Momentum*, JFE 2018 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2440866
- Baltussen/Da/Lammers/Martens, *Hedging Demand and Market Intraday Momentum*, JFE 2021 — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3760365
- Zarattini/Aziz/Barbon, *Beat the Market* (SPY intraday momentum) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172
- Zarattini/Aziz, *Can Day Trading Really Be Profitable?* (ORB) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4416622
- Zarattini/Barbon/Aziz, *A Profitable Day Trading Strategy* (Stocks in Play) — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4729284
- Quantitativo ES/NQ replication — https://www.quantitativo.com/p/intraday-momentum-for-es-and-nq
- QuantConnect ORB replication + community regime findings — https://www.quantconnect.com/research/18444/opening-range-breakout-for-stocks-in-play/
- Beckmeyer/Branger/Gayda, *Retail Traders Love 0DTE Options… But Should They?* — https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4404704
- SEBI FY24-25 individual-trader loss study (91%, ₹1.06L cr) — https://www.moneylife.in/article/91-percentage-of-retail-traders-lost-money-in-derivatives-losses-in-fo-surged-41-percentage-to-rs105-lakh-crore-in-fy2425-sebi-study/77613.html
- SEBI Sep-2024 study (93%, ₹1.8L cr FY22–24) — https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html
- SEBI index-derivatives reforms — https://zerodha.com/z-connect/business-updates/sebis-new-rules-for-index-derivatives-heres-whats-changing ; intraday limits framework — https://www.sebi.gov.in/legal/circulars/sep-2025/framework-for-intraday-position-limits-monitoring-for-equity-index-derivatives_96376.html
- NSE Tuesday / BSE Thursday expiry shift (Sep 2025) — https://www.venturasecurities.com/blog/changes-in-expiry-nse-and-bse/
- QuantPedia BTC intraday/overnight anomalies — https://quantpedia.com/are-there-seasonal-intraday-or-overnight-anomalies-in-bitcoin/
- Concretum, *Seasonality in Bitcoin Intraday Trend Trading* — https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- BTC→alt tick-level lead-lag (16–118s) — https://businessperspectives.org/images/pdf/applications/publishing/templates/article/assets/17735/IMFI_2023_01_Anderson.pdf ; strategy validation — https://link.springer.com/article/10.1007/s10690-026-09589-z
- Robot Wealth strategy index (retail edge classes) — https://robotwealth.com/index-of-strategies/
- Kite historical now free / expired-options limits — https://kite.trade/forum/discussion/14806/historical-data-is-now-free-with-base-kite-connect-subscription ; https://support.zerodha.com/category/trading-and-markets/charts-and-orders/charts/articles/historical-data-for-expired-f-o-contract
- BANKNIFTY 1-min options data quote (₹4,200+GST/yr) — https://tradingqna.com/t/received-quote-for-historical-bank-nifty-1-min-data/135259
- Budget 2026 STT hikes — https://www.bajajfinserv.in/securities-transaction-tax ; https://cleartax.in/s/securities-transaction-tax-stt
- Delta India fees — https://www.delta.exchange/fees
