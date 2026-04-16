import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy import AvaxUsdcDegenLpStrategy


@pytest.fixture
def config() -> dict:
    with open(Path(__file__).parent.parent / "config.json") as f:
        return json.load(f)


@pytest.fixture
def strategy(config: dict) -> AvaxUsdcDegenLpStrategy:
    return AvaxUsdcDegenLpStrategy(
        config=config,
        chain="avalanche",
        wallet_address="0x" + "1" * 40,
    )


def make_market(
    *,
    rsi: Decimal = Decimal("50"),
    fee_apr: Decimal = Decimal("80"),
    atr: Decimal = Decimal("0.5"),
    best_pool_available: bool = True,
    price_wavax: Decimal = Decimal("30"),
    total_portfolio_usd: Decimal = Decimal("100"),
    wavax_balance: Decimal = Decimal("3"),
    usdc_balance: Decimal = Decimal("200"),
    il_percent: Decimal = Decimal("0.3"),
) -> MagicMock:
    market = MagicMock()
    market.rsi.return_value = SimpleNamespace(value=rsi)

    def _price(token: str):
        return price_wavax if token == "WAVAX" else Decimal("1")

    market.price.side_effect = _price

    def _balance(token: str):
        if token == "WAVAX":
            return SimpleNamespace(balance=wavax_balance, balance_usd=wavax_balance * price_wavax)
        return SimpleNamespace(balance=usdc_balance, balance_usd=usdc_balance)

    market.balance.side_effect = _balance
    if best_pool_available:
        market.best_pool.return_value = SimpleNamespace(value=SimpleNamespace(fee_apr=float(fee_apr)))
    else:
        market.best_pool = None

    market.atr.return_value = SimpleNamespace(value=atr)
    market.total_portfolio_usd.return_value = total_portfolio_usd
    market.projected_il.return_value = SimpleNamespace(il_percent=il_percent)
    return market


def activate_strategy(strategy: AvaxUsdcDegenLpStrategy, now: datetime, entry_portfolio: Decimal = Decimal("100")) -> None:
    strategy._now = lambda: now
    strategy._state = strategy.STATE_ACTIVE
    strategy._position_id = strategy._lp_position_id()
    strategy._runtime_start = now - timedelta(hours=1)
    strategy._entry_time = now - timedelta(hours=1)
    strategy._entry_portfolio_usd = entry_portfolio
    strategy._peak_portfolio_usd = entry_portfolio
    strategy._has_entered_once = True


def test_entry_opens_lp_when_gates_pass(strategy: AvaxUsdcDegenLpStrategy):
    market = make_market()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    strategy._now = lambda: now

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"
    assert intent.protocol == "traderjoe_v2"
    assert intent.pool == "WAVAX/USDC/20"


def test_entry_holds_when_rsi_out_of_band(strategy: AvaxUsdcDegenLpStrategy):
    market = make_market(rsi=Decimal("70"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "ENTRY_RSI_FAIL"


def test_entry_holds_when_fee_apr_is_weak(strategy: AvaxUsdcDegenLpStrategy):
    market = make_market(fee_apr=Decimal("5"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "ENTRY_FEE_FAIL"


def test_entry_uses_atr_fallback_when_best_pool_unavailable(strategy: AvaxUsdcDegenLpStrategy):
    market = make_market(best_pool_available=False, atr=Decimal("0.6"), price_wavax=Decimal("30"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_OPEN"


def test_entry_holds_when_atr_fallback_is_weak(strategy: AvaxUsdcDegenLpStrategy):
    market = make_market(best_pool_available=False, atr=Decimal("0.1"), price_wavax=Decimal("30"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "ENTRY_FEE_FAIL"


def test_partial_take_profit_collects_fees(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now, entry_portfolio=Decimal("100"))
    market = make_market(total_portfolio_usd=Decimal("102.5"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_COLLECT_FEES"


def test_partial_take_profit_giveback_closes_position(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now, entry_portfolio=Decimal("100"))
    strategy._partial_tp_taken = True
    market = make_market(total_portfolio_usd=Decimal("100.4"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_full_take_profit_closes_and_terminates(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now, entry_portfolio=Decimal("100"))
    market = make_market(total_portfolio_usd=Decimal("104.5"))

    intent = strategy.decide(market)
    strategy.on_intent_executed(intent, True, SimpleNamespace())

    assert intent.intent_type.value == "LP_CLOSE"
    assert strategy._state == strategy.STATE_TERMINATED
    assert strategy._terminate_reason == "FULL_TAKE_PROFIT"


def test_hard_stop_loss_disables_reentry(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now, entry_portfolio=Decimal("100"))
    market = make_market(total_portfolio_usd=Decimal("96"))

    intent = strategy.decide(market)
    strategy.on_intent_executed(intent, True, SimpleNamespace())

    assert intent.intent_type.value == "LP_CLOSE"
    assert strategy._hard_stop_triggered is True
    assert strategy._state == strategy.STATE_TERMINATED


def test_edge_decay_persistence_closes(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now)
    strategy._edge_decay_count = strategy.edge_decay_checks - 1
    market = make_market(fee_apr=Decimal("1"), total_portfolio_usd=Decimal("100.2"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_pnl_stall_triggers_close(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now)
    strategy._stall_count = strategy.pnl_stall_consecutive - 1
    strategy._pnl_samples = [
        {"ts": (now - timedelta(hours=3)).isoformat(), "pnl_pct": "0.0010"},
        {"ts": (now - timedelta(hours=1)).isoformat(), "pnl_pct": "0.0011"},
    ]
    market = make_market(total_portfolio_usd=Decimal("100.12"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_max_runtime_triggers_close(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    activate_strategy(strategy, now)
    strategy._runtime_start = now - timedelta(hours=25)
    market = make_market(total_portfolio_usd=Decimal("100.4"))

    intent = strategy.decide(market)

    assert intent.intent_type.value == "LP_CLOSE"


def test_reentry_limited_to_one(strategy: AvaxUsdcDegenLpStrategy):
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    strategy._now = lambda: now
    strategy._state = strategy.STATE_COOLDOWN
    strategy._has_entered_once = True
    strategy._reentry_count = strategy.max_reentries
    strategy._cooldown_until = now - timedelta(minutes=1)

    intent = strategy.decide(make_market())

    assert intent.intent_type.value == "HOLD"
    assert strategy._state == strategy.STATE_TERMINATED
    assert strategy._terminate_reason == "MAX_REENTRIES"


def test_teardown_emits_lp_close_for_active_position(strategy: AvaxUsdcDegenLpStrategy):
    strategy._state = strategy.STATE_ACTIVE
    strategy._position_id = strategy._lp_position_id()

    summary = strategy.get_open_positions()
    intents = strategy.generate_teardown_intents()

    assert len(summary.positions) == 1
    assert summary.positions[0].position_id == strategy._lp_position_id()
    assert len(intents) == 1
    assert intents[0].intent_type.value == "LP_CLOSE"


def test_decide_handles_market_errors(strategy: AvaxUsdcDegenLpStrategy):
    market = make_market()
    market.rsi.side_effect = ValueError("rsi failed")

    intent = strategy.decide(market)

    assert intent.intent_type.value == "HOLD"
    assert intent.reason_code == "DATA_UNAVAILABLE"
