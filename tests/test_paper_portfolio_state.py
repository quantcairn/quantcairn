import os
from datetime import datetime

import pytest

from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.paper_broker import PaperBroker
from src.broker.paper_portfolio_state import (
    PaperPortfolioState,
    PaperPortfolioStateCorruptError,
    PaperPortfolioStateError,
    PaperPortfolioStateStore,
    default_paper_portfolio_state_path,
    read_paper_portfolio_state,
    write_paper_portfolio_state,
)
from src.candidate_validation.outcome_collector import OutcomeCollector
from src.notifier.alerts import Notifier


def test_paper_broker_persists_unified_portfolio_state(tmp_path):
    state_path = tmp_path / "paper_state.json"
    broker = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
    )
    broker.connect()

    buy = broker.place_order(
        "SOFI",
        OrderSide.BUY,
        10,
        OrderType.MARKET,
        current_bid=10.0,
        current_ask=10.0,
    )

    assert buy.status == OrderStatus.FILLED
    state = read_paper_portfolio_state(state_path)
    assert state is not None
    assert state["broker"] == "PaperBroker"
    assert state["execution_mode"] == "paper"
    assert state["cash"] < 1_000.0
    assert state["positions_count"] == 1
    assert state["positions"][0]["ticker"] == "SOFI"
    assert state["positions"][0]["symbol"] == "SOFI"
    assert state["positions"][0]["average_cost"] > 0
    assert state["positions"][0]["market_price"] > 0
    assert state["positions"][0]["quantity"] == 10
    assert buy.order_id in state["processed_fill_ids"]
    assert state["last_fill_id"] == buy.order_id
    assert state["state_version"] >= 2
    assert state["fill_sequence"] == 1
    assert state["last_fill_time"]
    assert state["last_update_time"]
    assert state["writer_pid"] == os.getpid()
    assert state["writer_port"] is None
    assert state["writer_mode"] == "paper"
    assert str(state["writer_run_id"]).startswith("paper-run-")


def test_paper_broker_persists_realized_pnl_after_sell(tmp_path):
    state_path = tmp_path / "paper_state.json"
    broker = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
    )
    broker.seed_position("SOFI", quantity=10, avg_price=10.0)

    sell = broker.place_order(
        "SOFI",
        OrderSide.SELL,
        5,
        OrderType.MARKET,
        current_bid=12.0,
        current_ask=12.0,
    )

    assert sell.status == OrderStatus.FILLED
    state = read_paper_portfolio_state(state_path)
    assert state is not None
    assert state["realized_pnl"] > 0
    assert state["positions"][0]["quantity"] == 5
    assert state["fill_sequence"] == 1


def test_notifier_reads_paper_portfolio_state_without_writing(tmp_path, monkeypatch):
    state_path = tmp_path / "paper_state.json"
    write_paper_portfolio_state(
        PaperPortfolioState(cash=900.0, equity=900.0, buying_power=1_800.0),
        path=state_path,
    )
    monkeypatch.setenv("SOXS_PAPER_PORTFOLIO_STATE_PATH", str(state_path))
    notifier = Notifier(console=False, macos_notification=False, webhook_url=None)
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("SOFI", "BUY", 1, 10.0, mode="paper")

    assert calls
    assert "现金 $900.00" in calls[0][0][1]
    assert "权益 $900.00" in calls[0][0][1]


def test_outcome_collector_reads_unified_paper_state(tmp_path):
    state_path = tmp_path / "paper_state.json"
    write_paper_portfolio_state(PaperPortfolioState(cash=800.0, equity=800.0), path=state_path)

    collector = OutcomeCollector(paper_state_path=state_path)

    state = collector.load_paper_portfolio_state()
    assert state is not None
    assert state["cash"] == 800.0
    assert state["equity"] == 800.0


def test_default_paper_portfolio_state_path_is_shared_account(monkeypatch, tmp_path):
    monkeypatch.setenv("SOXS_STATE_DIR", str(tmp_path / "state"))
    path = default_paper_portfolio_state_path()

    assert path == (tmp_path / "state" / "paper" / "paper-default" / "portfolio_state.json").resolve()


def test_state_version_increments_on_each_successful_save(tmp_path):
    state_path = tmp_path / "paper_state.json"

    first = write_paper_portfolio_state(PaperPortfolioState(cash=100.0, equity=100.0), path=state_path)
    second = write_paper_portfolio_state(PaperPortfolioState(cash=100.0, equity=100.0), path=state_path)

    assert first["state_version"] == 1
    assert second["state_version"] == 2
    assert second["fill_sequence"] == 0


def test_duplicate_fill_id_does_not_increment_fill_sequence(tmp_path):
    state_path = tmp_path / "paper_state.json"
    state = PaperPortfolioState(cash=100.0, equity=100.0, last_fill_id="fill-1", last_event_id="event-1")

    first = write_paper_portfolio_state(state, path=state_path)
    second = write_paper_portfolio_state(state, path=state_path)

    assert first["fill_sequence"] == 1
    assert second["fill_sequence"] == 1
    assert second["processed_fill_ids"] == ["fill-1"]


