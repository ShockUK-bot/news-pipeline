"""Broker layer (Phase 4). Alpaca paper first (design D7 confirmation);
IBKR remains the fill-quality upgrade path behind the same protocol.

LLMs never touch this module (baseline rule: LLMs never call the broker API).
Only C4 submits/cancels; A3 reads capital numbers from C4's reconciliation
rows in journal.control, never from here.

Providers:
  AlpacaBroker — httpx against https://paper-api.alpaca.markets (env
                 ALPACA_KEY_ID / ALPACA_SECRET_KEY; PAPER endpoint is
                 hard-coded until real capital is a decision).
  FakeBroker   — programmable fixture broker: scripted fill behaviors per
                 client_order_id or ticker (fill / partial / reject / rest),
                 mutating account + positions state, drift injection for
                 reconciliation tests.

All prices float, all qty int, all timestamps aware UTC.
"""
from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

import httpx

from .clock import utcnow
from .log import get_logger

log = get_logger("broker")


@dataclass
class Account:
    equity: float
    settled_cash: float          # cash-account rule: buying power = settled cash
    currency: str = "USD"
    regt_buying_power: float = 0.0   # (v0.13) margin BP — clips SHORT entries
    multiplier: float = 1.0          # (v0.13) 1 = cash, 2 = Reg-T margin
    shorting_enabled: bool = False   # (v0.13) account-level short permission


@dataclass
class BrokerPosition:
    ticker: str
    qty: int                     # (v0.13) always positive; direction in `side`
    avg_entry: float
    side: str = "LONG"           # (v0.13) LONG | SHORT (Alpaca reports shorts
                                 #         as negative qty + side="short")


@dataclass
class BrokerOrder:
    broker_order_id: str
    client_order_id: Optional[str]
    ticker: str
    side: str                    # BUY | SELL
    order_type: str              # limit | stop
    qty: int
    limit_price: Optional[float]
    stop_price: Optional[float]
    status: str                  # accepted | partially_filled | filled |
                                 # canceled | rejected | expired
    filled_qty: int = 0
    filled_avg_price: Optional[float] = None
    submitted_ts: Optional[datetime] = None
    raw: dict = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in ("filled", "canceled", "rejected", "expired")


