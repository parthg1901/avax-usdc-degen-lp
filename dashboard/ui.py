from typing import Any

from almanak.framework.dashboard.templates import get_traderjoe_v2_config, render_lp_dashboard


def _build_lp_config(strategy_config: dict[str, Any]):
    pool = str(strategy_config.get("pool", "WAVAX/USDC/20"))
    pool_parts = pool.split("/")

    token0 = str(strategy_config.get("base_token", pool_parts[0] if len(pool_parts) > 0 else "WAVAX"))
    token1 = str(strategy_config.get("quote_token", pool_parts[1] if len(pool_parts) > 1 else "USDC"))
    bin_step = str(pool_parts[2] if len(pool_parts) > 2 else "20")
    chain = str(strategy_config.get("chain", "avalanche"))

    return get_traderjoe_v2_config(
        token0=token0,
        token1=token1,
        bin_step=bin_step,
        chain=chain,
    )


def render_custom_dashboard(
    strategy_id: str,
    strategy_config: dict[str, Any],
    api_client: Any,
    session_state: dict[str, Any],
) -> None:
    config = _build_lp_config(strategy_config)
    render_lp_dashboard(strategy_id, strategy_config, session_state, config)
