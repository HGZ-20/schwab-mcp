"""Charles Schwab Trader API v1 order payload builders and OCC option symbol helpers."""

from __future__ import annotations

import copy
import re
from datetime import datetime
from typing import Any

EQUITY_INSTRUCTIONS = {"BUY", "SELL", "SELL_SHORT", "BUY_TO_COVER"}
OPTION_INSTRUCTIONS = {
    "BUY_TO_OPEN",
    "BUY_TO_CLOSE",
    "SELL_TO_OPEN",
    "SELL_TO_CLOSE",
}
SINGLE_ORDER_TYPES = {"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}
EQUITY_ORDER_TYPES = SINGLE_ORDER_TYPES | {"MARKET_ON_CLOSE", "LIMIT_ON_CLOSE"}
TRAILING_ORDER_TYPES = {"TRAILING_STOP", "TRAILING_STOP_LIMIT"}
SPREAD_ORDER_TYPES = {"NET_DEBIT", "NET_CREDIT", "NET_ZERO", "MARKET"}
VALID_DURATIONS = {"DAY", "GOOD_TILL_CANCEL", "FILL_OR_KILL", "IMMEDIATE_OR_CANCEL"}
VALID_SESSIONS = {"NORMAL", "AM", "PM", "SEAMLESS"}
VALID_LINK_BASIS = {
    "MANUAL",
    "BASE",
    "TRIGGER",
    "LAST",
    "BID",
    "ASK",
    "ASK_BID",
    "MARK",
    "AVERAGE",
}
VALID_LINK_TYPES = {"VALUE", "PERCENT", "TICK"}
VALID_COMPLEX_STRATEGIES = {
    "NONE",
    "COVERED",
    "VERTICAL",
    "BACK_RATIO",
    "CALENDAR",
    "DIAGONAL",
    "STRADDLE",
    "STRANGLE",
    "COLLAR_SYNTHETIC",
    "BUTTERFLY",
    "CONDOR",
    "IRON_CONDOR",
    "VERTICAL_ROLL",
    "COLLAR_WITH_STOCK",
    "DOUBLE_DIAGONAL",
    "UNBALANCED_BUTTERFLY",
    "UNBALANCED_CONDOR",
    "UNBALANCED_IRON_CONDOR",
    "UNBALANCED_VERTICAL_ROLL",
    "CUSTOM",
}

ENTRY_TO_EXIT_INSTRUCTION = {
    "BUY": "SELL",
    "SELL_SHORT": "BUY_TO_COVER",
    "BUY_TO_OPEN": "SELL_TO_CLOSE",
    "SELL_TO_OPEN": "BUY_TO_CLOSE",
}

OCC_REGEX = re.compile(r"^([A-Z0-9 .]{6})(\d{6})([CP])(\d{8})$")


def format_price(value: float) -> str:
    """Format order price as string with Schwab-compatible decimal precision."""
    if value <= 0:
        raise ValueError(f"Price must be positive, got {value}.")
    decimals = 4 if value < 1.0 else 2
    formatted = f"{value:.{decimals}f}"
    if decimals == 4:
        formatted = formatted.rstrip("0")
        if len(formatted.split(".")[1]) < 2:
            formatted = f"{value:.2f}"
    return formatted


def build_occ_symbol(
    underlying: str,
    expiration: str,
    contract_type: str,
    strike: float,
) -> str:
    """Build a standard 21-character OCC option symbol used by Charles Schwab."""
    root = underlying.strip().upper()
    if not root or len(root) > 6:
        raise ValueError(f"Invalid underlying symbol for OCC option: {underlying!r}")

    exp_clean = expiration.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp_clean):
        dt = datetime.strptime(exp_clean, "%Y-%m-%d")
        exp_yymmdd = dt.strftime("%y%m%d")
    elif re.fullmatch(r"\d{6}", exp_clean):
        datetime.strptime(exp_clean, "%y%m%d")
        exp_yymmdd = exp_clean
    else:
        raise ValueError(f"Expiration must be YYYY-MM-DD or YYMMDD, got {expiration!r}.")

    ct = contract_type.strip().upper()
    if ct in {"C", "CALL"}:
        cp = "C"
    elif ct in {"P", "PUT"}:
        cp = "P"
    else:
        raise ValueError(f"contract_type must be CALL/C or PUT/P, got {contract_type!r}.")

    if strike <= 0:
        raise ValueError(f"Strike price must be positive, got {strike}.")

    strike_int = round(strike * 1000)
    return f"{root:<6}{exp_yymmdd}{cp}{strike_int:08d}"


