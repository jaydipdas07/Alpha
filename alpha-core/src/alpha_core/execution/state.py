"""State store — persisted orders/fills/positions/P&L + append-only audit (ADR 0005).

Money/quantity are stored as exact ``Decimal`` text (never float) and timestamps
as ISO-8601 UTC text, portable across SQLite (dev/paper) and Postgres (live).
``fills`` and ``audit_log`` are **append-only**, enforced both in this layer
(insert-only repositories) and at the DB layer (triggers). Every logical state
change runs in one transaction (``transaction()``), so a crash leaves no partial
state; ``orders``/``positions`` are materialized views reconstructable from the
append-only ``fills`` via ``rebuild_positions``.

Note: ``create_schema`` mirrors what alembic revision 0001 creates; for the paper
milestone (SQLite) it is the runtime schema authority. The append-only triggers
are created here for SQLite and would be emitted by the migration for Postgres.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Text, create_engine, event, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Fill, Order, Position


class DecimalText(TypeDecorator[Decimal]):
    """Store ``Decimal`` as its canonical string — exact on SQLite and Postgres."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: object) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: str | None, dialect: object) -> Decimal | None:
        return None if value is None else Decimal(value)


class TZDateTime(TypeDecorator[datetime]):
    """Store a tz-aware datetime as ISO-8601 UTC text; parse back tz-aware."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> str | None:
        if value is None:
            return None
        return value.astimezone(UTC).isoformat()

    def process_result_value(self, value: str | None, dialect: object) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"
    client_order_id: Mapped[str] = mapped_column(Text, primary_key=True)
    venue_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    symbol: Mapped[str] = mapped_column(Text)
    venue: Mapped[str] = mapped_column(Text)
    asset_class: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    order_type: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(DecimalText)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    state: Mapped[str] = mapped_column(Text)
    filled_quantity: Mapped[Decimal] = mapped_column(DecimalText)
    average_fill_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    strategy_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZDateTime)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime)


class FillRow(Base):
    __tablename__ = "fills"
    fill_id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(Text)
    venue_order_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue_fill_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)
    symbol: Mapped[str] = mapped_column(Text)
    venue: Mapped[str] = mapped_column(Text)
    asset_class: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(DecimalText)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    fees: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime)


class PositionRow(Base):
    __tablename__ = "positions"
    venue: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    asset_class: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(DecimalText)
    average_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    last_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime)


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime)
    recorded_at: Mapped[datetime] = mapped_column(TZDateTime)
    event_type: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    hash: Mapped[str] = mapped_column(Text)


class PnlLedgerRow(Base):
    __tablename__ = "pnl_ledger"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZDateTime)
    venue: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    fill_id: Mapped[str] = mapped_column(Text)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    currency: Mapped[str] = mapped_column(Text)


class DailyPnlRow(Base):
    __tablename__ = "daily_pnl"
    trading_date: Mapped[str] = mapped_column(Text, primary_key=True)
    day_start_equity: Mapped[Decimal] = mapped_column(DecimalText)
    realized_pnl: Mapped[Decimal] = mapped_column(DecimalText)
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    halted: Mapped[bool] = mapped_column()
    halt_trigger: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime)


@dataclass(frozen=True, slots=True)
class DailyPnl:
    """Detached-safe view of a ``daily_pnl`` row (survives session close)."""

    trading_date: str
    day_start_equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal | None
    halted: bool
    halt_trigger: str | None


# pnl_ledger is append-only (realized-P&L events); daily_pnl is a mutable summary.
_APPEND_ONLY_TABLES = ("audit_log", "fills", "pnl_ledger")


def run_migrations(url: str) -> None:
    """Run ``alembic upgrade head`` against ``url`` (the production schema path)."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[3]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def append_only_trigger_sql(dialect: str) -> list[str]:
    """DDL that blocks UPDATE/DELETE on the append-only tables (ADR 0005).

    Shared by ``StateStore.create_schema`` (dev/tests) and alembic revision 0001
    (production). SQLite uses ``RAISE(ABORT)`` triggers; Postgres uses
    ``BEFORE UPDATE OR DELETE`` triggers calling a ``RAISE EXCEPTION`` function.
    """
    statements: list[str] = []
    if dialect == "postgresql":
        statements.append(
            "CREATE OR REPLACE FUNCTION vega_append_only() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'table is append-only'; END; $$ LANGUAGE plpgsql;"
        )
    for table in _APPEND_ONLY_TABLES:
        if dialect == "postgresql":
            statements.append(
                f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION vega_append_only();"
            )
        else:  # sqlite (and compatible)
            for op in ("UPDATE", "DELETE"):
                statements.append(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{op.lower()} BEFORE {op} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END;"
                )
    return statements


