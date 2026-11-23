"""Unit tests for order payload builders, OCC option symbols, and RiskGuard policies."""

from __future__ import annotations

from pathlib import Path

import pytest

from schwab_mcp.config import Settings
from schwab_mcp.risk.guard import RiskGuard
from schwab_mcp.schwab.orders import (
    build_bracket_order,
    build_equity_order,
    build_exit_oco_order,
    build_occ_symbol,
    build_option_order,
    build_option_spread_order,
    build_trailing_stop_order,
    build_trigger_order,
    extract_underlying_symbol,
)


def test_occ_symbol_builder_and_underlying_extractor() -> None:
    occ = build_occ_symbol("AAPL", "2026-06-18", "CALL", 200.0)
    assert occ == "AAPL  260618C00200000"
    assert len(occ) == 21
    assert extract_underlying_symbol(occ, "OPTION") == "AAPL"

    occ_half = build_occ_symbol("spy", "261218", "p", 582.5)
    assert occ_half == "SPY   261218P00582500"
    assert len(occ_half) == 21


def test_equity_order_builder_validation() -> None:
    order = build_equity_order(
        symbol="nvda",
        instruction="buy",
        quantity=10,
        order_type="LIMIT",
        price=125.5,
        duration="DAY",
        session="PM",
    )
    assert order["orderType"] == "LIMIT"
    assert order["price"] == "125.50"
    assert order["session"] == "PM"
    assert order["orderLegCollection"][0]["instrument"] == {
        "symbol": "NVDA",
        "assetType": "EQUITY",
    }

    with pytest.raises(ValueError, match="Extended-hours"):
        build_equity_order(
            symbol="NVDA",
            instruction="BUY",
            quantity=1,
            order_type="MARKET",
            session="AM",
        )


def test_option_and_spread_order_builders() -> None:
    single = build_option_order(
        instruction="BUY_TO_OPEN",
        quantity=2,
        underlying="AAPL",
        expiration="2026-06-18",
        contract_type="CALL",
        strike=210,
        order_type="LIMIT",
        price=3.25,
    )
    assert single["orderLegCollection"][0]["instrument"]["symbol"] == "AAPL  260618C00210000"
    assert single["price"] == "3.25"

    spread = build_option_spread_order(
        legs=[
            {
                "symbol": "AAPL  260618C00200000",
                "instruction": "BUY_TO_OPEN",
                "quantity": 1,
            },
            {
                "symbol": "AAPL  260618C00210000",
                "instruction": "SELL_TO_OPEN",
                "quantity": 1,
            },
        ],
        order_type="NET_DEBIT",
        price=4.10,
        complex_order_strategy_type="VERTICAL",
    )
    assert spread["orderType"] == "NET_DEBIT"
    assert spread["complexOrderStrategyType"] == "VERTICAL"
    assert len(spread["orderLegCollection"]) == 2


async def test_risk_guard_enforces_dry_run_limits_and_symbols(tmp_path: Path) -> None:
    dry_settings = Settings(
        schwab_dry_run=True,
        schwab_read_only=False,
        schwab_max_order_usd=1000.0,
        schwab_blocked_symbols="GME",
        schwab_audit_log_path=tmp_path / "audit.jsonl",
    )
    guard = RiskGuard(dry_settings)

    order = build_equity_order(
        symbol="AAPL", instruction="BUY", quantity=2, order_type="LIMIT", price=200.0
    )
    ev = await guard.evaluate_order(order, request_dry_run=False, confirm_live=True)
    assert ev.allowed is True
    assert ev.effective_dry_run is True
    assert ev.estimated_notional_usd == 400.0

    big_order = build_equity_order(
        symbol="AAPL", instruction="BUY", quantity=10, order_type="LIMIT", price=200.0
    )
    ev_big = await guard.evaluate_order(big_order, request_dry_run=True)
    assert ev_big.allowed is False
    assert "exceeds SCHWAB_MAX_ORDER_USD" in (ev_big.reason or "")

    blocked_opt = build_option_order(
        instruction="BUY_TO_OPEN",
        quantity=1,
        underlying="GME",
        expiration="2026-06-18",
        contract_type="CALL",
        strike=20.0,
        order_type="LIMIT",
        price=1.0,
    )
    ev_blocked = await guard.evaluate_order(blocked_opt)
    assert ev_blocked.allowed is False
    assert "blocked" in (ev_blocked.reason or "").lower()

    live_settings = Settings(
        schwab_dry_run=False,
        schwab_read_only=False,
        schwab_max_order_usd=1000.0,
        schwab_audit_log_path=tmp_path / "audit.jsonl",
    )
    live_guard = RiskGuard(live_settings)
    ev_no_confirm = await live_guard.evaluate_order(order, confirm_live=False)
    assert ev_no_confirm.allowed is False
    assert "confirm_live=True" in (ev_no_confirm.reason or "")

    ev_confirm = await live_guard.evaluate_order(order, confirm_live=True)
    assert ev_confirm.allowed is True
    assert ev_confirm.effective_dry_run is False

    mkt = build_equity_order(symbol="AAPL", instruction="BUY", quantity=5, order_type="MARKET")

    async def fake_quote(_sym: str) -> float:
        return 250.0

    ev_mkt = await live_guard.evaluate_order(mkt, confirm_live=True, quote_lookup=fake_quote)
    assert ev_mkt.allowed is False
    assert ev_mkt.estimated_notional_usd == 1250.0