def validate_occ_symbol(option_symbol: str) -> str:
    """Validate and normalize a 21-character OCC option symbol."""
    normalized = option_symbol.upper()
    if not OCC_REGEX.match(normalized):
        raise ValueError(
            f"Invalid 21-char OCC option symbol: {option_symbol!r}. "
            "Expected format like 'AAPL  260618C00200000' (use build_option_symbol helper)."
        )
    return normalized


def extract_underlying_symbol(symbol: str, asset_type: str = "EQUITY") -> str:
    """Extract the underlying ticker symbol from an equity or OCC option symbol."""
    upper = symbol.upper()
    if asset_type.upper() == "OPTION" or OCC_REGEX.match(upper):
        return upper[:6].strip()
    return upper.strip()


def _resolve_instrument(
    *,
    asset_type: str,
    symbol: str | None = None,
    option_symbol: str | None = None,
    underlying: str | None = None,
    expiration: str | None = None,
    contract_type: str | None = None,
    strike: float | None = None,
) -> tuple[str, str]:
    atype = asset_type.strip().upper()
    if atype == "EQUITY":
        sym = (symbol or "").strip().upper()
        if not sym:
            raise ValueError("Equity symbol cannot be empty.")
        return sym, "EQUITY"
    if atype == "OPTION":
        raw_opt = option_symbol or symbol
        if raw_opt:
            return validate_occ_symbol(raw_opt), "OPTION"
        if not (underlying and expiration and contract_type and strike is not None):
            raise ValueError(
                "For OPTION orders, provide either 'option_symbol'/'symbol' OR all of "
                "('underlying', 'expiration', 'contract_type', 'strike')."
            )
        return build_occ_symbol(underlying, expiration, contract_type, strike), "OPTION"
    raise ValueError(f"Unsupported asset_type {asset_type!r}. Allowed: ['EQUITY', 'OPTION'].")


def _validate_instruction(instruction: str, asset_type: str) -> str:
    inst = instruction.strip().upper()
    valid = OPTION_INSTRUCTIONS if asset_type == "OPTION" else EQUITY_INSTRUCTIONS
    if inst not in valid:
        raise ValueError(
            f"Invalid {asset_type} instruction {instruction!r}. Allowed: {sorted(valid)}"
        )
    return inst


def _validate_session_and_duration(order_type: str, session: str, duration: str) -> tuple[str, str]:
    sess = session.strip().upper()
    dur = duration.strip().upper()
    if sess not in VALID_SESSIONS:
        raise ValueError(f"Invalid session {session!r}. Allowed: {sorted(VALID_SESSIONS)}")
    if dur not in VALID_DURATIONS:
        raise ValueError(f"Invalid duration {duration!r}. Allowed: {sorted(VALID_DURATIONS)}")
    if sess in {"AM", "PM", "SEAMLESS"} and order_type != "LIMIT":
        raise ValueError(f"Extended-hours session {sess!r} only supports LIMIT orders on Schwab.")
    if sess in {"AM", "PM", "SEAMLESS"} and dur != "DAY":
        raise ValueError(f"Extended-hours session {sess!r} requires duration='DAY' on Schwab.")
    if order_type in {"MARKET_ON_CLOSE", "LIMIT_ON_CLOSE"}:
        if sess != "NORMAL":
            raise ValueError(f"{order_type} requires session='NORMAL'.")
        if dur != "DAY":
            raise ValueError(f"{order_type} requires duration='DAY'.")
    return sess, dur


