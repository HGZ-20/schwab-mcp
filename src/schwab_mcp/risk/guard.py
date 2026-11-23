"""Trading risk controls, dry-run enforcement, and JSONL audit logging."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import Settings
from ..schwab.orders import extract_underlying_symbol

QuoteLookupFn = Callable[[str], Awaitable[float | None]]


@dataclass
class RiskEvaluation:
    """Result of evaluating a trading action against safety policies."""

    allowed: bool
    effective_dry_run: bool
    estimated_notional_usd: float | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)


def _iter_order_legs(order_payload: dict[str, Any]) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for leg in order_payload.get("orderLegCollection", []):
        legs.append(leg)
    for child in order_payload.get("childOrderStrategies", []):
        if isinstance(child, dict):
            legs.extend(_iter_order_legs(child))
    return legs


class RiskGuard:
    """Enforces read-only, dry-run, symbol restrictions, notional caps, and audit logging."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def audit(self, event: str, details: dict[str, Any]) -> None:
        """Append a structured trading audit event to the JSONL log file."""
        path = self.settings.schwab_audit_log_path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "event": event,
            **details,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    async def evaluate_order(
        self,
        order_payload: dict[str, Any],
        *,
        request_dry_run: bool | None = None,
        confirm_live: bool = False,
        quote_lookup: QuoteLookupFn | None = None,
    ) -> RiskEvaluation:
        """Evaluate an order payload against all configured risk guardrails."""
        warnings: list[str] = []

        # Global SCHWAB_DRY_RUN=true can never be bypassed by an AI tool argument.
        if self.settings.schwab_dry_run:
            effective_dry_run = True
            if request_dry_run is False or confirm_live:
                warnings.append(
                    "Global SCHWAB_DRY_RUN=true is enabled; executing as dry-run/preview only."
                )
        else:
            effective_dry_run = bool(request_dry_run) if request_dry_run is not None else False

        legs = _iter_order_legs(order_payload)
        if not legs:
            return RiskEvaluation(
                allowed=False,
                effective_dry_run=effective_dry_run,
                reason="Order payload contains no orderLegCollection entries.",
            )

        # 1. Symbol whitelist / blacklist checks
        allowed_set = self.settings.allowed_symbol_set
        blocked_set = self.settings.blocked_symbol_set
        for leg in legs:
            inst = leg.get("instrument", {})
            sym = str(inst.get("symbol", ""))
            asset_type = str(inst.get("assetType", "EQUITY")).upper()
            underlying = extract_underlying_symbol(sym, asset_type)

            if underlying in blocked_set or sym.upper() in blocked_set:
                return RiskEvaluation(
                    allowed=False,
                    effective_dry_run=effective_dry_run,
                    reason=f"Symbol '{underlying}' is blocked by SCHWAB_BLOCKED_SYMBOLS.",
                )
            if allowed_set and underlying not in allowed_set and sym.upper() not in allowed_set:
                return RiskEvaluation(
                    allowed=False,
                    effective_dry_run=effective_dry_run,
                    reason=(
                        f"Symbol '{underlying}' is not in SCHWAB_ALLOWED_SYMBOLS "
                        f"({sorted(allowed_set)})."
                    ),
                )

        # 2. Notional value estimation & cap check
        estimated_notional = await self._estimate_notional_usd(
            order_payload, quote_lookup, warnings
        )
        if estimated_notional is None:
            if not effective_dry_run:
                return RiskEvaluation(
                    allowed=False,
                    effective_dry_run=effective_dry_run,
                    reason=(
                        "Unable to estimate order notional value for risk check "
                        "(missing price and live quote unavailable). Refusing live order."
                    ),
                    warnings=warnings,
                )
            warnings.append("Could not estimate notional USD value during dry-run.")
        elif estimated_notional > self.settings.schwab_max_order_usd:
            return RiskEvaluation(
                allowed=False,
                effective_dry_run=effective_dry_run,
                estimated_notional_usd=round(estimated_notional, 2),
                reason=(
                    f"Estimated order notional ${estimated_notional:,.2f} exceeds "
                    f"SCHWAB_MAX_ORDER_USD (${self.settings.schwab_max_order_usd:,.2f})."
                ),
                warnings=warnings,
            )

        # 3. Live execution guards
        if not effective_dry_run:
            if self.settings.schwab_read_only:
                return RiskEvaluation(
                    allowed=False,
                    effective_dry_run=False,
                    estimated_notional_usd=(
                        round(estimated_notional, 2) if estimated_notional is not None else None
                    ),
                    reason="Live trading is disabled because SCHWAB_READ_ONLY=true.",
                    warnings=warnings,
                )
            if not confirm_live:
                return RiskEvaluation(
                    allowed=False,
                    effective_dry_run=False,
                    estimated_notional_usd=(
                        round(estimated_notional, 2) if estimated_notional is not None else None
                    ),
                    reason=(
                        "Live order requires explicit confirm_live=True. "
                        "Preview the order first or set dry_run=True to simulate."
                    ),
                    warnings=warnings,
                )

        return RiskEvaluation(
            allowed=True,
            effective_dry_run=effective_dry_run,
            estimated_notional_usd=(
                round(estimated_notional, 2) if estimated_notional is not None else None
            ),
            warnings=warnings,
        )

    def evaluate_cancel(self, *, confirm_live: bool = False) -> RiskEvaluation:
        """Evaluate an order cancellation request against read-only & dry-run policies."""
        if self.settings.schwab_read_only:
            return RiskEvaluation(
                allowed=False,
                effective_dry_run=self.settings.schwab_dry_run,
                reason="Order cancellation is disabled because SCHWAB_READ_ONLY=true.",
            )
        if self.settings.schwab_dry_run:
            return RiskEvaluation(
                allowed=True,
                effective_dry_run=True,
                warnings=[
                    "Global SCHWAB_DRY_RUN=true is enabled; cancel request will NOT be sent to Schwab."
                ],
            )
        if not confirm_live:
            return RiskEvaluation(
                allowed=False,
                effective_dry_run=False,
                reason="Live order cancellation requires explicit confirm_live=True.",
            )
        return RiskEvaluation(allowed=True, effective_dry_run=False)

    async def _estimate_notional_usd(
        self,
        order_payload: dict[str, Any],
        quote_lookup: QuoteLookupFn | None,
        warnings: list[str],
    ) -> float | None:
        strategy = str(order_payload.get("orderStrategyType", "SINGLE")).upper()
        children = [c for c in order_payload.get("childOrderStrategies", []) if isinstance(c, dict)]
        own_legs = order_payload.get("orderLegCollection", [])

        if strategy == "OCO" and children:
            child_vals: list[float] = []
            for child in children:
                val = await self._estimate_notional_usd(child, quote_lookup, warnings)
                if val is not None:
                    child_vals.append(val)
            return max(child_vals) if child_vals else None

        if strategy == "TRIGGER" and own_legs:
            return await self._estimate_single_order_notional(
                order_payload, own_legs, quote_lookup, warnings
            )

        if not own_legs and children:
            total_children = 0.0
            for child in children:
                val = await self._estimate_notional_usd(child, quote_lookup, warnings)
                if val is None:
                    return None
                total_children += val
            return total_children

        return await self._estimate_single_order_notional(
            order_payload, own_legs, quote_lookup, warnings
        )

    async def _estimate_single_order_notional(
        self,
        order_payload: dict[str, Any],
        legs: list[dict[str, Any]],
        quote_lookup: QuoteLookupFn | None,
        warnings: list[str],
    ) -> float | None:
        order_type = str(order_payload.get("orderType", "")).upper()
        if order_type in {"TRAILING_STOP", "TRAILING_STOP_LIMIT"}:
            explicit_price = None
        else:
            explicit_price_raw = order_payload.get("price") or order_payload.get("stopPrice")
            explicit_price = float(explicit_price_raw) if explicit_price_raw is not None else None

        if len(legs) > 1 and order_type in {"NET_DEBIT", "NET_CREDIT", "NET_ZERO"}:
            max_qty = max(float(leg.get("quantity", 0)) for leg in legs)
            has_option = any(
                str(leg.get("instrument", {}).get("assetType", "")).upper() == "OPTION"
                for leg in legs
            )
            multiplier = 100.0 if has_option else 1.0
            if order_type == "NET_ZERO":
                warnings.append(
                    "NET_ZERO spread has $0 net debit/credit; verify margin requirement via previewOrder."
                )
                return 0.0
            if explicit_price is not None:
                warnings.append(
                    "Multi-leg spread notional is estimated from net debit/credit; "
                    "actual margin risk may differ for credit/naked legs."
                )
                return max_qty * abs(explicit_price) * multiplier

        total = 0.0
        for leg in legs:
            qty = float(leg.get("quantity", 0))
            inst = leg.get("instrument", {})
            sym = str(inst.get("symbol", ""))
            asset_type = str(inst.get("assetType", "EQUITY")).upper()
            multiplier = 100.0 if asset_type == "OPTION" else 1.0

            ref_price: float | None
            if len(legs) == 1 and explicit_price is not None:
                ref_price = explicit_price
            elif quote_lookup is not None and sym:
                ref_price = await quote_lookup(sym)
                if ref_price is None:
                    return None
            else:
                return None

            total += qty * abs(ref_price) * multiplier

        return total