def test_last_fill_time_is_timezone_aware_iso(tmp_path):
    state_path = tmp_path / "paper_state.json"
    payload = write_paper_portfolio_state(
        PaperPortfolioState(cash=100.0, equity=100.0, last_fill_id="fill-time"),
        path=state_path,
    )

    parsed = datetime.fromisoformat(payload["last_fill_time"].replace("Z", "+00:00"))
    updated = datetime.fromisoformat(payload["last_update_time"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed <= updated


def test_writer_audit_fields_are_persisted(tmp_path):
    state_path = tmp_path / "paper_state.json"
    store = PaperPortfolioStateStore(
        path=state_path,
        account_id="paper-default",
        writer_port=8091,
        writer_mode="test",
        writer_run_id="run-test",
    )

    with store.locked():
        saved = store.save(PaperPortfolioState(cash=100.0, equity=100.0, execution_mode="test", writer_mode="test"))

    payload = saved.to_dict()
    assert payload["writer_pid"] == os.getpid()
    assert payload["writer_port"] == 8091
    assert payload["writer_mode"] == "test"
    assert payload["writer_run_id"] == "run-test"


def test_corrupt_state_does_not_reset_funds(tmp_path):
    state_path = tmp_path / "paper_state.json"
    state_path.write_text("{bad-json", encoding="utf-8")
    store = PaperPortfolioStateStore(path=state_path)

    with pytest.raises(PaperPortfolioStateCorruptError):
        store.load()

    assert state_path.read_text(encoding="utf-8") == "{bad-json"
    assert list(tmp_path.glob("paper_state.json.corrupt.*"))


def test_two_paper_broker_writers_do_not_lose_updates(tmp_path):
    state_path = tmp_path / "paper_state.json"
    first = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
        writer_run_id="writer-1",
    )
    second = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
        writer_run_id="writer-2",
    )

    buy_one = first.place_order("SOFI", OrderSide.BUY, 10, OrderType.LIMIT, current_bid=10.0, current_ask=10.0)
    buy_two = second.place_order("SOFI", OrderSide.BUY, 5, OrderType.LIMIT, current_bid=10.0, current_ask=10.0)

    assert buy_one.status == OrderStatus.FILLED
    assert buy_two.status == OrderStatus.FILLED
    state = read_paper_portfolio_state(state_path)
    assert state["cash"] == 850.0
    assert state["positions"][0]["quantity"] == 15
    assert state["fill_sequence"] == 2


def test_paper_broker_rejects_invalid_fill_inputs_without_state_change(tmp_path):
    state_path = tmp_path / "paper_state.json"
    broker = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
    )

    invalid_qty = broker.place_order("SOFI", OrderSide.BUY, 0, OrderType.MARKET, current_bid=10.0, current_ask=10.0)
    invalid_price = broker.place_order("SOFI", OrderSide.BUY, 1, OrderType.MARKET, current_bid=0.0, current_ask=0.0)

    assert invalid_qty.status == OrderStatus.REJECTED
    assert invalid_qty.notes == "Invalid quantity"
    assert invalid_price.status == OrderStatus.REJECTED
    state = read_paper_portfolio_state(state_path)
    assert state["cash"] == 1_000.0
    assert state["fill_sequence"] == 0


def test_paper_broker_rejects_sell_over_position_without_state_change(tmp_path):
    state_path = tmp_path / "paper_state.json"
    broker = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
    )

    sell = broker.place_order("SOFI", OrderSide.SELL, 1, OrderType.MARKET, current_bid=10.0, current_ask=10.0)

    assert sell.status == OrderStatus.REJECTED
    assert sell.notes == "Insufficient position"
    state = read_paper_portfolio_state(state_path)
    assert state["cash"] == 1_000.0
    assert state["fill_sequence"] == 0


def test_closed_position_is_removed_from_state(tmp_path):
    state_path = tmp_path / "paper_state.json"
    broker = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
    )

    broker.place_order("SOFI", OrderSide.BUY, 10, OrderType.MARKET, current_bid=10.0, current_ask=10.0)
    broker.place_order("SOFI", OrderSide.SELL, 10, OrderType.MARKET, current_bid=11.0, current_ask=11.0)

    state = read_paper_portfolio_state(state_path)
    assert state["positions"] == []
    assert state["positions_count"] == 0


def test_persist_failure_returns_rejected_order(tmp_path):
    state_path = tmp_path / "paper_state.json"
    broker = PaperBroker(
        initial_cash=1_000.0,
        commission_per_share=0.0,
        slippage_pct=0.0,
        portfolio_state_path=state_path,
        persist_portfolio_state=True,
    )
    monkeypatch_store = broker._state_store
    assert monkeypatch_store is not None
    monkeypatch_store._atomic_write = lambda _state: (_ for _ in ()).throw(
        PaperPortfolioStateError("forced_persist_failure")
    )

    order = broker.place_order("SOFI", OrderSide.BUY, 1, OrderType.MARKET, current_bid=10.0, current_ask=10.0)

    assert order.status == OrderStatus.REJECTED
    assert "PERSIST_FAILED" in order.notes
