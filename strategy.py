import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from almanak.framework.intents import Intent
from almanak.framework.strategies import IntentStrategy, MarketSnapshot, almanak_strategy

logger = logging.getLogger(__name__)


@almanak_strategy(
    name="avax_usdc_degen_lp",
    description="Short-lived unhedged WAVAX/USDC LP fee harvest strategy on TraderJoe V2",
    version="1.0.0",
    author="Almanak",
    tags=["lp", "traderjoe_v2", "avalanche", "wavax", "usdc"],
    supported_chains=["avalanche"],
    supported_protocols=["traderjoe_v2"],
    intent_types=["LP_OPEN", "LP_CLOSE", "COLLECT_FEES", "HOLD"],
    default_chain="avalanche",
)
class AvaxUsdcDegenLpStrategy(IntentStrategy):
    STATE_IDLE = "idle"
    STATE_ACTIVE = "active"
    STATE_COOLDOWN = "cooldown"
    STATE_TERMINATED = "terminated"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.pool = str(self.get_config("pool", "WAVAX/USDC/20"))
        self.protocol = str(self.get_config("protocol", "traderjoe_v2"))
        self.base_token = str(self.get_config("base_token", "WAVAX"))
        self.quote_token = str(self.get_config("quote_token", "USDC"))

        self.rsi_period = int(self.get_config("rsi_period", 14))
        self.rsi_timeframe = str(self.get_config("rsi_timeframe", "1h"))
        self.rsi_entry_low = Decimal(str(self.get_config("rsi_entry_low", "45")))
        self.rsi_entry_high = Decimal(str(self.get_config("rsi_entry_high", "60")))
        self.min_fee_apr = Decimal(str(self.get_config("min_fee_apr", "70")))
        self.min_atr_pct_for_entry = Decimal(str(self.get_config("min_atr_pct_for_entry", "1.2")))
        self.edge_decay_checks = int(self.get_config("edge_decay_checks", 3))

        self.target_notional_usd = Decimal(str(self.get_config("target_notional_usd", "90")))
        self.min_position_usd = Decimal(str(self.get_config("min_position_usd", "80")))
        self.range_width_pct = Decimal(str(self.get_config("range_width_pct", "0.08")))
        self.bin_range = int(self.get_config("bin_range", 5))
        self.id_slippage = int(self.get_config("id_slippage", 20))

        self.partial_take_profit_pct = Decimal(str(self.get_config("partial_take_profit_pct", "0.02")))
        self.partial_tp_floor_pct = Decimal(str(self.get_config("partial_tp_floor_pct", "0.005")))
        self.full_take_profit_pct = Decimal(str(self.get_config("full_take_profit_pct", "0.04")))
        self.hard_stop_loss_pct = Decimal(str(self.get_config("hard_stop_loss_pct", "-0.03")))
        self.max_drawdown_pct = Decimal(str(self.get_config("max_drawdown_pct", "0.05")))
        self.max_projected_il_pct = Decimal(str(self.get_config("max_projected_il_pct", "1.5")))
        self.il_scenario_price_change_pct = Decimal(str(self.get_config("il_scenario_price_change_pct", "20")))

        self.max_runtime_hours = int(self.get_config("max_runtime_hours", 24))
        self.pnl_stall_lookback_hours = int(self.get_config("pnl_stall_lookback_hours", 4))
        self.pnl_slope_min_pct_per_hour = Decimal(str(self.get_config("pnl_slope_min_pct_per_hour", "0.0005")))
        self.pnl_stall_consecutive = int(self.get_config("pnl_stall_consecutive", 3))

        self.max_reentries = int(self.get_config("max_reentries", 1))
        self.reentry_cooldown_hours = int(self.get_config("reentry_cooldown_hours", 4))

        self._state = self.STATE_IDLE
        self._position_id: str | None = None
        self._runtime_start: datetime | None = None
        self._entry_time: datetime | None = None
        self._entry_portfolio_usd: Decimal | None = None
        self._peak_portfolio_usd: Decimal | None = None
        self._partial_tp_taken = False
        self._hard_stop_triggered = False
        self._terminate_reason: str | None = None

        self._has_entered_once = False
        self._reentry_count = 0
        self._cooldown_until: datetime | None = None
        self._edge_decay_count = 0
        self._stall_count = 0
        self._pnl_samples: list[dict[str, str]] = []

        self._pending_termination = False
        self._pending_close_reason: str | None = None

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _hold(self, reason: str, reason_code: str) -> Intent:
        return Intent.hold(reason=reason, reason_code=reason_code)

    def _lp_position_id(self) -> str:
        return f"traderjoe-lp-{self.pool.replace('/', '-')}"

    def _extract_numeric_value(self, payload: Any) -> Decimal | None:
        current = payload
        if hasattr(current, "value"):
            current = current.value

        if current is None:
            return None

        if isinstance(current, dict):
            for key in ("value", "fee_apr", "atr"):
                if key in current and current[key] is not None:
                    return Decimal(str(current[key]))
            return None

        for attr in ("fee_apr", "atr", "value"):
            if hasattr(current, attr):
                value = getattr(current, attr)
                if value is not None:
                    return Decimal(str(value))

        try:
            return Decimal(str(current))
        except Exception:
            return None

    def _extract_fee_apr(self, payload: Any) -> Decimal | None:
        current = payload
        if hasattr(current, "value"):
            current = current.value

        candidates = [current]
        if hasattr(current, "best_pool"):
            candidates.append(current.best_pool)
        if hasattr(current, "pool"):
            candidates.append(current.pool)

        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, dict) and "fee_apr" in candidate:
                return Decimal(str(candidate["fee_apr"]))
            if hasattr(candidate, "fee_apr"):
                return Decimal(str(candidate.fee_apr))
        return None

    def _get_fee_signal(self, market: MarketSnapshot, price_base: Decimal) -> tuple[Decimal, str, Decimal]:
        if hasattr(market, "best_pool"):
            try:
                data = market.best_pool(
                    self.base_token,
                    self.quote_token,
                    chain=self.chain,
                    metric="fee_apr",
                    protocols=[self.protocol],
                )
                fee_apr = self._extract_fee_apr(data)
                if fee_apr is not None:
                    return fee_apr, "fee_apr", self.min_fee_apr
            except Exception as exc:
                logger.debug("best_pool unavailable; falling back to ATR proxy: %s", exc)

        if hasattr(market, "atr"):
            atr_data = market.atr(self.base_token, period=self.rsi_period, timeframe=self.rsi_timeframe)
            atr_value = self._extract_numeric_value(atr_data)
            if atr_value is not None and price_base > Decimal("0"):
                atr_pct = (atr_value / price_base) * Decimal("100")
                return atr_pct, "atr_pct", self.min_atr_pct_for_entry

        raise ValueError("fee signal unavailable")

    def _read_pnl_state(self, market: MarketSnapshot) -> tuple[Decimal, Decimal]:
        total_portfolio = Decimal(str(market.total_portfolio_usd()))
        if self._entry_portfolio_usd is None or self._entry_portfolio_usd <= Decimal("0"):
            raise ValueError("entry portfolio baseline missing")
        pnl_pct = (total_portfolio - self._entry_portfolio_usd) / self._entry_portfolio_usd
        return total_portfolio, pnl_pct

    def _record_pnl_sample(self, now: datetime, pnl_pct: Decimal) -> None:
        self._pnl_samples.append({"ts": now.isoformat(), "pnl_pct": str(pnl_pct)})
        max_samples = 96
        if len(self._pnl_samples) > max_samples:
            self._pnl_samples = self._pnl_samples[-max_samples:]

    def _pnl_slope_per_hour(self, now: datetime) -> Decimal | None:
        if len(self._pnl_samples) < 2:
            return None

        cutoff = now - timedelta(hours=self.pnl_stall_lookback_hours)
        window_samples: list[tuple[datetime, Decimal]] = []
        for point in self._pnl_samples:
            ts = datetime.fromisoformat(point["ts"])
            pnl_pct = Decimal(point["pnl_pct"])
            if ts >= cutoff:
                window_samples.append((ts, pnl_pct))

        if len(window_samples) < 2:
            return None

        first_ts, first_pnl = window_samples[0]
        last_ts, last_pnl = window_samples[-1]
        hours = Decimal(str((last_ts - first_ts).total_seconds())) / Decimal("3600")
        if hours <= Decimal("0"):
            return None
        return (last_pnl - first_pnl) / hours

    def _terminate_with_close(self, reason: str) -> Intent:
        self._pending_termination = True
        self._pending_close_reason = reason
        return Intent.lp_close(
            position_id=self._position_id or self._lp_position_id(),
            pool=self.pool,
            collect_fees=True,
            protocol=self.protocol,
            chain=self.chain,
        )

    def _parse_pool_tokens(self) -> tuple[str, str]:
        parts = self.pool.split("/")
        token0 = parts[0] if len(parts) > 0 else self.base_token
        token1 = parts[1] if len(parts) > 1 else self.quote_token
        return token0, token1

    def _entry_amounts(self, market: MarketSnapshot, price_base: Decimal, price_quote: Decimal) -> tuple[Decimal, Decimal] | None:
        base_balance = market.balance(self.base_token)
        quote_balance = market.balance(self.quote_token)

        half_notional = self.target_notional_usd / Decimal("2")
        desired_base = half_notional / price_base
        desired_quote = half_notional / price_quote

        amount_base = min(desired_base, Decimal(str(base_balance.balance)))
        amount_quote = min(desired_quote, Decimal(str(quote_balance.balance)))

        notional = amount_base * price_base + amount_quote * price_quote
        if notional < self.min_position_usd:
            return None
        if amount_base <= Decimal("0") or amount_quote <= Decimal("0"):
            return None
        return amount_base, amount_quote

    def decide(self, market: MarketSnapshot):
        now = self._now()
        if self._runtime_start is None:
            self._runtime_start = now

        if self._state == self.STATE_TERMINATED:
            return self._hold(f"Strategy terminated: {self._terminate_reason}", "TERMINATED")

        try:
            rsi_data = market.rsi(self.base_token, period=self.rsi_period, timeframe=self.rsi_timeframe)
            rsi_value = Decimal(str(rsi_data.value))
            price_base = Decimal(str(market.price(self.base_token)))
            price_quote = Decimal(str(market.price(self.quote_token)))
            fee_signal, fee_signal_source, fee_signal_threshold = self._get_fee_signal(market, price_base)
        except Exception as exc:
            logger.warning("Market data unavailable: %s", exc)
            return self._hold(f"Market data unavailable: {exc}", "DATA_UNAVAILABLE")

        if self._state == self.STATE_ACTIVE:
            if self._entry_portfolio_usd is None:
                try:
                    baseline = Decimal(str(market.total_portfolio_usd()))
                except Exception as exc:
                    return self._hold(f"PnL state unavailable: {exc}", "PNL_UNAVAILABLE")
                self._entry_portfolio_usd = baseline
                self._peak_portfolio_usd = baseline

            try:
                total_portfolio, pnl_pct = self._read_pnl_state(market)
            except Exception as exc:
                return self._hold(f"PnL state unavailable: {exc}", "PNL_UNAVAILABLE")

            self._record_pnl_sample(now, pnl_pct)
            if self._peak_portfolio_usd is None or total_portfolio > self._peak_portfolio_usd:
                self._peak_portfolio_usd = total_portfolio

            runtime_hours = Decimal(str((now - self._runtime_start).total_seconds())) / Decimal("3600")
            if runtime_hours >= Decimal(str(self.max_runtime_hours)):
                return self._terminate_with_close("MAX_RUNTIME")

            drawdown_pct = Decimal("0")
            if self._peak_portfolio_usd and self._peak_portfolio_usd > Decimal("0"):
                drawdown_pct = (self._peak_portfolio_usd - total_portfolio) / self._peak_portfolio_usd

            if pnl_pct <= self.hard_stop_loss_pct:
                self._hard_stop_triggered = True
                return self._terminate_with_close("HARD_STOP_LOSS")

            if drawdown_pct >= self.max_drawdown_pct:
                self._hard_stop_triggered = True
                return self._terminate_with_close("DRAWDOWN_GUARD")

            try:
                il_projection = market.projected_il(
                    self.base_token,
                    self.quote_token,
                    price_change_pct=self.il_scenario_price_change_pct,
                )
                il_pct = abs(Decimal(str(il_projection.il_percent)))
            except Exception:
                il_pct = Decimal("0")

            if il_pct >= self.max_projected_il_pct:
                self._hard_stop_triggered = True
                return self._terminate_with_close("IL_GUARD")

            if pnl_pct >= self.full_take_profit_pct:
                return self._terminate_with_close("FULL_TAKE_PROFIT")

            if pnl_pct >= self.partial_take_profit_pct and not self._partial_tp_taken:
                return Intent.collect_fees(pool=self.pool, protocol=self.protocol, chain=self.chain)

            if self._partial_tp_taken and pnl_pct <= self.partial_tp_floor_pct:
                return self._terminate_with_close("PARTIAL_TP_GIVEBACK")

            if fee_signal < fee_signal_threshold:
                self._edge_decay_count += 1
            else:
                self._edge_decay_count = 0
            if self._edge_decay_count >= self.edge_decay_checks:
                return self._terminate_with_close("EDGE_DECAY")

            pnl_slope = self._pnl_slope_per_hour(now)
            if pnl_slope is not None and pnl_slope <= self.pnl_slope_min_pct_per_hour:
                self._stall_count += 1
            else:
                self._stall_count = 0
            if self._stall_count >= self.pnl_stall_consecutive:
                return self._terminate_with_close("PNL_STALL")

            return self._hold(
                f"LP active. pnl={pnl_pct:.2%}, edge={fee_signal:.2f} ({fee_signal_source})",
                "LP_ACTIVE",
            )

        if self._state == self.STATE_COOLDOWN:
            if self._hard_stop_triggered:
                self._state = self.STATE_TERMINATED
                self._terminate_reason = "NO_REENTRY_AFTER_HARD_STOP"
                return self._hold("No re-entry after hard stop", "TERMINATED")

            if self._reentry_count >= self.max_reentries:
                self._state = self.STATE_TERMINATED
                self._terminate_reason = "MAX_REENTRIES"
                return self._hold("Max re-entry count reached", "TERMINATED")

            if self._cooldown_until and now < self._cooldown_until:
                remaining = self._cooldown_until - now
                return self._hold(
                    f"Cooldown active ({remaining})",
                    "COOLDOWN",
                )

            self._state = self.STATE_IDLE

        if self._hard_stop_triggered:
            self._state = self.STATE_TERMINATED
            self._terminate_reason = "NO_REENTRY_AFTER_HARD_STOP"
            return self._hold("No re-entry after hard stop", "TERMINATED")

        if self._has_entered_once and self._reentry_count >= self.max_reentries:
            self._state = self.STATE_TERMINATED
            self._terminate_reason = "MAX_REENTRIES"
            return self._hold("Max re-entry count reached", "TERMINATED")

        if not (self.rsi_entry_low <= rsi_value <= self.rsi_entry_high):
            return self._hold(f"RSI {rsi_value:.2f} outside entry band", "ENTRY_RSI_FAIL")

        if fee_signal < fee_signal_threshold:
            metric = "fee APR" if fee_signal_source == "fee_apr" else "ATR%"
            return self._hold(
                f"{metric} {fee_signal:.2f} below threshold {fee_signal_threshold:.2f}",
                "ENTRY_FEE_FAIL",
            )

        try:
            amounts = self._entry_amounts(market, price_base, price_quote)
        except Exception as exc:
            return self._hold(f"Balance unavailable: {exc}", "INSUFFICIENT_BALANCE")
        if amounts is None:
            return self._hold("Not enough balance for target LP size", "INSUFFICIENT_BALANCE")

        amount_base, amount_quote = amounts
        token0, token1 = self._parse_pool_tokens()
        amount0 = amount_base if token0 == self.base_token else amount_quote
        amount1 = amount_quote if token1 == self.quote_token else amount_base

        half_width = self.range_width_pct / Decimal("2")
        range_lower = price_base * (Decimal("1") - half_width)
        range_upper = price_base * (Decimal("1") + half_width)

        return Intent.lp_open(
            pool=self.pool,
            amount0=amount0,
            amount1=amount1,
            range_lower=range_lower,
            range_upper=range_upper,
            protocol=self.protocol,
            chain=self.chain,
            protocol_params={
                "bin_range": self.bin_range,
                "id_slippage": self.id_slippage,
            },
        )

    def on_intent_executed(self, intent, success: bool, result):
        if not success:
            if self._pending_termination:
                self._pending_termination = False
                self._pending_close_reason = None
            return

        intent_type = getattr(intent, "intent_type", None)
        intent_type_value = intent_type.value if intent_type else ""

        if intent_type_value == "LP_OPEN":
            self._state = self.STATE_ACTIVE
            self._position_id = str(getattr(result, "position_id", self._lp_position_id()) or self._lp_position_id())
            self._entry_time = self._now()
            self._partial_tp_taken = False
            self._edge_decay_count = 0
            self._stall_count = 0
            self._pnl_samples = []

            if not self._has_entered_once:
                self._has_entered_once = True
            else:
                self._reentry_count += 1

        elif intent_type_value == "COLLECT_FEES":
            self._partial_tp_taken = True

        elif intent_type_value == "LP_CLOSE":
            self._position_id = None
            self._entry_time = None
            self._entry_portfolio_usd = None
            self._peak_portfolio_usd = None
            self._partial_tp_taken = False
            self._edge_decay_count = 0
            self._stall_count = 0
            self._pnl_samples = []

            if self._pending_termination:
                self._state = self.STATE_TERMINATED
                self._terminate_reason = self._pending_close_reason or "TERMINATED"
                self._pending_termination = False
                self._pending_close_reason = None
            else:
                self._state = self.STATE_COOLDOWN
                self._cooldown_until = self._now() + timedelta(hours=self.reentry_cooldown_hours)

    def get_status(self) -> dict[str, Any]:
        return {
            "strategy": "avax_usdc_degen_lp",
            "chain": self.chain,
            "state": self._state,
            "position_id": self._position_id,
            "reentry_count": self._reentry_count,
            "terminate_reason": self._terminate_reason,
            "hard_stop_triggered": self._hard_stop_triggered,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
        }

    def get_persistent_state(self):
        return {
            "state": self._state,
            "position_id": self._position_id,
            "runtime_start": self._runtime_start.isoformat() if self._runtime_start else None,
            "entry_time": self._entry_time.isoformat() if self._entry_time else None,
            "entry_portfolio_usd": str(self._entry_portfolio_usd) if self._entry_portfolio_usd else None,
            "peak_portfolio_usd": str(self._peak_portfolio_usd) if self._peak_portfolio_usd else None,
            "partial_tp_taken": self._partial_tp_taken,
            "hard_stop_triggered": self._hard_stop_triggered,
            "terminate_reason": self._terminate_reason,
            "has_entered_once": self._has_entered_once,
            "reentry_count": self._reentry_count,
            "cooldown_until": self._cooldown_until.isoformat() if self._cooldown_until else None,
            "edge_decay_count": self._edge_decay_count,
            "stall_count": self._stall_count,
            "pnl_samples": self._pnl_samples,
        }

    def load_persistent_state(self, state):
        if not state:
            return

        self._state = state.get("state", self.STATE_IDLE)
        self._position_id = state.get("position_id")

        runtime_start = state.get("runtime_start")
        self._runtime_start = datetime.fromisoformat(runtime_start) if runtime_start else None

        entry_time = state.get("entry_time")
        self._entry_time = datetime.fromisoformat(entry_time) if entry_time else None

        entry_portfolio = state.get("entry_portfolio_usd")
        self._entry_portfolio_usd = Decimal(entry_portfolio) if entry_portfolio else None

        peak_portfolio = state.get("peak_portfolio_usd")
        self._peak_portfolio_usd = Decimal(peak_portfolio) if peak_portfolio else None

        self._partial_tp_taken = bool(state.get("partial_tp_taken", False))
        self._hard_stop_triggered = bool(state.get("hard_stop_triggered", False))
        self._terminate_reason = state.get("terminate_reason")
        self._has_entered_once = bool(state.get("has_entered_once", False))
        self._reentry_count = int(state.get("reentry_count", 0))

        cooldown_until = state.get("cooldown_until")
        self._cooldown_until = datetime.fromisoformat(cooldown_until) if cooldown_until else None

        self._edge_decay_count = int(state.get("edge_decay_count", 0))
        self._stall_count = int(state.get("stall_count", 0))
        self._pnl_samples = state.get("pnl_samples", [])

    def supports_teardown(self) -> bool:
        return True

    def get_open_positions(self):
        from almanak.framework.teardown import PositionInfo, PositionType, TeardownPositionSummary

        positions = []
        if self._state == self.STATE_ACTIVE and self._position_id:
            positions.append(
                PositionInfo(
                    position_type=PositionType.LP,
                    position_id=self._position_id,
                    chain=self.chain,
                    protocol=self.protocol,
                    value_usd=Decimal("0"),
                    details={
                        "pool": self.pool,
                        "base_token": self.base_token,
                        "quote_token": self.quote_token,
                        "bin_range": self.bin_range,
                        "mode": self._state,
                    },
                )
            )

        return TeardownPositionSummary(
            strategy_id=getattr(self, "strategy_id", "avax_usdc_degen_lp"),
            timestamp=self._now(),
            positions=positions,
        )

    def generate_teardown_intents(self, mode=None, market=None) -> list[Intent]:
        if self._state != self.STATE_ACTIVE or not self._position_id:
            return []

        return [
            Intent.lp_close(
                position_id=self._position_id,
                pool=self.pool,
                collect_fees=True,
                protocol=self.protocol,
                chain=self.chain,
            )
        ]
