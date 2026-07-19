from src.broker.base import OrderSide, OrderStatus, OrderType
from src.broker.paper_broker import PaperBroker
from src.broker.paper_portfolio_state import (
    PaperPortfolioState,
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
    assert state["positions"][0]["quantity"] == 10
    assert buy.order_id in state["processed_fill_ids"]
    assert state["last_fill_id"] == buy.order_id


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


def test_notifier_reads_paper_portfolio_state_without_writing(tmp_path, monkeypatch):
    state_path = tmp_path / "paper_state.json"
    write_paper_portfolio_state(
        PaperPortfolioState(cash=900.0, equity=1_010.0),
        path=state_path,
    )
    monkeypatch.setenv("SOXS_PAPER_PORTFOLIO_STATE_PATH", str(state_path))
    notifier = Notifier(console=False, macos_notification=False, webhook_url=None)
    calls = []
    monkeypatch.setattr(notifier, "_send", lambda *args, **kwargs: calls.append((args, kwargs)))

    notifier.trade("SOFI", "BUY", 1, 10.0, mode="paper")

    assert calls
    assert "现金 $900.00" in calls[0][0][1]
    assert "权益 $1,010.00" in calls[0][0][1]


def test_outcome_collector_reads_unified_paper_state(tmp_path):
    state_path = tmp_path / "paper_state.json"
    write_paper_portfolio_state(PaperPortfolioState(cash=800.0, equity=950.0), path=state_path)

    collector = OutcomeCollector(paper_state_path=state_path)

    state = collector.load_paper_portfolio_state()
    assert state is not None
    assert state["cash"] == 800.0
    assert state["equity"] == 950.0
