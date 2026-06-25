#!/usr/bin/env bash
# pod/seed/seed.sh — coherent demo seed for the Alpha Vault pod (B0.4).
#
# Records do NOT round-trip through `lemma pods import`, so seed data lands here.
# Run ONCE against the target pod after import:   bash pod/seed/seed.sh
# Money / qty / price are TEXT string-Decimals (B5). The idempotency keys
# (client_order_id, dedup_key, cell_key, …) make a second run fail on the unique
# columns — expected; run once on a fresh pod.
set -euo pipefail
jid() { python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"; }

echo "seeding strategy -> backtest -> deployment -> paper_run -> order -> fill -> position -> pnl + heartbeats ..."

SID=$(lemma --json records create strategies -d '{"name":"BTC MA Crossover","family":"ma_crossover","market":"crypto","status":"paper","origin":"constrained","config":{"fast_period":10,"slow_period":30},"rationale":"Trend-follow BTC-perp on a fast/slow SMA cross."}' | jid)

lemma records create backtests -d "{\"strategy_id\":\"$SID\",\"status\":\"complete\",\"market\":\"crypto\",\"family\":\"ma_crossover\",\"window\":\"2023-01..2024-12\",\"sharpe\":1.42,\"max_dd\":0.18,\"cpcv_pbo\":0.12,\"deflated_sharpe\":0.91,\"net_pnl\":\"18420.75\",\"trial_count\":7,\"holdout_used\":true,\"holdout_metrics\":{\"sharpe\":1.31},\"dataset_version\":\"btc-5m-v1\",\"engine_version\":\"alpha-core-0.0.0\",\"cost_model_version\":\"delta-v1\",\"seed\":\"4294967295\"}" >/dev/null

DID=$(lemma --json records create deployments -d "{\"strategy_id\":\"$SID\",\"venue\":\"DELTA\",\"mode\":\"paper\",\"status\":\"running\",\"capital\":\"100000.00\",\"risk_limits\":{\"max_position\":\"0.5\",\"daily_loss_halt\":\"2000.00\"},\"worker_id\":\"worker-1\"}" | jid)

lemma records create paper_runs -d "{\"deployment_id\":\"$DID\",\"status\":\"running\",\"window\":\"2026-06-25..2026-07-25\",\"started_at\":\"2026-06-25T00:00:00Z\",\"metrics\":{\"tracking_error\":0.03}}" >/dev/null

OID=$(lemma --json records create orders -d "{\"deployment_id\":\"$DID\",\"client_order_id\":\"alpha-seed-0001\",\"venue_order_id\":\"DLT-99001\",\"symbol\":\"BTCUSDT\",\"venue\":\"DELTA\",\"asset_class\":\"CRYPTO\",\"side\":\"BUY\",\"order_type\":\"MARKET\",\"state\":\"FILLED\",\"quantity\":\"0.001\",\"filled_quantity\":\"0.001\",\"average_fill_price\":\"98765.43210987654321\"}" | jid)

lemma records create fills -d "{\"order_id\":\"$OID\",\"dedup_key\":\"$OID|DLT-F-1\",\"venue_order_id\":\"DLT-99001\",\"venue_fill_id\":\"DLT-F-1\",\"symbol\":\"BTCUSDT\",\"venue\":\"DELTA\",\"side\":\"BUY\",\"quantity\":\"0.001\",\"price\":\"98765.43210987654321\",\"fees\":\"0.03950617\",\"ts\":\"2026-06-25T09:30:00Z\"}" >/dev/null

lemma records create positions -d "{\"deployment_id\":\"$DID\",\"position_key\":\"$DID|DELTA|BTCUSDT\",\"symbol\":\"BTCUSDT\",\"venue\":\"DELTA\",\"quantity\":\"0.001\",\"avg_entry_price\":\"98765.43210987654321\",\"realized_pnl\":\"0\",\"unrealized_pnl\":\"12.34\"}" >/dev/null

lemma records create pnl_snapshots -d "{\"deployment_id\":\"$DID\",\"snapshot_key\":\"$DID|2026-06-25T10:00:00Z\",\"ts\":\"2026-06-25T10:00:00Z\",\"realized_pnl\":\"0\",\"unrealized_pnl\":\"12.34\",\"equity\":\"100012.34\",\"fees_paid\":\"0.03950617\",\"funding_paid\":\"-0.85\"}" >/dev/null

lemma records create worker_status -d '{"worker_id":"worker-1","last_seen":"2026-06-25T10:00:05Z","mode":"paper","armed":true,"positions_hash":"sha256:demo","build_version":"0.0.0","detail":{"adapters":["delta"]}}' >/dev/null
lemma records create research_status -d '{"research_id":"research-1","last_seen":"2026-06-25T09:55:00Z","status":"idle","current_task":"awaiting nightly discovery"}' >/dev/null
lemma records create research_ledger -d '{"cell_key":"crypto|ma_crossover|2023-01..2024-12","market":"crypto","family":"ma_crossover","window":"2023-01..2024-12","cumulative_trials":7}' >/dev/null

echo "seed complete. (broker_credentials is RLS/per-user — seed your own daily Kite token via the token-relay path, not here.)"
lemma query run "SELECT s.name, d.venue, d.mode, p.quantity, p.unrealized_pnl FROM positions p JOIN deployments d ON d.id = p.deployment_id JOIN strategies s ON s.id = d.strategy_id"
