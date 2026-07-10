"""Alpaca paper smoke test — run ON THE SPARK, first deployment session.
Validates: auth, account/settled-cash read, positions read, a far-from-market
limit order submit + cancel (never fills), order status round-trip.
Usage: PYTHONPATH=src ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... python3 ops/alpaca-smoke.py
"""
import asyncio, sys

async def main():
    from common.broker import AlpacaBroker
    b = AlpacaBroker()
    acct = await b.get_account()
    print(f"account: equity={acct.equity:.2f} settled={acct.settled_cash:.2f}")
    pos = await b.get_positions()
    print(f"positions: {len(pos)}")
    o = await b.submit_limit("AAPL", "BUY", 1, 1.00,
                             client_order_id="smoke-test-limit-1")
    print(f"submitted: {o.broker_order_id} status={o.status}")
    o2 = await b.get_order(o.broker_order_id)
    print(f"round-trip status={o2.status}")
    ok = await b.cancel(o.broker_order_id)
    print(f"cancelled: {ok}")
    final = await b.get_order(o.broker_order_id)
    assert final.status in ("canceled", "pending_cancel"), final.status
    print("SMOKE TEST PASSED")

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

