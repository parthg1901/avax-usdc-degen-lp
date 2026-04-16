from almanak.framework.dashboard.templates import LPDashboardConfig

from dashboard.ui import _build_lp_config, render_custom_dashboard


def test_dashboard_imports():
    assert callable(render_custom_dashboard)


def test_build_lp_config_from_strategy_config():
    strategy_config = {
        "pool": "WAVAX/USDC/20",
        "base_token": "WAVAX",
        "quote_token": "USDC",
        "chain": "avalanche",
    }

    config = _build_lp_config(strategy_config)

    assert isinstance(config, LPDashboardConfig)
    assert config.protocol == "traderjoe_v2"
    assert config.token0 == "WAVAX"
    assert config.token1 == "USDC"
    assert config.chain == "avalanche"
    assert config.fee_tier == "Bin Step 20"


def test_render_custom_dashboard_uses_lp_template():
    from unittest.mock import patch

    strategy_id = "avax_usdc_degen_lp"
    strategy_config = {
        "pool": "WAVAX/USDC/20",
        "base_token": "WAVAX",
        "quote_token": "USDC",
        "chain": "avalanche",
    }
    session_state = {"is_active": True}

    with patch("dashboard.ui.render_lp_dashboard") as render_template:
        render_custom_dashboard(
            strategy_id=strategy_id,
            strategy_config=strategy_config,
            api_client=None,
            session_state=session_state,
        )

    render_template.assert_called_once()
    call_args = render_template.call_args.args

    assert call_args[0] == strategy_id
    assert call_args[1] == strategy_config
    assert call_args[2] == session_state
    assert isinstance(call_args[3], LPDashboardConfig)
    assert call_args[3].protocol == "traderjoe_v2"
