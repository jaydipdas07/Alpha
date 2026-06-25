"""Track A (NautilusTrader shell) — the engine bake-off's OTHER track.

Runs the SAME portable B0.7 contract (`MaCrossover`) through **NautilusTrader**:
the contract drives a Nautilus `Strategy`, and Vega's `CostModel` (the Track-B
cost kernel) wires into a custom Nautilus `FeeModel`. This is the co-equal of the
Track-B backtest (`alpha-core/tests/test_bakeoff_b0_9f.py`); B0.10 compares the two
on parity + integration friction to pick the engine at 0.GATE.

NOT a CI test (NautilusTrader is a heavy, Rust-backed dependency kept out of the
alpha-core kernel + CI — see README). Run in an isolated venv:

    cd track-a && uv venv && UV_HTTP_TIMEOUT=600 \
        uv pip install -e ../alpha-core nautilus_trader
    .venv/bin/python track_a_backtest.py
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import FeeModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Venue
from nautilus_trader.model.objects import Currency, Money, Price, Quantity
from nautilus_trader.test_kit.providers import TestInstrumentProvider
from nautilus_trader.trading.strategy import Strategy

from alpha_core.core.enums import AssetClass, Side
from alpha_core.core.enums import Venue as AlphaVenue
from alpha_core.core.models import Bar as AlphaBar
from alpha_core.execution.costs import CostModel, InstrumentMeta
from alpha_core.helpers.config import load_yaml
from alpha_core.strategy.examples.ma_crossover import MaCrossover, MaCrossoverConfig

INSTRUMENT = TestInstrumentProvider.btcusdt_binance()
BAR_TYPE = BarType.from_str(f"{INSTRUMENT.id}-1-MINUTE-LAST-EXTERNAL")
USDT = Currency.from_str("USDT")
SYMBOL = "BTCUSDT"
START_CASH = Decimal("1000000")
_INTERVAL = timedelta(minutes=1)
_META = InstrumentMeta(asset_class=AssetClass.CRYPTO)
# Same synthetic rise-then-fall series as the Track-B proof: MaCrossover(2,4) BUYs then SELLs.
_CLOSES = [
    "30000",
    "30000",
    "30000",
    "30000",
    "30200",
    "30500",
    "30800",
    "31000",
    "30500",
    "30000",
    "29500",
    "29000",
]


class VegaCostFeeModel(FeeModel):
    """Wires the Track-B cost kernel (`alpha_core.execution.costs.CostModel`) into
    Nautilus's per-fill commission hook — the same cost logic feeds both tracks."""

    def __init__(self) -> None:
        super().__init__()
        self._cost = CostModel(load_yaml("costs.yaml"))
        self.total_commission = Decimal(0)

    def get_commission(self, order, fill_qty, fill_px, instrument):  # type: ignore[no-untyped-def]
        side = Side.BUY if order.side == OrderSide.BUY else Side.SELL
        breakdown = self._cost.estimate(
            side=side,
            quantity=Decimal(str(fill_qty)),
            ltp=Decimal(str(fill_px)),
            instrument=_META,
        )
        # Map Vega's all-in cost (slippage + spread + fees + taxes) to the Nautilus
        # commission so Track-A's total P&L impact matches Track-B's. (Mechanism nuance
        # for B0.10: Track B splits cost into fill-price + fees; here it's all commission.)
        commission = round(breakdown.total, 8)
        self.total_commission += commission
        return Money(commission, USDT)


class ContractStrategyConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast: int = 2
    slow: int = 4
    trade_qty: str = "0.010000"


class ContractStrategy(Strategy):
    """Drives the portable `MaCrossover` contract from Nautilus bar events — the
    integration seam: Nautilus `Bar` -> `alpha_core.Bar` -> contract -> Nautilus order."""

    def __init__(self, config: ContractStrategyConfig) -> None:
        super().__init__(config)
        self._ma = MaCrossover(
            MaCrossoverConfig(
                fast_period=config.fast, slow_period=config.slow, quantity=Decimal(config.trade_qty)
            )
        )

    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        abar = AlphaBar(
            symbol=SYMBOL,
            venue=AlphaVenue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=datetime.fromtimestamp(bar.ts_event / 1e9, tz=UTC),
            interval=_INTERVAL,
            open=Decimal(str(bar.open)),
            high=Decimal(str(bar.high)),
            low=Decimal(str(bar.low)),
            close=Decimal(str(bar.close)),
            volume=Decimal(str(bar.volume)),
        )
        for sig in self._ma.on_bar(abar):
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY if sig.side is Side.BUY else OrderSide.SELL,
                quantity=INSTRUMENT.make_qty(sig.quantity),
            )
            self.submit_order(order)


def main() -> None:
    engine = BacktestEngine(config=BacktestEngineConfig(logging=LoggingConfig(bypass_logging=True)))
    venue = Venue("BINANCE")
    fee_model = VegaCostFeeModel()
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(START_CASH, USDT)],
        fee_model=fee_model,
    )
    engine.add_instrument(INSTRUMENT)
    engine.add_data(
        [
            Bar(
                BAR_TYPE,
                Price(float(c), INSTRUMENT.price_precision),
                Price(float(c), INSTRUMENT.price_precision),
                Price(float(c), INSTRUMENT.price_precision),
                Price(float(c), INSTRUMENT.price_precision),
                Quantity(1.0, INSTRUMENT.size_precision),
                i * 60_000_000_000,
                i * 60_000_000_000,
            )
            for i, c in enumerate(_CLOSES)
        ]
    )
    engine.add_strategy(
        ContractStrategy(ContractStrategyConfig(instrument_id=INSTRUMENT.id, bar_type=BAR_TYPE))
    )
    engine.run()

    end_balance = Decimal(str(engine.portfolio.account(venue).balance_total(USDT)).split()[0])
    filled = sum(1 for o in engine.cache.orders() if o.filled_qty > 0)
    print("=== Track A (NautilusTrader) — B0.7 MaCrossover backtest ===")
    print(f"fills:           {filled}")
    print(f"total cost:      {fee_model.total_commission} USDT (Vega CostModel via FeeModel)")
    print(f"start cash:      {START_CASH} USDT")
    print(f"end balance:     {end_balance} USDT")
    print(f"net P&L:         {end_balance - START_CASH} USDT")
    engine.dispose()


if __name__ == "__main__":
    main()