class StateStore:
    """SQLAlchemy-backed persistence with append-only audit/fills."""

    def __init__(self, url: str = "sqlite:///:memory:") -> None:
        # In-memory SQLite needs a single shared connection to keep the schema/data.
        if ":memory:" in url:
            self._engine = create_engine(
                url, poolclass=StaticPool, connect_args={"check_same_thread": False}
            )
        else:
            self._engine = create_engine(url)

        # SQLite doesn't enforce foreign keys unless asked, per-connection. The
        # PRAGMA is SQLite-only syntax, so register it for SQLite alone — Postgres
        # enforces FKs natively and chokes on PRAGMA otherwise.
        if self._engine.dialect.name == "sqlite":  # pragma: no branch - Postgres in deploy only

            @event.listens_for(self._engine, "connect")
            def _fk_on(dbapi_conn: Any, _rec: Any) -> None:  # pragma: no cover - trivial
                dbapi_conn.execute("PRAGMA foreign_keys=ON")

    def create_schema(self) -> None:
        """Create tables and the append-only triggers (dev/tests; idempotent).

        Mirrors alembic revision 0001 — the production path is ``alembic upgrade``
        (see ``migrations/``). Both share ``append_only_trigger_sql``.
        """
        Base.metadata.create_all(self._engine)
        with self._engine.begin() as conn:
            for sql in append_only_trigger_sql(self._engine.dialect.name):
                conn.execute(text(sql))

    def dispose(self) -> None:
        """Release the engine's connection pool (free a short-lived store)."""
        self._engine.dispose()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """One atomic unit of work — commits, or rolls back fully on error."""
        session = Session(self._engine)
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # --- writes ----------------------------------------------------------------

    def save_order(self, session: Session, order: Order) -> None:
        """Insert or update the order row (the only mutable table besides positions)."""
        session.merge(_order_to_row(order))

    def append_fill(self, session: Session, fill: Fill) -> None:
        """Insert a fill (append-only; duplicate ``venue_fill_id`` raises)."""
        session.add(_fill_to_row(fill))

    def upsert_position(self, session: Session, position: Position) -> None:
        session.merge(_position_to_row(position))

    def append_audit(
        self,
        session: Session,
        *,
        event_type: str,
        payload: dict[str, Any],
        entity_type: str | None = None,
        entity_id: str | None = None,
        strategy_id: str | None = None,
        ts: datetime | None = None,
    ) -> str:
        """Append an audit row, chaining its hash to the previous row."""
        prev_hash = session.execute(
            select(AuditRow.hash).order_by(AuditRow.id.desc()).limit(1)
        ).scalar_one_or_none()
        now = ts or datetime.now(UTC)
        body = json.dumps(
            {
                "event_type": event_type,
                "entity_id": entity_id,
                "payload": payload,
                "ts": now.isoformat(),
            },
            sort_keys=True,
        )
        digest = hashlib.sha256(f"{prev_hash or ''}{body}".encode()).hexdigest()
        session.add(
            AuditRow(
                ts=now,
                recorded_at=datetime.now(UTC),
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                strategy_id=strategy_id,
                payload=json.dumps(payload, sort_keys=True),
                prev_hash=prev_hash,
                hash=digest,
            )
        )
        return digest

    def append_pnl(
        self,
        session: Session,
        *,
        ts: datetime,
        venue: Venue,
        symbol: str,
        fill_id: str,
        realized: Decimal,
        currency: str = "INR",
    ) -> None:
        """Append a realized-P&L event (append-only)."""
        session.add(
            PnlLedgerRow(
                ts=ts,
                venue=venue.value,
                symbol=symbol,
                fill_id=fill_id,
                realized_pnl=realized,
                currency=currency,
            )
        )

    def upsert_daily_pnl(
        self,
        session: Session,
        *,
        trading_date: str,
        day_start_equity: Decimal,
        realized: Decimal,
        unrealized: Decimal | None,
        halted: bool,
        halt_trigger: str | None,
        updated_at: datetime,
    ) -> None:
        """Insert/update the per-day P&L summary (the kill-switch baseline)."""
        session.merge(
            DailyPnlRow(
                trading_date=trading_date,
                day_start_equity=day_start_equity,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                halted=halted,
                halt_trigger=halt_trigger,
                updated_at=updated_at,
            )
        )

    def load_daily_pnl(self, session: Session, trading_date: str) -> DailyPnl | None:
        row = session.get(DailyPnlRow, trading_date)
        if row is None:
            return None
        return DailyPnl(
            trading_date=row.trading_date,
            day_start_equity=row.day_start_equity,
            realized_pnl=row.realized_pnl,
            unrealized_pnl=row.unrealized_pnl,
            halted=row.halted,
            halt_trigger=row.halt_trigger,
        )

    # --- reads -----------------------------------------------------------------

    def load_orders(self, session: Session) -> list[Order]:
        return [_row_to_order(r) for r in session.execute(select(OrderRow)).scalars()]

    def load_positions(self, session: Session) -> list[Position]:
        return [_row_to_position(r) for r in session.execute(select(PositionRow)).scalars()]

    def load_fills(self, session: Session) -> list[Fill]:
        return [_row_to_fill(r) for r in session.execute(select(FillRow)).scalars()]

    def rebuild_positions(self, session: Session) -> dict[tuple[Venue, str], Decimal]:
        """Reconstruct net signed quantity per (venue, symbol) from append-only fills."""
        out: dict[tuple[Venue, str], Decimal] = {}
        for fill in self.load_fills(session):
            signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
            key = (fill.venue, fill.symbol)
            out[key] = out.get(key, Decimal(0)) + signed
        return out

    def verify_audit_chain(self, session: Session) -> bool:
        """Re-walk the audit hash chain; False if any link is broken."""
        prev = ""
        for row in session.execute(select(AuditRow).order_by(AuditRow.id)).scalars():
            body = json.dumps(
                {
                    "event_type": row.event_type,
                    "entity_id": row.entity_id,
                    "payload": json.loads(row.payload),
                    "ts": row.ts.isoformat(),
                },
                sort_keys=True,
            )
            if hashlib.sha256(f"{prev}{body}".encode()).hexdigest() != row.hash:
                return False
            prev = row.hash
        return True