def _apply_order_pricing(
    payload: dict[str, Any],
    order_type: str,
    price: float | None,
    stop_price: float | None,
) -> None:
    if order_type in {"MARKET", "MARKET_ON_CLOSE"}:
        if price is not None or stop_price is not None:
            raise ValueError(f"{order_type} orders must not specify price or stop_price.")
    elif order_type in {"LIMIT", "LIMIT_ON_CLOSE"}:
        if price is None:
            raise ValueError(f"{order_type} orders require 'price'.")
        if stop_price is not None:
            raise ValueError(f"{order_type} orders must not specify 'stop_price'.")
        payload["price"] = format_price(price)
    elif order_type == "STOP":
        if stop_price is None:
            raise ValueError("STOP orders require 'stop_price'.")
        if price is not None:
            raise ValueError("STOP orders must not specify 'price'.")
        payload["stopPrice"] = format_price(stop_price)
    elif order_type == "STOP_LIMIT":
        if price is None or stop_price is None:
            raise ValueError("STOP_LIMIT orders require both 'price' and 'stop_price'.")
        payload["price"] = format_price(price)
        payload["stopPrice"] = format_price(stop_price)


def build_equity_order(
    *,
    symbol: str,
    instruction: str,
    quantity: float,
    order_type: str = "LIMIT",
    price: float | None = None,
    stop_price: float | None = None,
    duration: str = "DAY",
    session: str = "NORMAL",
) -> dict[str, Any]:
    """Build a validated Schwab single-leg equity/ETF order JSON payload."""
    sym, atype = _resolve_instrument(asset_type="EQUITY", symbol=symbol)
    inst = _validate_instruction(instruction, atype)

    if quantity <= 0:
        raise ValueError(f"Quantity must be positive, got {quantity}.")

    otype = order_type.strip().upper()
    if otype not in EQUITY_ORDER_TYPES:
        raise ValueError(
            f"Invalid order_type {order_type!r}. Allowed: {sorted(EQUITY_ORDER_TYPES)}"
        )

    sess, dur = _validate_session_and_duration(otype, session, duration)

    payload: dict[str, Any] = {
        "orderType": otype,
        "session": sess,
        "duration": dur,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": inst,
                "quantity": quantity,
                "instrument": {
                    "symbol": sym,
                    "assetType": "EQUITY",
                },
            }
        ],
    }
    _apply_order_pricing(payload, otype, price, stop_price)
    return payload


def build_option_order(
    *,
    instruction: str,
    quantity: int,
    option_symbol: str | None = None,
    underlying: str | None = None,
    expiration: str | None = None,
    contract_type: str | None = None,
    strike: float | None = None,
    order_type: str = "LIMIT",
    price: float | None = None,
    stop_price: float | None = None,
    duration: str = "DAY",
    session: str = "NORMAL",
) -> dict[str, Any]:
    """Build a validated Schwab single-leg option order JSON payload."""
    occ, atype = _resolve_instrument(
        asset_type="OPTION",
        option_symbol=option_symbol,
        underlying=underlying,
        expiration=expiration,
        contract_type=contract_type,
        strike=strike,
    )
    inst = _validate_instruction(instruction, atype)

    if not isinstance(quantity, int) or quantity <= 0:
        raise ValueError(f"Option contract quantity must be a positive integer, got {quantity}.")

    otype = order_type.strip().upper()
    if otype not in SINGLE_ORDER_TYPES:
        raise ValueError(
            f"Invalid order_type {order_type!r}. Allowed: {sorted(SINGLE_ORDER_TYPES)}"
        )

    sess, dur = _validate_session_and_duration(otype, session, duration)
    if sess != "NORMAL":
        raise ValueError("Options orders on Schwab only support session='NORMAL'.")

    payload: dict[str, Any] = {
        "orderType": otype,
        "session": sess,
        "duration": dur,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": inst,
                "quantity": quantity,
                "instrument": {
                    "symbol": occ,
                    "assetType": "OPTION",
                },
            }
        ],
    }
    _apply_order_pricing(payload, otype, price, stop_price)
    return payload


