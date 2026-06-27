#!/usr/bin/env bash
# pod/seed/seed_cockpit_demo.sh — ADDITIVE cockpit demo enrichment for the Vault pod.
#
# Run AFTER seed.sh (the B0.4 chain). It populates the tables the cockpit's views
# render so the dashboard demos itself: varied strategies, backtests INCL. rejected
# (high PBO / negative DSR), discovery runs, a research ledger, a running deployment
# with a real equity curve, risk events, a pending approval, and a pending command —
# and refreshes the worker/research heartbeats to "now" so the health strip is green.
#
#   bash pod/seed/seed_cockpit_demo.sh        # run ONCE on a pod already seeded by seed.sh
#
# Demo data ONLY — never real money/keys (broker_credentials is RLS, seeded via the
# token-relay path, not here). Money/qty/price are TEXT string-Decimals (B5). Unique
# keys are namespaced `demo2-…`; a second run halts on the first collision (expected).
set -euo pipefail
jid() { python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"; }
rid() { lemma --json query run "$1" | python3 -c "import sys,json;r=json.load(sys.stdin).get('items',[]);print(r[0]['id'] if r else '')"; }
ago() { python3 -c "import sys;from datetime import datetime,timezone,timedelta;print((datetime.now(timezone.utc)-timedelta(seconds=int(sys.argv[1]))).strftime('%Y-%m-%dT%H:%M:%SZ'))" "$1"; }

echo "== refreshing worker/research heartbeats to now (best-effort) =="
WID=$(rid "SELECT id FROM worker_status WHERE worker_id='worker-1'")
if [ -n "$WID" ]; then
  lemma records update worker_status "$WID" -d "{\"last_seen\":\"$(ago 8)\",\"armed\":true,\"detail\":{\"adapters\":[\"delta\",\"binance\"],\"reconciled_at\":\"$(ago 45)\"}}" >/dev/null 2>&1 || echo "  (worker heartbeat refresh skipped)"
fi
RID=$(rid "SELECT id FROM research_status WHERE research_id='research-1'")
if [ -n "$RID" ]; then
  lemma records update research_status "$RID" -d "{\"last_seen\":\"$(ago 20)\",\"status\":\"running\",\"current_task\":\"CPCV on equity/orb cell\"}" >/dev/null 2>&1 || echo "  (research heartbeat refresh skipped)"
fi

echo "== strategies (varied status / family / market) =="
S_ORB=$(lemma --json records create strategies -d '{"name":"NIFTY Opening-Range Breakout","family":"opening_range_breakout","market":"equity","status":"approved","origin":"constrained","config":{"or_minutes":15,"stop_atr":1.5},"rationale":"Breakout of the first 15-min range on NIFTY constituents."}' | jid)
S_VWAP=$(lemma --json records create strategies -d '{"name":"BANKNIFTY VWAP Reversion","family":"vwap_reversion","market":"equity","status":"paper","origin":"constrained","config":{"band_bps":35},"rationale":"Fade stretched intraday moves back to VWAP."}' | jid)
S_MOM=$(lemma --json records create strategies -d '{"name":"ETH Momentum ROC","family":"momentum_roc","market":"crypto","status":"backtested","origin":"constrained","config":{"roc_period":24},"rationale":"Rate-of-change momentum on ETH-perp."}' | jid)
S_REJ=$(lemma --json records create strategies -d '{"name":"BTC RSI-Bollinger (overfit)","family":"rsi_bollinger","market":"crypto","status":"rejected","origin":"constrained","config":{"rsi_period":2},"rationale":"RSI(2) mean-revert — strong in-sample, failed CPCV/PBO."}' | jid)
lemma records create strategies -d '{"name":"Legacy MA Scalper","family":"ma_crossover","market":"crypto","status":"retired","origin":"manual","config":{},"rationale":"Retired — superseded by the discovery loop."}' >/dev/null

echo "== backtests (incl. REJECTED: high PBO / negative DSR) =="
bt() { lemma records create backtests -d "$1" >/dev/null; }
bt "{\"strategy_id\":\"$S_ORB\",\"status\":\"complete\",\"market\":\"equity\",\"family\":\"opening_range_breakout\",\"window\":\"2021-01..2024-12\",\"sharpe\":2.05,\"max_dd\":0.14,\"cpcv_pbo\":0.11,\"deflated_sharpe\":1.42,\"net_pnl\":\"54210.00\",\"trial_count\":9,\"holdout_used\":true,\"holdout_metrics\":{\"sharpe\":1.88},\"dataset_version\":\"nse-1d-v2\",\"engine_version\":\"alpha-core-0.0.0\",\"cost_model_version\":\"kite-v1\",\"seed\":\"7\"}"
bt "{\"strategy_id\":\"$S_VWAP\",\"status\":\"complete\",\"market\":\"equity\",\"family\":\"vwap_reversion\",\"window\":\"2021-01..2024-12\",\"sharpe\":1.66,\"max_dd\":0.12,\"cpcv_pbo\":0.22,\"deflated_sharpe\":1.03,\"net_pnl\":\"31180.00\",\"trial_count\":14,\"holdout_used\":true,\"holdout_metrics\":{\"sharpe\":1.41},\"dataset_version\":\"nse-1d-v2\",\"engine_version\":\"alpha-core-0.0.0\",\"cost_model_version\":\"kite-v1\",\"seed\":\"11\"}"
bt "{\"strategy_id\":\"$S_MOM\",\"status\":\"complete\",\"market\":\"crypto\",\"family\":\"momentum_roc\",\"window\":\"2023-01..2024-12\",\"sharpe\":1.18,\"max_dd\":0.27,\"cpcv_pbo\":0.39,\"deflated_sharpe\":0.41,\"net_pnl\":\"9120.50\",\"trial_count\":21,\"holdout_used\":false,\"dataset_version\":\"eth-5m-v1\",\"engine_version\":\"alpha-core-0.0.0\",\"cost_model_version\":\"delta-v1\",\"seed\":\"19\"}"
bt "{\"strategy_id\":\"$S_REJ\",\"status\":\"complete\",\"market\":\"crypto\",\"family\":\"rsi_bollinger\",\"window\":\"2023-01..2024-12\",\"sharpe\":2.71,\"max_dd\":0.33,\"cpcv_pbo\":0.86,\"deflated_sharpe\":-0.22,\"net_pnl\":\"-4210.00\",\"trial_count\":48,\"holdout_used\":false,\"dataset_version\":\"btc-5m-v1\",\"engine_version\":\"alpha-core-0.0.0\",\"cost_model_version\":\"delta-v1\",\"seed\":\"23\"}"
bt "{\"strategy_id\":\"$S_MOM\",\"status\":\"failed\",\"market\":\"crypto\",\"family\":\"momentum_roc\",\"window\":\"2022-01..2022-12\",\"net_pnl\":\"0\",\"trial_count\":0,\"holdout_used\":false,\"engine_version\":\"alpha-core-0.0.0\"}"

echo "== discovery runs =="
dr() { lemma records create discovery_runs -d "$1" >/dev/null; }
dr "{\"status\":\"complete\",\"market\":\"equity\",\"family\":\"opening_range_breakout\",\"window\":\"2021-01..2024-12\",\"trial_count\":9,\"survivors\":1,\"detail\":{\"promoted\":[\"NIFTY ORB\"]}}"
dr "{\"status\":\"complete\",\"market\":\"equity\",\"family\":\"vwap_reversion\",\"window\":\"2021-01..2024-12\",\"trial_count\":14,\"survivors\":1}"
dr "{\"status\":\"complete\",\"market\":\"crypto\",\"family\":\"rsi_bollinger\",\"window\":\"2023-01..2024-12\",\"trial_count\":48,\"survivors\":0,\"detail\":{\"note\":\"all candidates failed PBO\"}}"
dr "{\"status\":\"running\",\"market\":\"crypto\",\"family\":\"momentum_roc\",\"window\":\"2023-01..2024-12\",\"trial_count\":6,\"survivors\":0}"
dr "{\"status\":\"failed\",\"market\":\"equity\",\"family\":\"ma_crossover\",\"window\":\"2020-01..2024-12\",\"trial_count\":2,\"survivors\":0,\"detail\":{\"error\":\"data gap in window\"}}"

echo "== research ledger cells =="
lc() { lemma records create research_ledger -d "$1" >/dev/null; }
lc '{"cell_key":"equity|opening_range_breakout|2021-01..2024-12","market":"equity","family":"opening_range_breakout","window":"2021-01..2024-12","cumulative_trials":9}'
lc '{"cell_key":"equity|vwap_reversion|2021-01..2024-12","market":"equity","family":"vwap_reversion","window":"2021-01..2024-12","cumulative_trials":14}'
lc '{"cell_key":"crypto|rsi_bollinger|2023-01..2024-12","market":"crypto","family":"rsi_bollinger","window":"2023-01..2024-12","cumulative_trials":48}'

echo "== running deployment + equity curve (30 hourly snapshots) =="
DID2=$(lemma --json records create deployments -d "{\"strategy_id\":\"$S_ORB\",\"venue\":\"KITE\",\"mode\":\"paper\",\"status\":\"running\",\"capital\":\"500000.00\",\"risk_limits\":{\"max_position\":\"50\",\"daily_loss_halt\":\"10000.00\"},\"worker_id\":\"worker-1\"}" | jid)
python3 - "$DID2" > /tmp/alpha_curve.jsonl <<'PY'
import sys, json, random
from datetime import datetime, timezone, timedelta
did = sys.argv[1]; random.seed(7)
now = datetime.now(timezone.utc); eq = 500000.0; n = 30
for i in range(n):
    ts = (now - timedelta(hours=(n - 1 - i))).strftime('%Y-%m-%dT%H:%M:%SZ')
    eq += random.uniform(-900, 1500)
    print(json.dumps({"deployment_id": did, "snapshot_key": f"demo2-{did}-{i:03d}", "ts": ts,
                      "realized_pnl": f"{eq-500000:.2f}", "unrealized_pnl": "0.00",
                      "equity": f"{eq:.2f}", "fees_paid": f"{i*1.7:.2f}", "funding_paid": "0.00"}))
PY
while IFS= read -r line; do lemma records create pnl_snapshots -d "$line" >/dev/null; done < /tmp/alpha_curve.jsonl
rm -f /tmp/alpha_curve.jsonl

echo "== risk events (info / warning / critical) =="
re() { lemma records create risk_events -d "$1" >/dev/null; }
re "{\"deployment_id\":\"$DID2\",\"kind\":\"reconcile_mismatch\",\"severity\":\"info\",\"ts\":\"$(ago 5400)\",\"detail\":{\"resolved\":true,\"note\":\"explained drift adopted\"}}"
re "{\"deployment_id\":\"$DID2\",\"kind\":\"funding_bleed\",\"severity\":\"warning\",\"ts\":\"$(ago 3600)\",\"detail\":{\"funding_paid\":\"-42.10\"}}"
re "{\"deployment_id\":\"$DID2\",\"kind\":\"drift\",\"severity\":\"warning\",\"ts\":\"$(ago 1800)\",\"detail\":{\"qty_delta\":\"0.4\"}}"
re "{\"deployment_id\":\"$DID2\",\"kind\":\"limit_breach\",\"severity\":\"critical\",\"ts\":\"$(ago 600)\",\"detail\":{\"limit\":\"daily_loss\",\"value\":\"-9120\"}}"

echo "== pending approval (deployment) + pending command =="
lemma records create deployments -d "{\"strategy_id\":\"$S_VWAP\",\"venue\":\"KITE\",\"mode\":\"paper\",\"status\":\"pending_approval\",\"capital\":\"250000.00\",\"risk_limits\":{\"max_position\":\"25\",\"daily_loss_halt\":\"5000.00\"}}" >/dev/null
lemma records create commands -d "{\"deployment_id\":\"$DID2\",\"kind\":\"arm_kill\",\"status\":\"pending\",\"worker_id\":\"worker-1\",\"payload\":{\"reason\":\"manual arm from cockpit\"}}" >/dev/null

echo
echo "cockpit demo seed complete."
lemma query run "SELECT status, count(*) n FROM backtests GROUP BY status ORDER BY n DESC"