class Broker(Protocol):
    async def get_account(self) -> Account: ...
    async def get_positions(self) -> list[BrokerPosition]: ...
    async def get_open_orders(self) -> list[BrokerOrder]: ...
    async def get_order(self, broker_order_id: str) -> BrokerOrder: ...
    async def submit_limit(self, ticker: str, side: str, qty: int,
                           limit_price: float, client_order_id: str,
                           tif: str = "day") -> BrokerOrder: ...
    async def submit_stop(self, ticker: str, side: str, qty: int,
                          stop_price: float, client_order_id: str,
                          tif: str = "gtc") -> BrokerOrder: ...
    async def cancel(self, broker_order_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Alpaca (paper)
# ---------------------------------------------------------------------------

def _safe_json(resp) -> dict:
    try:
        j = resp.json()
        return j if isinstance(j, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def order_reject_message(method: str, status_code: int,
                         payload: dict) -> str | None:
    """v0.13.3: which HTTP responses are BROKER DECISIONS (-> BrokerReject,
    journaled as BROKER_REJECT, message consumed) rather than transport
    errors (-> raise, retry, DLQ)?

    422 = malformed/unacceptable order (the original case). 403 = the
    broker REFUSING the order: account not allowed to short, insufficient
    buying power, blocked account. Before this, a 403 on a short entry
    crashed the handler and retried a deterministic refusal 5x into the
    DLQ, leaving the intent PENDING with no journal row — four days of
    SELL_SHORT intents (2026-08-17..21) died invisibly this way.

    Pure; unit-tested directly."""
    if method != "POST" or status_code not in (403, 422):
        return None
    return str(payload.get("message") or f"HTTP {status_code} on order")


class AlpacaBroker:
    BASE = "https://paper-api.alpaca.markets"     # paper hard-coded (Phase 4)

    def __init__(self):
        key = os.environ.get("ALPACA_KEY_ID")
        secret = os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY not set")
        self._headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}

    async def _req(self, method: str, path: str, json: dict | None = None) -> dict | list:
        async with httpx.AsyncClient(timeout=15.0, headers=self._headers) as c:
            resp = await c.request(method, f"{self.BASE}{path}", json=json)
            reject = order_reject_message(method, resp.status_code,
                                          _safe_json(resp))
            if reject is not None:
                raise BrokerReject(reject)
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    @staticmethod
    def _order(o: dict) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=o["id"], client_order_id=o.get("client_order_id"),
            ticker=o["symbol"], side=o["side"].upper(),
            order_type=o["type"], qty=int(float(o["qty"])),
            limit_price=float(o["limit_price"]) if o.get("limit_price") else None,
            stop_price=float(o["stop_price"]) if o.get("stop_price") else None,
            status=o["status"], filled_qty=int(float(o.get("filled_qty") or 0)),
            filled_avg_price=(float(o["filled_avg_price"])
                              if o.get("filled_avg_price") else None),
            submitted_ts=(datetime.fromisoformat(o["submitted_at"].replace("Z", "+00:00"))
                          if o.get("submitted_at") else None),
            raw=o)

    async def get_account(self) -> Account:
        a = await self._req("GET", "/v2/account")
        # cash-account settled funds: Alpaca exposes non_marginable_buying_power
        settled = float(a.get("non_marginable_buying_power") or a.get("cash") or 0)
        return Account(
            equity=float(a["equity"]), settled_cash=settled,
            regt_buying_power=float(a.get("regt_buying_power") or 0),
            multiplier=float(a.get("multiplier") or 1),
            shorting_enabled=bool(a.get("shorting_enabled", False)))

    async def get_positions(self) -> list[BrokerPosition]:
        rows = await self._req("GET", "/v2/positions")
        # (v0.13) Alpaca reports shorts as negative qty + side="short"; we
        # normalize to positive qty + explicit side so no caller ever has to
        # reason about signed quantities from the broker.
        return [BrokerPosition(p["symbol"], abs(int(float(p["qty"]))),
                               float(p["avg_entry_price"]),
                               side=(p.get("side") or "long").upper())
                for p in rows]

    async def get_open_orders(self) -> list[BrokerOrder]:
        rows = await self._req("GET", "/v2/orders?status=open&limit=500")
        return [self._order(o) for o in rows]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self._order(await self._req("GET", f"/v2/orders/{broker_order_id}"))

    async def submit_limit(self, ticker, side, qty, limit_price,
                           client_order_id, tif="day") -> BrokerOrder:
        return self._order(await self._req("POST", "/v2/orders", {
            "symbol": ticker, "side": side.lower(), "type": "limit",
            "qty": str(qty), "limit_price": f"{limit_price:.2f}",
            "time_in_force": tif, "client_order_id": client_order_id}))

    async def submit_stop(self, ticker, side, qty, stop_price,
                          client_order_id, tif="gtc") -> BrokerOrder:
        return self._order(await self._req("POST", "/v2/orders", {
            "symbol": ticker, "side": side.lower(), "type": "stop",
            "qty": str(qty), "stop_price": f"{stop_price:.2f}",
            "time_in_force": tif, "client_order_id": client_order_id}))

    async def cancel(self, broker_order_id: str) -> bool:
        try:
            await self._req("DELETE", f"/v2/orders/{broker_order_id}")
            return True
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return False       # already terminal
            raise


class BrokerReject(Exception):
    pass


# ---------------------------------------------------------------------------
# Fake (tests/dev)
# ---------------------------------------------------------------------------

