"""MIP v1 test suite — run with: python test_invalidation_dsl.py"""
from invalidation_dsl import (ArmContext, Bar, MIPError, STDLIB,
                              compile_predicate, validate)
 
CTX = ArmContext(entry_price=100.0, initial_stop=95.0, r_unit=5.0,
                 prenews_price=98.4, prior_day_low=96.1, prior_day_high=101.3,
                 day_open=99.2, atr_14=2.5, mark=100.0)
 
def bar(tf, close, low=None, vwap=None, vol=1.0, ts=0):
    low = close if low is None else low
    vwap = close if vwap is None else vwap
    return Bar(ts=ts, tf=tf, open=close, high=close, low=low, close=close,
               vwap=vwap, volume_ratio=vol)
 
passed = 0
def ok(name, cond):
    global passed
    assert cond, name
    passed += 1
    print(f"  ok  {name}")
 
# ---- 1. Validation -----------------------------------------------------------
try:
    validate({"std": "no_such_thing"})
    assert False
except MIPError as e:
    ok("unknown stdlib rejected", e.code == "SCHEMA")
 
try:
    validate({"id": "x", "when": {"metric": "close", "tf": "1m", "op": "<", "value": 1},
              "action": {"type": "widen_stop"}})
    assert False
except MIPError as e:
    ok("risk-increasing action unrepresentable", e.code == "SCHEMA")
 
try:
    validate({"id": "x", "when": {"all": [
        {"metric": "close", "tf": "1m", "op": "<", "value": 1}] * 4},
        "action": {"type": "exit"}})
    assert False
except MIPError as e:
    ok("max 3 conditions enforced", e.code == "SCHEMA")
 
# ---- 2. Compile sanity checks -------------------------------------------------
try:
    compile_predicate({"id": "x", "action": {"type": "exit"},
                       "when": {"metric": "close", "tf": "session", "op": "<",
                                "value": {"ref": "prenews_price"}}},
                      ArmContext(entry_price=100, initial_stop=95, r_unit=5,
                                 prenews_price=None, mark=100))
    assert False
except MIPError as e:
    ok("unresolvable ref rejected", e.code == "UNRESOLVABLE_REF")
 
try:
    compile_predicate({"id": "x", "action": {"type": "exit"},
                       "when": {"metric": "close", "tf": "session", "op": "<",
                                "value": {"ref": "prenews_price"}}},
                      ArmContext(entry_price=100, initial_stop=95, r_unit=5,
                                 prenews_price=98.4, mark=97.0))
    assert False
except MIPError as e:
    ok("already-true exit rejected (IMMEDIATE_FIRE)", e.code == "IMMEDIATE_FIRE")
 
try:
    compile_predicate({"id": "x", "action": {"type": "exit"},
                       "when": {"metric": "close", "tf": "session", "op": "<",
                                "value": 12.0}}, CTX)
    assert False
except MIPError as e:
    ok("absurd level rejected", e.code == "ABSURD_LEVEL")
 
p = compile_predicate({"std": "close_below_prenews"}, CTX)
ok("stdlib compiles to literals",
   p.compiled_form["conds"][0]["value"] == 98.4 and p.compiled_form["persist"] == 1)
 
# ---- 3. Evaluation: session close below pre-news ------------------------------
p = compile_predicate({"std": "close_below_prenews"}, CTX)
ok("above pre-news: no fire", p.on_bar(bar("session", 99.0, ts=1)) is None)
f = p.on_bar(bar("session", 98.0, ts=2))
ok("below pre-news: fires exit", f is not None and f.action["type"] == "exit")
ok("fires once then disarms", p.on_bar(bar("session", 90.0, ts=3)) is None)
 
# ---- 4. Persistence with streak reset ------------------------------------------
p = compile_predicate({"std": "vwap_loss_on_volume"}, CTX)
ok("streak 1/2: no fire",
   p.on_bar(bar("15m", 99.0, vwap=99.5, vol=2.0, ts=1)) is None)
ok("streak resets on qualifying miss",
   p.on_bar(bar("15m", 99.6, vwap=99.5, vol=2.0, ts=2)) is None and p._streak == 0)
p.on_bar(bar("15m", 99.0, vwap=99.5, vol=2.0, ts=3))
f = p.on_bar(bar("15m", 98.9, vwap=99.4, vol=1.8, ts=4))
ok("2 consecutive: fires tighten_stop",
   f is not None and f.action == {"type": "tighten_stop", "to": {"ref": "breakeven"}})
 
# ---- 5. Param override ----------------------------------------------------------
p = compile_predicate({"std": "vwap_loss_on_volume", "params": {"vol_mult": 3.0}}, CTX)
p.on_bar(bar("15m", 99.0, vwap=99.5, vol=2.0, ts=1))
ok("param override respected (vol 2.0 < 3.0 no streak)", p._streak == 0)
 
# ---- 6. cross_below is edge-triggered -------------------------------------------
p = compile_predicate({"std": "break_of_day_open"}, CTX)   # day_open resolved lazily
ok("first bar below open: no prev, no fire",
   p.on_bar(bar("15m", 98.0, ts=1), day_open=99.2) is None)
p2 = compile_predicate({"std": "break_of_day_open"}, CTX)
p2.on_bar(bar("15m", 99.5, ts=1), day_open=99.2)
f = p2.on_bar(bar("15m", 98.9, ts=2), day_open=99.2)
ok("cross from above fires alert_guard",
   f is not None and f.action["type"] == "alert_guard")
 
# ---- 7. drawdown_r --------------------------------------------------------------
p = compile_predicate({"std": "give_back_from_entry", "params": {"r": 0.5}}, CTX)
ok("dd 0.4R: hold", p.on_bar(bar("5m", 98.5, low=98.0, ts=1)) is None)
f = p.on_bar(bar("5m", 97.6, low=97.4, ts=2))   # (100-97.4)/5 = 0.52R
ok("dd 0.52R >= 0.5R: alert_guard", f is not None and f.action["type"] == "alert_guard")
 
# ---- 8. Determinism: same bars => identical fires --------------------------------
bars = [bar("session", 99.5, ts=1), bar("session", 98.3, ts=2),
        bar("session", 97.0, ts=3)]
runs = []
for _ in range(2):
    p = compile_predicate({"std": "close_below_prenews"}, CTX)
    runs.append([(f.bar_ts, f.predicate_id) for b in bars
                 if (f := p.on_bar(b)) is not None])
ok("determinism across runs", runs[0] == runs[1] == [(2, "close_below_prenews")])
 
# ---- 9. Custom mixed-tf AND compiles + literal journal form ----------------------
spec = {"id": "hbm_breakdown",
        "when": {"all": [
            {"metric": "close", "tf": "15m", "op": "<",
             "value": {"ref": "prenews_price", "offset_pct": -0.02}},
            {"metric": "volume_ratio", "tf": "15m", "op": ">", "value": 1.5}]},
        "persist": {"bars": 2}, "action": {"type": "exit"}}
p = compile_predicate(spec, CTX)
ok("offset_pct applied at compile",
   abs(p.compiled_form["conds"][0]["value"] - 98.4 * 0.98) < 1e-9)
 
print(f"\nALL {passed} CHECKS PASSED")