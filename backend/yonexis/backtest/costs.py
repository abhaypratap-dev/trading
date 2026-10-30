"""Indian equity transaction cost model (NSE).

Defaults approximate a discount broker. Rates change, so every rate is a field
you can override from config or the admin UI. Verify against your broker's
current contract note before trusting absolute P&L.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class CostModel:
    product: str = "intraday"            # "intraday", "delivery" or "futures"
    brokerage_pct: float = 0.03          # % of order value
    brokerage_cap: float = 20.0          # Rs per executed order
    stt_intraday_sell_pct: float = 0.025
    stt_delivery_pct: float = 0.1        # both sides
    exchange_txn_pct: float = 0.00297    # NSE equity
    sebi_fee_per_crore: float = 10.0
    stamp_intraday_buy_pct: float = 0.003
    stamp_delivery_buy_pct: float = 0.015
    stt_futures_sell_pct: float = 0.05   # raised from 0.02% by Budget 2026
    exchange_txn_futures_pct: float = 0.00173   # NSE futures
    stamp_futures_buy_pct: float = 0.002
    gst_pct: float = 18.0                # on brokerage + exchange + SEBI fee
    multiplier: float = 1.0              # stress tests scale all costs

    def order_cost(self, value: float, is_buy: bool) -> float:
        brokerage = min(value * self.brokerage_pct / 100, self.brokerage_cap)
        if self.product == "futures":
            stt = 0.0 if is_buy else value * self.stt_futures_sell_pct / 100
            stamp = value * self.stamp_futures_buy_pct / 100 if is_buy else 0.0
            exch = value * self.exchange_txn_futures_pct / 100
            sebi = value * self.sebi_fee_per_crore / 1e7
            gst = (brokerage + exch + sebi) * self.gst_pct / 100
            return (brokerage + stt + exch + sebi + stamp + gst) * self.multiplier
        if self.product == "delivery":
            stt = value * self.stt_delivery_pct / 100
            stamp = value * self.stamp_delivery_buy_pct / 100 if is_buy else 0.0
        else:
            stt = 0.0 if is_buy else value * self.stt_intraday_sell_pct / 100
            stamp = value * self.stamp_intraday_buy_pct / 100 if is_buy else 0.0
        exch = value * self.exchange_txn_pct / 100
        sebi = value * self.sebi_fee_per_crore / 1e7
        gst = (brokerage + exch + sebi) * self.gst_pct / 100
        return (brokerage + stt + exch + sebi + stamp + gst) * self.multiplier

    def round_trip(self, buy_value: float, sell_value: float) -> float:
        return self.order_cost(buy_value, True) + self.order_cost(sell_value, False)

    def for_futures(self) -> "CostModel":
        from dataclasses import replace
        return replace(self, product="futures")

    def to_dict(self) -> dict:
        return asdict(self)