def build_trailing_stop_order(
    *,
    instruction: str,
    quantity: float,
    stop_price_offset: float,
    symbol: str | None = None,
    asset_type: str = "EQUITY",
    option_symbol: str | None = None,
    underlying: str | None = None,
    expiration: str | None = None,
    contract_type: str | None = None,
    strike: float | None = None,
    order_type: str = "TRAILING_STOP",
    stop_price_link_type: str = "PERCENT",
    stop_price_link_basis: str = "BID",
    price_offset: float | None = None,
    price_link_type: str = "VALUE",
    price_link_basis: str | None = None,
    duration: str = "GOOD_TILL_CANCEL",
    session: str = "NORMAL",
) -> dict[str, Any]:
    """Build a Schwab TRAILING_STOP or TRAILING_STOP_LIMIT order for equity or option."""
    sym, atype = _resolve_instrument(
        asset_type=asset_type,
        symbol=symbol,
        option_symbol=option_symbol,
        underlying=underlying,
        expiration=expiration,
        contract_type=contract_type,
        strike=strike,
    )
    inst = _validate_instruction(instruction, atype)

    if atype == "OPTION" and (not isinstance(quantity, int) or quantity <= 0):
        raise ValueError(f"Option contract quantity must be a positive integer, got {quantity}.")
    if quantity <= 0:
        raise ValueError(f"Quantity must be positive, got {quantity}.")

    otype = order_type.strip().upper()
    if otype not in TRAILING_ORDER_TYPES:
        raise ValueError(
            f"Invalid trailing order_type {order_type!r}. Allowed: {sorted(TRAILING_ORDER_TYPES)}"
        )

    link_type = stop_price_link_type.strip().upper()
    if link_type not in VALID_LINK_TYPES:
        raise ValueError(
            f"Invalid stop_price_link_type {stop_price_link_type!r}. Allowed: {sorted(VALID_LINK_TYPES)}"
        )

    link_basis = stop_price_link_basis.strip().upper()
    if link_basis not in VALID_LINK_BASIS:
        raise ValueError(
            f"Invalid stop_price_link_basis {stop_price_link_basis!r}. Allowed: {sorted(VALID_LINK_BASIS)}"
        )

    if stop_price_offset <= 0:
        raise ValueError(f"stop_price_offset must be positive, got {stop_price_offset}.")
    if link_type == "PERCENT" and stop_price_offset >= 100:
        raise ValueError(f"Percentage stop_price_offset must be < 100, got {stop_price_offset}.")

    sess = session.strip().upper()
    dur = duration.strip().upper()
    if sess != "NORMAL":
        raise ValueError("Trailing stop orders on Schwab only support session='NORMAL'.")
    if dur not in {"DAY", "GOOD_TILL_CANCEL"}:
        raise ValueError("Trailing stop orders support duration='DAY' or 'GOOD_TILL_CANCEL'.")

    payload: dict[str, Any] = {
        "orderType": otype,
        "session": sess,
        "duration": dur,
        "complexOrderStrategyType": "NONE",
        "stopPriceLinkBasis": link_basis,
        "stopPriceLinkType": link_type,
        "stopPriceOffset": round(float(stop_price_offset), 4),
        "stopType": "STANDARD",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": inst,
                "quantity": quantity,
                "instrument": {
                    "symbol": sym,
                    "assetType": atype,
                },
            }
        ],
    }

    if otype == "TRAILING_STOP_LIMIT":
        if price_offset is None:
            raise ValueError(
                "TRAILING_STOP_LIMIT requires 'price_offset' (limit offset relative to price_link_basis, e.g. 0.25)."
            )
        p_link_type = price_link_type.strip().upper()
        if p_link_type not in VALID_LINK_TYPES:
            raise ValueError(
                f"Invalid price_link_type {price_link_type!r}. Allowed: {sorted(VALID_LINK_TYPES)}"
            )
        p_link_basis = (price_link_basis or link_basis).strip().upper()
        if p_link_basis not in VALID_LINK_BASIS:
            raise ValueError(
                f"Invalid price_link_basis {price_link_basis!r}. Allowed: {sorted(VALID_LINK_BASIS)}"
            )
        payload["priceLinkBasis"] = p_link_basis
        payload["priceLinkType"] = p_link_type
        payload["priceOffset"] = round(float(price_offset), 4)
    elif price_offset is not None:
        raise ValueError("price_offset is only valid for TRAILING_STOP_LIMIT orders.")

    return payload