# --- row <-> model mapping -----------------------------------------------------


def _order_to_row(o: Order) -> OrderRow:
    return OrderRow(
        client_order_id=o.client_order_id,
        venue_order_id=o.venue_order_id,
        symbol=o.symbol,
        venue=o.venue.value,
        asset_class=o.asset_class.value,
        side=o.side.value,
        order_type=o.order_type.value,
        quantity=o.quantity,
        limit_price=o.limit_price,
        stop_price=o.stop_price,
        state=o.state.value,
        filled_quantity=o.filled_quantity,
        average_fill_price=o.average_fill_price,
        strategy_id=o.strategy_id,
        created_at=o.created_at,
        updated_at=o.updated_at,
    )


def _row_to_order(r: OrderRow) -> Order:
    return Order(
        client_order_id=r.client_order_id,
        venue_order_id=r.venue_order_id,
        symbol=r.symbol,
        venue=Venue(r.venue),
        asset_class=AssetClass(r.asset_class),
        side=Side(r.side),
        order_type=OrderType(r.order_type),
        quantity=r.quantity,
        limit_price=r.limit_price,
        stop_price=r.stop_price,
        state=OrderState(r.state),
        filled_quantity=r.filled_quantity,
        average_fill_price=r.average_fill_price,
        strategy_id=r.strategy_id,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _fill_to_row(f: Fill) -> FillRow:
    return FillRow(
        fill_id=f.fill_id,
        client_order_id=f.client_order_id,
        venue_order_id=f.venue_order_id,
        venue_fill_id=f.venue_fill_id,
        symbol=f.symbol,
        venue=f.venue.value,
        asset_class=f.asset_class.value,
        side=f.side.value,
        quantity=f.quantity,
        price=f.price,
        fees=f.fees,
        ts=f.ts,
        recorded_at=datetime.now(UTC),
    )


def _row_to_fill(r: FillRow) -> Fill:
    return Fill(
        fill_id=r.fill_id,
        client_order_id=r.client_order_id,
        venue_order_id=r.venue_order_id,
        venue_fill_id=r.venue_fill_id,
        symbol=r.symbol,
        venue=Venue(r.venue),
        asset_class=AssetClass(r.asset_class),
        side=Side(r.side),
        quantity=r.quantity,
        price=r.price,
        fees=r.fees,
        ts=r.ts,
    )


def _position_to_row(p: Position) -> PositionRow:
    return PositionRow(
        venue=p.venue.value,
        symbol=p.symbol,
        asset_class=p.asset_class.value,
        quantity=p.quantity,
        average_price=p.average_price,
        realized_pnl=p.realized_pnl,
        unrealized_pnl=p.unrealized_pnl,
        last_price=p.last_price,
        updated_at=p.updated_at,
    )


def _row_to_position(r: PositionRow) -> Position:
    return Position(
        venue=Venue(r.venue),
        symbol=r.symbol,
        asset_class=AssetClass(r.asset_class),
        quantity=r.quantity,
        average_price=r.average_price,
        realized_pnl=r.realized_pnl,
        unrealized_pnl=r.unrealized_pnl,
        last_price=r.last_price,
        updated_at=r.updated_at,
    )