@dataclass
class FakeBroker:
    """Programmable broker. Behaviors are scripted per client_order_id (exact)
    or per ticker (fallback); default behavior is immediate full fill for
    limits and resting acceptance for stops.

    behavior values:
      "fill"           accept + immediately fill at limit/stop price
      "partial:<n>"    accept + fill n shares, remain partially_filled
      "rest"           accept, never fill (until fill_order() called)
      "reject"         broker 422-style rejection
    """
    equity: float = 50_000.0
    settled_cash: float = 50_000.0
    behaviors: dict[str, str] = field(default_factory=dict)
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    positions: dict[str, BrokerPosition] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    submissions: list[dict] = field(default_factory=list)   # assertion trail
    cancels: list[str] = field(default_factory=list)

    # -- programming interface -------------------------------------------------
    def set_behavior(self, key: str, behavior: str) -> None:
        self.behaviors[key] = behavior

    def fill_order(self, broker_order_id: str, price: float | None = None,
                   qty: int | None = None) -> None:
        """Manually fill a resting order (e.g. a catastrophe stop in a test)."""
        o = self.orders[broker_order_id]
        fill_qty = qty if qty is not None else (o.qty - o.filled_qty)
        px = price if price is not None else (o.limit_price or o.stop_price)
        self._apply_fill(o, fill_qty, px)

    def inject_position(self, ticker: str, qty: int, avg_entry: float,
                        side: str = "LONG") -> None:
        """Reconciliation-drift fixture: a position the DB doesn't know about."""
        self.positions[ticker] = BrokerPosition(ticker, qty, avg_entry, side=side)

    def drop_position(self, ticker: str) -> None:
        """Reconciliation-drift fixture: broker lost/closed a position."""
        self.positions.pop(ticker, None)

    # -- internals ---------------------------------------------------------------
    def _behavior_for(self, client_order_id: str, ticker: str) -> str:
        return self.behaviors.get(client_order_id,
                                  self.behaviors.get(ticker, "fill"))

    def _apply_fill(self, o: BrokerOrder, qty: int, price: float) -> None:
        o.filled_qty += qty
        o.filled_avg_price = price
        o.status = "filled" if o.filled_qty >= o.qty else "partially_filled"
        sign = 1 if o.side == "BUY" else -1
        pos = self.positions.get(o.ticker)
        # (v0.13) signed arithmetic: LONG holdings are +, SHORT holdings are −.
        # A SELL from flat opens a short (Alpaca behavior); crossing zero flips.
        held = 0 if pos is None else (pos.qty if pos.side == "LONG" else -pos.qty)
        new_held = held + sign * qty
        if new_held == 0:
            self.positions.pop(o.ticker, None)
        else:
            new_side = "LONG" if new_held > 0 else "SHORT"
            if pos is None or new_side != pos.side:
                # opened fresh (or flipped through zero): basis resets
                self.positions[o.ticker] = BrokerPosition(
                    o.ticker, abs(new_held), price, side=new_side)
            else:
                if abs(new_held) > pos.qty:      # increased same-direction
                    pos.avg_entry = ((pos.avg_entry * pos.qty + price * qty)
                                     / abs(new_held))
                pos.qty = abs(new_held)
        self.settled_cash -= sign * qty * price

    def _submit(self, ticker, side, qty, order_type, limit_price, stop_price,
                client_order_id) -> BrokerOrder:
        # idempotency at the broker: duplicate client_order_id returns original
        for o in self.orders.values():
            if o.client_order_id == client_order_id:
                return o
        behavior = self._behavior_for(client_order_id, ticker)
        self.submissions.append({"ticker": ticker, "side": side, "qty": qty,
                                 "type": order_type, "limit": limit_price,
                                 "stop": stop_price, "coid": client_order_id})
        if behavior == "reject":
            raise BrokerReject(f"scripted reject for {client_order_id}")
        oid = f"fake-{os.urandom(4).hex()}-{next(self._seq)}"
        o = BrokerOrder(broker_order_id=oid, client_order_id=client_order_id,
                        ticker=ticker, side=side, order_type=order_type,
                        qty=qty, limit_price=limit_price, stop_price=stop_price,
                        status="accepted", submitted_ts=utcnow())
        self.orders[oid] = o
        if behavior == "fill" and order_type == "limit":
            self._apply_fill(o, qty, limit_price)
        elif behavior.startswith("partial:") and order_type == "limit":
            self._apply_fill(o, int(behavior.split(":")[1]), limit_price)
        # stops rest by default regardless of behavior (fill via fill_order)
        return o

    # -- Broker interface ----------------------------------------------------------
    async def get_account(self) -> Account:
        return Account(equity=self.equity, settled_cash=self.settled_cash,
                       regt_buying_power=self.equity * 2, multiplier=2.0,
                       shorting_enabled=True)

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions.values())

    async def get_open_orders(self) -> list[BrokerOrder]:
        return [o for o in self.orders.values() if not o.terminal]

    async def get_order(self, broker_order_id: str) -> BrokerOrder:
        return self.orders[broker_order_id]

    async def submit_limit(self, ticker, side, qty, limit_price,
                           client_order_id, tif="day") -> BrokerOrder:
        return self._submit(ticker, side, qty, "limit", limit_price, None,
                            client_order_id)

    async def submit_stop(self, ticker, side, qty, stop_price,
                          client_order_id, tif="gtc") -> BrokerOrder:
        return self._submit(ticker, side, qty, "stop", None, stop_price,
                            client_order_id)

    async def cancel(self, broker_order_id: str) -> bool:
        o = self.orders.get(broker_order_id)
        self.cancels.append(broker_order_id)
        if o is None or o.terminal:
            return False
        o.status = "canceled"
        return True


def get_broker() -> Broker:
    kind = os.environ.get("BROKER", "alpaca").lower()
    if kind == "alpaca":
        return AlpacaBroker()
    if kind == "fake":
        return FakeBroker()
    raise RuntimeError(f"unknown BROKER={kind!r} (expected 'alpaca' or 'fake')")