def build_option_spread_order(
    *,
    legs: list[dict[str, Any]],
    order_type: str = "NET_DEBIT",
    price: float | None = None,
    duration: str = "DAY",
    session: str = "NORMAL",
    complex_order_strategy_type: str | None = None,
) -> dict[str, Any]:
    """Build a validated Schwab multi-leg option (or covered stock+option) order payload."""
    if not legs or len(legs) < 2 or len(legs) > 4:
        raise ValueError("Multi-leg spread orders require between 2 and 4 legs.")

    otype = order_type.strip().upper()
    if otype not in SPREAD_ORDER_TYPES:
        raise ValueError(
            f"Invalid spread order_type {order_type!r}. Allowed: {sorted(SPREAD_ORDER_TYPES)}"
        )

    sess = session.strip().upper()
    dur = duration.strip().upper()
    if sess != "NORMAL":
        raise ValueError("Multi-leg option orders only support session='NORMAL'.")
    if dur not in VALID_DURATIONS:
        raise ValueError(f"Invalid duration {duration!r}. Allowed: {sorted(VALID_DURATIONS)}")

    if otype in {"NET_DEBIT", "NET_CREDIT"}:
        if price is None:
            raise ValueError(f"Spread order_type {otype!r} requires 'price'.")
    elif price is not None:
        raise ValueError(f"Spread order_type {otype!r} must not specify 'price'.")

    leg_collection: list[dict[str, Any]] = []
    for idx, leg in enumerate(legs, start=1):
        asset_type = str(leg.get("asset_type", "OPTION")).strip().upper()
        if asset_type not in {"OPTION", "EQUITY"}:
            raise ValueError(f"Leg {idx}: unsupported asset_type {asset_type!r}.")

        inst = _validate_instruction(str(leg.get("instruction", "")), asset_type)

        qty = leg.get("quantity")
        if qty is None or qty <= 0:
            raise ValueError(f"Leg {idx}: quantity must be positive, got {qty!r}.")

        raw_symbol = str(leg.get("symbol", ""))
        if asset_type == "OPTION":
            sym = validate_occ_symbol(raw_symbol)
        else:
            sym = raw_symbol.strip().upper()
            if not sym:
                raise ValueError(f"Leg {idx}: equity symbol cannot be empty.")

        leg_collection.append(
            {
                "instruction": inst,
                "quantity": qty,
                "instrument": {
                    "symbol": sym,
                    "assetType": asset_type,
                },
            }
        )

    payload: dict[str, Any] = {
        "orderType": otype,
        "session": sess,
        "duration": dur,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": leg_collection,
    }
    if complex_order_strategy_type:
        cstrategy = complex_order_strategy_type.strip().upper()
        if cstrategy not in VALID_COMPLEX_STRATEGIES:
            raise ValueError(
                f"Invalid complex_order_strategy_type {complex_order_strategy_type!r}."
            )
        payload["complexOrderStrategyType"] = cstrategy

    if price is not None:
        payload["price"] = format_price(price)

    return payload