async def test_advanced_order_builders_and_composite_risk_sizing(tmp_path: Path) -> None:
    moc = build_equity_order(
        symbol="SPY", instruction="SELL", quantity=2, order_type="MARKET_ON_CLOSE"
    )
    assert moc["orderType"] == "MARKET_ON_CLOSE"
    loc = build_equity_order(
        symbol="SPY", instruction="BUY", quantity=2, order_type="LIMIT_ON_CLOSE", price=600.0
    )
    assert loc["orderType"] == "LIMIT_ON_CLOSE"
    assert loc["price"] == "600.00"

    ts = build_trailing_stop_order(
        symbol="AAPL",
        instruction="SELL",
        quantity=2,
        stop_price_offset=5.0,
        stop_price_link_type="PERCENT",
        stop_price_link_basis="BID",
    )
    assert ts["orderType"] == "TRAILING_STOP"
    assert ts["stopPriceOffset"] == 5.0

    tsl = build_trailing_stop_order(
        symbol="AAPL",
        instruction="SELL",
        quantity=2,
        order_type="TRAILING_STOP_LIMIT",
        stop_price_offset=2.0,
        stop_price_link_type="VALUE",
        price_offset=0.25,
    )
    assert tsl["orderType"] == "TRAILING_STOP_LIMIT"
    assert tsl["priceOffset"] == 0.25

    oco = build_exit_oco_order(
        symbol="AAPL",
        quantity=2,
        take_profit_price=360.0,
        trailing_stop_offset=4.0,
    )
    assert oco["orderStrategyType"] == "OCO"
    assert len(oco["childOrderStrategies"]) == 2

    bracket = build_bracket_order(
        symbol="AAPL",
        quantity=2,
        entry_order_type="LIMIT",
        entry_price=300.0,
        take_profit_price=330.0,
        stop_loss_price=285.0,
    )
    assert bracket["orderStrategyType"] == "TRIGGER"
    assert bracket["childOrderStrategies"][0]["orderStrategyType"] == "OCO"

    with pytest.raises(ValueError, match="take_profit_price"):
        build_bracket_order(
            symbol="AAPL",
            quantity=1,
            entry_price=300.0,
            take_profit_price=290.0,
            stop_loss_price=280.0,
        )

    settings = Settings(
        schwab_dry_run=False,
        schwab_read_only=False,
        schwab_max_order_usd=1000.0,
        schwab_audit_log_path=tmp_path / "audit.jsonl",
    )
    guard = RiskGuard(settings)
    ev_bracket = await guard.evaluate_order(bracket, confirm_live=True)
    assert ev_bracket.allowed is True
    assert ev_bracket.estimated_notional_usd == 600.0

    oco_fixed = build_exit_oco_order(
        symbol="AAPL",
        quantity=2,
        take_profit_price=330.0,
        stop_loss_price=285.0,
    )
    ev_oco = await guard.evaluate_order(oco_fixed, confirm_live=True)
    assert ev_oco.allowed is True
    assert ev_oco.estimated_notional_usd == 660.0

    primary = build_equity_order(
        symbol="AAPL", instruction="BUY", quantity=1, order_type="LIMIT", price=300.0
    )
    trig = build_trigger_order(primary, [ts])
    assert primary["orderStrategyType"] == "SINGLE"
    assert trig["orderStrategyType"] == "TRIGGER"
