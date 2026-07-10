"""MIP v1 — Machine-Invalidation Predicate DSL reference implementation.

Three functions matter:
    validate(spec)                      -> raises MIPError if not schema-legal
    compile_predicate(spec, ctx)       -> CompiledPredicate (refs resolved to literals)
    CompiledPredicate.on_bar(bar)      -> None | Fire(action, detail)

Design per invalidation-dsl-spec.md: closed vocabulary, compile-to-numbers at ARM,
risk-reducing actions only, deterministic evaluation (same bars => same fires).
This module is dependency-free by intent; C4 embeds it directly.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

METRICS = {"close", "low", "high", "last", "vwap", "volume_ratio", "drawdown_r"}
TFS = {"1m", "5m", "15m", "session"}
OPS = {"<", "<=", ">", ">=", "cross_below", "cross_above"}
REFS = {"prenews_price", "entry_price", "initial_stop",
        "prior_day_low", "prior_day_high", "day_open"}

STDLIB: dict[str, dict[str, Any]] = {
    "close_below_prenews": {
        "id": "close_below_prenews",
        "when": {"metric": "close", "tf": "session", "op": "<",
                 "value": {"ref": "prenews_price"}},
        "persist": {"bars": 1},
        "action": {"type": "exit"},
    },
    "session_close_below_prior_low": {
        "id": "session_close_below_prior_low",
        "when": {"metric": "close", "tf": "session", "op": "<",
                 "value": {"ref": "prior_day_low"}},
        "persist": {"bars": 1},
        "action": {"type": "exit"},
    },
    "vwap_loss_on_volume": {
        "id": "vwap_loss_on_volume",
        "when": {"all": [
            {"metric": "close", "tf": "15m", "op": "<", "value": {"ref": "__vwap__"}},
            {"metric": "volume_ratio", "tf": "15m", "op": ">", "value": 1.5},
        ]},
        "persist": {"bars": 2},
        "action": {"type": "tighten_stop", "to": {"ref": "breakeven"}},
        "_params": {"vol_mult": ("when.all.1.value", 1.5)},
    },
    "give_back_from_entry": {
        "id": "give_back_from_entry",
        "when": {"metric": "drawdown_r", "tf": "5m", "op": ">=", "value": 0.75},
        "persist": {"bars": 1},
        "action": {"type": "alert_guard"},
        "_params": {"r": ("when.value", 0.75)},
    },
    "break_of_day_open": {
        "id": "break_of_day_open",
        "when": {"metric": "close", "tf": "15m", "op": "cross_below",
                 "value": {"ref": "day_open"}},
        "persist": {"bars": 1},
        "action": {"type": "alert_guard"},
    },
}


class MIPError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        super().__init__(f"{code}: {detail}")


# ---------------------------------------------------------------------------
# Validation (structural — what the JSON Schema enforces on the model side)
# ---------------------------------------------------------------------------

def _validate_cond(c: dict) -> None:
    if set(c) != {"metric", "tf", "op", "value"}:
        raise MIPError("SCHEMA", f"condition keys {sorted(c)}")
    if c["metric"] not in METRICS:
        raise MIPError("SCHEMA", f"metric {c['metric']}")
    if c["tf"] not in TFS:
        raise MIPError("SCHEMA", f"tf {c['tf']}")
    if c["op"] not in OPS:
        raise MIPError("SCHEMA", f"op {c['op']}")
    v = c["value"]
    if isinstance(v, dict):
        if v.get("ref") not in REFS and v.get("ref") != "__vwap__":
            raise MIPError("SCHEMA", f"ref {v.get('ref')}")
        off = v.get("offset_pct", 0)
        if not isinstance(off, (int, float)) or not -0.2 <= off <= 0.2:
            raise MIPError("SCHEMA", f"offset_pct {off}")
    elif not isinstance(v, (int, float)):
        raise MIPError("SCHEMA", f"value {v!r}")


def validate(spec: dict) -> dict:
    """Validate a spec; expand stdlib calls. Returns the expanded custom form."""
    if "std" in spec:
        if spec["std"] not in STDLIB:
            raise MIPError("SCHEMA", f"unknown std {spec['std']}")
        base = _deepcopy(STDLIB[spec["std"]])
        params = spec.get("params", {})
        mapping = base.pop("_params", {})
        for name, val in params.items():
            if name not in mapping:
                raise MIPError("SCHEMA", f"unknown param {name} for {spec['std']}")
            path, _default = mapping[name]
            _set_path(base, path, val)
        base.pop("_params", None)
        return validate(base)

    required = {"id", "when", "action"}
    if not required <= set(spec):
        raise MIPError("SCHEMA", f"missing {required - set(spec)}")
    when = spec["when"]
    conds = when["all"] if "all" in when else [when]
    if not 1 <= len(conds) <= 3:
        raise MIPError("SCHEMA", "1..3 conditions")
    for c in conds:
        _validate_cond(c)
    bars = spec.get("persist", {}).get("bars", 1)
    if not 1 <= bars <= 10:
        raise MIPError("SCHEMA", f"persist.bars {bars}")
    act = spec["action"]
    if act.get("type") not in {"exit", "tighten_stop", "alert_guard"}:
        raise MIPError("SCHEMA", f"action {act}")
    if act.get("type") == "tighten_stop":
        to = act.get("to", {})
        ok = to == {"ref": "breakeven"} or (
            isinstance(to.get("atr_k"), (int, float)) and 0 < to["atr_k"] <= 3)
        if not ok:
            raise MIPError("SCHEMA", f"tighten_stop.to {to}")
    return spec


# ---------------------------------------------------------------------------
# Compile / ARM — resolve refs to literals, run sanity checks
# ---------------------------------------------------------------------------

@dataclass
class ArmContext:
    entry_price: float
    initial_stop: float
    r_unit: float
    prenews_price: Optional[float] = None
    prior_day_low: Optional[float] = None
    prior_day_high: Optional[float] = None
    day_open: Optional[float] = None          # None pre-open; lazy refs allowed
    atr_14: Optional[float] = None
    mark: Optional[float] = None              # current price at ARM


@dataclass
class Bar:
    ts: int
    tf: str
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume_ratio: float


@dataclass
class Fire:
    predicate_id: str
    action: dict
    bar_ts: int
    detail: str


@dataclass
class CompiledCond:
    metric: str
    tf: str
    op: str
    value: Optional[float]          # None => lazy (__vwap__ / day_open pre-open)
    lazy_ref: Optional[str] = None
    entry_price: float = 0.0
    r_unit: float = 1.0
    _prev: Optional[float] = field(default=None, repr=False)

    def eval(self, bar: Bar, day_open: Optional[float]) -> bool:
        target = self.value
        if self.lazy_ref == "__vwap__":
            target = bar.vwap
        elif self.lazy_ref == "day_open":
            if day_open is None:
                return False
            target = day_open
        if self.metric == "drawdown_r":
            m = (self.entry_price - bar.low) / self.r_unit
        elif self.metric == "volume_ratio":
            m = bar.volume_ratio
        elif self.metric in ("close", "last"):
            m = bar.close
        else:
            m = getattr(bar, self.metric, None)
        if m is None or target is None:
            return False
        prev, self._prev = self._prev, m
        if self.op == "<":
            return m < target
        if self.op == "<=":
            return m <= target
        if self.op == ">":
            return m > target
        if self.op == ">=":
            return m >= target
        if self.op == "cross_below":
            return prev is not None and prev >= target and m < target
        if self.op == "cross_above":
            return prev is not None and prev <= target and m > target
        return False


@dataclass
class CompiledPredicate:
    predicate_id: str
    conds: list[CompiledCond]
    persist_bars: int
    action: dict
    compiled_form: dict             # journal this: INVALIDATION_ARMED
    _streak: int = 0
    fired: bool = False

    def on_bar(self, bar: Bar, day_open: Optional[float] = None) -> Optional[Fire]:
        if self.fired:
            return None
        relevant = [c for c in self.conds if c.tf == bar.tf]
        if not relevant:
            return None
        # a bar only advances the streak if EVERY condition at this tf holds and
        # all other-tf conditions held on their most recent bar (single-tf in v1
        # stdlib; mixed-tf ANDs evaluate on the slower tf's cadence)
        ok = all(c.eval(bar, day_open) for c in relevant)
        others = [c for c in self.conds if c.tf != bar.tf]
        if others:      # mixed-tf: require their last evaluation to have been true
            ok = ok and all(c._prev is not None for c in others)
        self._streak = self._streak + 1 if ok else 0
        if self._streak >= self.persist_bars:
            self.fired = True
            return Fire(self.predicate_id, self.action, bar.ts,
                        f"{self.predicate_id} persisted {self._streak} bar(s)")
        return None


def _resolve(value: Any, ctx: ArmContext) -> tuple[Optional[float], Optional[str]]:
    if isinstance(value, (int, float)):
        return float(value), None
    ref = value["ref"]
    if ref == "__vwap__":
        return None, "__vwap__"
    if ref == "day_open" and ctx.day_open is None:
        return None, "day_open"                      # lazy, compiles at 9:30
    raw = getattr(ctx, ref, None)
    if raw is None:
        raise MIPError("UNRESOLVABLE_REF", ref)
    return raw * (1 + value.get("offset_pct", 0)), None


def compile_predicate(spec: dict, ctx: ArmContext) -> CompiledPredicate:
    spec = validate(spec)
    when = spec["when"]
    conds_spec = when["all"] if "all" in when else [when]
    conds, literal_forms = [], []
    for c in conds_spec:
        val, lazy = _resolve(c["value"], ctx)
        conds.append(CompiledCond(c["metric"], c["tf"], c["op"], val, lazy,
                                  entry_price=ctx.entry_price, r_unit=ctx.r_unit))
        literal_forms.append({**c, "value": val if val is not None else f"lazy:{lazy}"})
        # sanity: price-level triggers must be plausible
        if val is not None and c["metric"] in ("close", "low", "high", "last"):
            if val <= 0 or abs(val - ctx.entry_price) > 0.5 * ctx.entry_price:
                raise MIPError("ABSURD_LEVEL", f"{val} vs entry {ctx.entry_price}")
    if spec["action"]["type"] == "exit" and ctx.mark is not None:
        for cc in conds:
            if cc.value is not None and cc.metric in ("close", "last") \
               and cc.op in ("<", "<=") and ctx.mark <= cc.value:
                raise MIPError("IMMEDIATE_FIRE",
                               f"mark {ctx.mark} already beyond {cc.value}")
    persist = spec.get("persist", {}).get("bars", 1)
    if all(c.tf == "session" for c in conds):
        persist = 1                                   # spec §3: session ignores persist
    compiled_form = {"id": spec["id"], "conds": literal_forms,
                     "persist": persist, "action": spec["action"]}
    return CompiledPredicate(spec["id"], conds, persist, spec["action"], compiled_form)


# ---------------------------------------------------------------------------

def _deepcopy(x):
    import copy
    return copy.deepcopy(x)


def _set_path(obj, path: str, val):
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[int(p)] if p.isdigit() else obj[p]
    last = parts[-1]
    if last.isdigit():
        obj[int(last)] = val
    else:
        obj[last] = val