def build_oco_order(child_orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap two standalone orders into a Schwab One-Cancels-Other (OCO) strategy."""
    if len(child_orders) != 2:
        raise ValueError(f"OCO order requires exactly 2 child orders, got {len(child_orders)}.")
    return {
        "orderStrategyType": "OCO",
        "childOrderStrategies": [copy.deepcopy(c) for c in child_orders],
    }


def build_trigger_order(
    primary_order: dict[str, Any],
    triggered_orders: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a Schwab 1st-Triggers-2nd (TRIGGER) conditional order."""
    if not triggered_orders:
        raise ValueError("TRIGGER order requires at least one triggered child order.")
    parent = copy.deepcopy(primary_order)
    parent["orderStrategyType"] = "TRIGGER"
    parent["childOrderStrategies"] = [copy.deepcopy(c) for c in triggered_orders]
    return parent


def _build_stop_exit_leg(
    *,
    sym: str,
    atype: str,
    exit_instruction: str,
    quantity: float,
    stop_loss_price: float | None,
    stop_limit_price: float | None,
    trailing_stop_offset: float | None,
    trailing_stop_link_type: str,
    trailing_stop_link_basis: str,
    duration: str,
    session: str,
) -> dict[str, Any]:
    has_fixed_stop = stop_loss_price is not None
    has_trailing_stop = trailing_stop_offset is not None
    if has_fixed_stop == has_trailing_stop:
        raise ValueError(
            "Provide EITHER 'stop_loss_price' (fixed STOP/STOP_LIMIT) OR "
            "'trailing_stop_offset' (TRAILING_STOP), not both or neither."
        )

    if has_trailing_stop:
        assert trailing_stop_offset is not None
        return build_trailing_stop_order(
            symbol=sym if atype == "EQUITY" else None,
            option_symbol=sym if atype == "OPTION" else None,
            asset_type=atype,
            instruction=exit_instruction,
            quantity=int(quantity) if atype == "OPTION" else quantity,
            stop_price_offset=trailing_stop_offset,
            stop_price_link_type=trailing_stop_link_type,
            stop_price_link_basis=trailing_stop_link_basis,
            duration=duration,
            session=session,
        )

    assert stop_loss_price is not None
    stop_otype = "STOP_LIMIT" if stop_limit_price is not None else "STOP"
    if atype == "OPTION":
        return build_option_order(
            option_symbol=sym,
            instruction=exit_instruction,
            quantity=int(quantity),
            order_type=stop_otype,
            price=stop_limit_price,
            stop_price=stop_loss_price,
            duration=duration,
            session=session,
        )
    return build_equity_order(
        symbol=sym,
        instruction=exit_instruction,
        quantity=quantity,
        order_type=stop_otype,
        price=stop_limit_price,
        stop_price=stop_loss_price,
        duration=duration,
        session=session,
    )


def build_exit_oco_order(
    *,
    symbol: str,
    quantity: float,
    take_profit_price: float,
    asset_type: str = "EQUITY",
    exit_instruction: str | None = None,
    stop_loss_price: float | None = None,
    stop_limit_price: float | None = None,
    trailing_stop_offset: float | None = None,
    trailing_stop_link_type: str = "PERCENT",
    trailing_stop_link_basis: str = "BID",
    duration: str = "GOOD_TILL_CANCEL",
    session: str = "NORMAL",
) -> dict[str, Any]:
    """Build an OCO Take-Profit + Stop-Loss (or Trailing Stop) exit pair for an existing position."""
    sym, atype = _resolve_instrument(
        asset_type=asset_type,
        symbol=symbol if asset_type.upper() == "EQUITY" else None,
        option_symbol=symbol if asset_type.upper() == "OPTION" else None,
    )
    default_exit = "SELL_TO_CLOSE" if atype == "OPTION" else "SELL"
    exit_inst = _validate_instruction(exit_instruction or default_exit, atype)

    if stop_loss_price is not None:
        is_closing_long = exit_inst in {"SELL", "SELL_TO_CLOSE"}
        if is_closing_long and take_profit_price <= stop_loss_price:
            raise ValueError(
                f"For closing a long ({exit_inst}), take_profit_price ({take_profit_price}) "
                f"must be greater than stop_loss_price ({stop_loss_price})."
            )
        if not is_closing_long and take_profit_price >= stop_loss_price:
            raise ValueError(
                f"For closing a short ({exit_inst}), take_profit_price ({take_profit_price}) "
                f"must be less than stop_loss_price ({stop_loss_price})."
            )

    if atype == "OPTION":
        tp_leg = build_option_order(
            option_symbol=sym,
            instruction=exit_inst,
            quantity=int(quantity),
            order_type="LIMIT",
            price=take_profit_price,
            duration=duration,
            session=session,
        )
    else:
        tp_leg = build_equity_order(
            symbol=sym,
            instruction=exit_inst,
            quantity=quantity,
            order_type="LIMIT",
            price=take_profit_price,
            duration=duration,
            session=session,
        )

    sl_leg = _build_stop_exit_leg(
        sym=sym,
        atype=atype,
        exit_instruction=exit_inst,
        quantity=quantity,
        stop_loss_price=stop_loss_price,
        stop_limit_price=stop_limit_price,
        trailing_stop_offset=trailing_stop_offset,
        trailing_stop_link_type=trailing_stop_link_type,
        trailing_stop_link_basis=trailing_stop_link_basis,
        duration=duration,
        session=session,
    )
    return build_oco_order([tp_leg, sl_leg])


def build_bracket_order(
    *,
    symbol: str,
    quantity: float,
    take_profit_price: float,
    asset_type: str = "EQUITY",
    entry_instruction: str | None = None,
    entry_order_type: str = "LIMIT",
    entry_price: float | None = None,
    entry_stop_price: float | None = None,
    stop_loss_price: float | None = None,
    stop_limit_price: float | None = None,
    trailing_stop_offset: float | None = None,
    trailing_stop_link_type: str = "PERCENT",
    trailing_stop_link_basis: str = "BID",
    entry_duration: str = "DAY",
    entry_session: str = "NORMAL",
    exit_duration: str = "GOOD_TILL_CANCEL",
) -> dict[str, Any]:
    """Build a Bracket order (TRIGGER entry -> OCO Take-Profit + Stop-Loss/Trailing Stop)."""
    sym, atype = _resolve_instrument(
        asset_type=asset_type,
        symbol=symbol if asset_type.upper() == "EQUITY" else None,
        option_symbol=symbol if asset_type.upper() == "OPTION" else None,
    )
    default_entry = "BUY_TO_OPEN" if atype == "OPTION" else "BUY"
    entry_inst = _validate_instruction(entry_instruction or default_entry, atype)
    exit_inst = ENTRY_TO_EXIT_INSTRUCTION.get(entry_inst)
    if not exit_inst:
        raise ValueError(
            f"entry_instruction {entry_inst!r} is not an opening instruction. "
            f"Allowed: {sorted(ENTRY_TO_EXIT_INSTRUCTION)}"
        )

    ref_entry = entry_price or entry_stop_price
    if ref_entry is not None:
        is_long = entry_inst in {"BUY", "BUY_TO_OPEN"}
        if is_long:
            if take_profit_price <= ref_entry:
                raise ValueError(
                    f"Long bracket take_profit_price ({take_profit_price}) must exceed entry price ({ref_entry})."
                )
            if stop_loss_price is not None and stop_loss_price >= ref_entry:
                raise ValueError(
                    f"Long bracket stop_loss_price ({stop_loss_price}) must be below entry price ({ref_entry})."
                )
        else:
            if take_profit_price >= ref_entry:
                raise ValueError(
                    f"Short bracket take_profit_price ({take_profit_price}) must be below entry price ({ref_entry})."
                )
            if stop_loss_price is not None and stop_loss_price <= ref_entry:
                raise ValueError(
                    f"Short bracket stop_loss_price ({stop_loss_price}) must exceed entry price ({ref_entry})."
                )

    if atype == "OPTION":
        entry_order = build_option_order(
            option_symbol=sym,
            instruction=entry_inst,
            quantity=int(quantity),
            order_type=entry_order_type,
            price=entry_price,
            stop_price=entry_stop_price,
            duration=entry_duration,
            session=entry_session,
        )
    else:
        entry_order = build_equity_order(
            symbol=sym,
            instruction=entry_inst,
            quantity=quantity,
            order_type=entry_order_type,
            price=entry_price,
            stop_price=entry_stop_price,
            duration=entry_duration,
            session=entry_session,
        )

    exit_oco = build_exit_oco_order(
        symbol=sym,
        asset_type=atype,
        quantity=quantity,
        exit_instruction=exit_inst,
        take_profit_price=take_profit_price,
        stop_loss_price=stop_loss_price,
        stop_limit_price=stop_limit_price,
        trailing_stop_offset=trailing_stop_offset,
        trailing_stop_link_type=trailing_stop_link_type,
        trailing_stop_link_basis=trailing_stop_link_basis,
        duration=exit_duration,
        session="NORMAL",
    )
    return build_trigger_order(entry_order, [exit_oco])
