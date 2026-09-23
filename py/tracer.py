"""
Play tracer — proof of concept.

Runs a viewer's solution (the body of a LeetCode-style function) and turns what the
code DOES to the state into a flat event list that the presenter can act out on the
board. The viewer writes only the core logic; every container they touch is a fixed,
instrumented helper we hand them (nums, grid, visited, queue, ...), so reads are
observable, and a per-line variable diff catches any list/scalar they create
themselves (dp arrays, rolling variables). Recursion is seen through call/return.

Design rule: the presenter performs STATE CHANGES, never syntax. Bottom-up array,
two rolling variables and top-down memo all produce the same vocabulary of events.

Runs on CPython today; meant for Pyodide in the browser (sys.settrace works there).
"""
from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# ----------------------------------------------------------------------------- events

@dataclass
class Event:
    kind: str                 # read | write | scalar | call | return | visit | push | pop | result
    line: int
    data: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        d = " ".join(f"{k}={v}" for k, v in self.data.items())
        return f"L{self.line:<3} {self.kind:<7} {d}"


class Trace:
    def __init__(self) -> None:
        self.events: list[Event] = []
        self.line = 0
        self.depth = 0

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append(Event(kind, self.line, data))


# ----------------------------------------------------------------------------- instrumented helpers

class TracedList(list):
    """`nums` / `dp` handed to the viewer: reads and writes are observable."""
    def __init__(self, it, trace: Trace, name: str):
        super().__init__(it)
        self._t, self._name = trace, name

    def __getitem__(self, i):
        v = super().__getitem__(i)
        if isinstance(i, int):
            self._t.emit("read", var=self._name, i=i % len(self) if len(self) else i, value=v)
        return v

    def __setitem__(self, i, v):
        super().__setitem__(i, v)
        if isinstance(i, int):
            self._t.emit("write", var=self._name, i=i % len(self), value=v)

    def __iter__(self):
        for i in range(len(self)):
            yield self[i]          # goes through __getitem__ so `for x in nums` is a read per house


class TracedDict(dict):
    """`memo` for top-down solutions: a write is a roof sign, a hit is a tap on it."""
    def __init__(self, trace: Trace, name: str = "memo"):
        super().__init__()
        self._t, self._name = trace, name

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        self._t.emit("write", var=self._name, i=k, value=v)

    def __contains__(self, k):
        hit = super().__contains__(k)
        if hit:
            self._t.emit("hit", var=self._name, i=k, value=super().__getitem__(k))
        return hit


class TracedGrid(list):
    """2-D grid: reading grid[r][c] is one event (she looks at that cell)."""
    def __init__(self, rows, trace: Trace, name: str = "grid"):
        super().__init__(_TracedRow(r, trace, name, ri) for ri, r in enumerate(rows))


class _TracedRow(list):
    def __init__(self, row, trace: Trace, name: str, r: int):
        super().__init__(row)
        self._t, self._name, self._r = trace, name, r

    def __getitem__(self, c):
        v = super().__getitem__(c)
        if isinstance(c, int):
            self._t.emit("read", var=self._name, r=self._r, c=c, value=v)
        return v

    def __setitem__(self, c, v):
        super().__setitem__(c, v)
        if isinstance(c, int):
            self._t.emit("write", var=self._name, r=self._r, c=c, value=v)


class TracedSet(set):
    """`visited`: adding a cell = she plants a flag on it."""
    def __init__(self, trace: Trace, name: str = "visited"):
        super().__init__()
        self._t, self._name = trace, name

    def add(self, x):
        if x not in self:
            self._t.emit("visit", var=self._name, at=x)
        super().add(x)


class TracedQueue(deque):
    """`queue` for BFS: push = card goes to the back of the tray, pop = she takes the front one."""
    def __init__(self, trace: Trace, name: str = "queue"):
        super().__init__()
        self._t, self._name = trace, name

    def append(self, x):
        super().append(x)
        self._t.emit("push", var=self._name, item=x, size=len(self))

    def popleft(self):
        x = super().popleft()
        self._t.emit("pop", var=self._name, item=x, size=len(self))
        return x

    def pop(self):
        x = super().pop()
        self._t.emit("pop", var=self._name, item=x, size=len(self), end="back")
        return x


# ----------------------------------------------------------------------------- the tracer

def _snapshot(locals_: dict) -> dict:
    out = {}
    for k, v in locals_.items():
        if k.startswith("_") or callable(v):
            continue
        if isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        elif isinstance(v, list) and not isinstance(v, (TracedList, TracedGrid, _TracedRow)):
            out[k] = list(v)  # copy so we can diff next line
        elif isinstance(v, dict) and not isinstance(v, TracedDict) and all(isinstance(x, int) for x in v):
            out[k] = dict(v)
    return out


def run_traced(
    source: str,
    func_name: str,
    args: tuple,
    helpers: dict[str, Any] | None = None,
    max_events: int = 5000,
    trace: Trace | None = None,
) -> tuple[Any, Trace]:
    """Exec `source` (must define `func_name`), call it with `args` under a line tracer.
    Pass the same `trace` the instrumented helpers were built with so their events and the
    line-diff events land in one ordered list."""
    trace = trace or Trace()
    ns: dict[str, Any] = {"__name__": "viewer", "deque": deque}
    ns.update(helpers or {})
    exec(compile(source, "<viewer>", "exec"), ns)
    if func_name not in ns or not callable(ns[func_name]):
        raise NameError(f"define a function called {func_name}(...) — that's what gets run")
    fn: Callable = ns[func_name]
    prev: dict[int, dict] = {}   # frame id -> last snapshot

    def diff(frame) -> None:
        fid = id(frame)
        now = _snapshot(frame.f_locals)
        was = prev.get(fid, {})
        for k, v in now.items():
            if k not in was:
                if isinstance(v, list):
                    trace.emit("newlist", var=k, size=len(v))
                elif isinstance(v, (int, float)):
                    trace.emit("scalar", var=k, value=v)
                continue
            w = was[k]
            if isinstance(v, list) and isinstance(w, list):
                for i, (a, b) in enumerate(zip(w, v)):
                    if a != b:
                        trace.emit("write", var=k, i=i, value=b, was=a)
            elif isinstance(v, dict) and isinstance(w, dict):
                for i, b in v.items():
                    if i not in w or w[i] != b:
                        trace.emit("write", var=k, i=i, value=b, was=w.get(i))
            elif v != w and isinstance(v, (int, float)):
                trace.emit("scalar", var=k, value=v, was=w)
        prev[fid] = now

    def tracer(frame, event, arg):
        if frame.f_code.co_filename != "<viewer>":
            return None
        if event == "call":
            trace.depth += 1
            trace.line = frame.f_lineno
            a = {k: frame.f_locals[k] for k in frame.f_code.co_varnames[: frame.f_code.co_argcount] if k in frame.f_locals}
            a = {k: v for k, v in a.items() if isinstance(v, (int, str, tuple))}
            trace.emit("call", fn=frame.f_code.co_name, args=a, depth=trace.depth)
            prev[id(frame)] = _snapshot(frame.f_locals)
            return tracer
        if event == "line":
            diff(frame)
            trace.line = frame.f_lineno
        elif event == "return":
            diff(frame)
            trace.emit("return", fn=frame.f_code.co_name, value=arg if isinstance(arg, (int, float, str, bool, tuple)) or arg is None else type(arg).__name__, depth=trace.depth)
            trace.depth -= 1
            prev.pop(id(frame), None)
        if len(trace.events) > max_events:
            raise RuntimeError("too many events — is the code looping?")
        return tracer

    sys.settrace(tracer)
    try:
        result = fn(*args)
    finally:
        sys.settrace(None)
    trace.emit("result", value=result)
    return result, trace


# ----------------------------------------------------------------------------- director: events -> what she does

def direct_house_robber(trace: Trace, nums: list[int], best: int) -> list[str]:
    """Turn the raw events into stage directions for the street board."""
    n = len(nums)
    out: list[str] = []
    offset: dict[str, int] = {}     # dp of size n+1 is shifted by one house
    signs: dict[int, int] = {}
    for e in trace.events:
        d = e.data
        if e.kind == "read" and d.get("var") == "nums":
            out.append(f"look at house {d['i']} (${d['value']})")
        elif e.kind == "newlist" and d["size"] in (n, n + 1, n + 2):
            offset[d["var"]] = d["size"] - n; out.append(f"lay out {d['size']} blank roof signs ({d['var']})")
        elif e.kind == "write" and d["var"] != "nums":
            i, v = d["i"], d["value"]
            house = i - offset.get(d["var"], 0)
            if house < 0:
                continue
            skip = house > 0 and signs.get(house - 1) == v
            signs[house] = v
            out.append(f"write ${v} on the sign of house {house}" + ("  (same as the sign before: SKIP this house)" if skip else "  (TAKE)" if house > 0 else ""))
        elif e.kind == "hit":
            out.append(f"tap the sign of house {d['i']}: already know ${d['value']}")
        elif e.kind == "scalar" and d["var"] not in ("i", "n", "j", "k") and isinstance(d["value"], int):
            out.append(f"floating card '{d['var']}' now shows ${d['value']}")
        elif e.kind == "call" and d["depth"] > 1:
            out.append(f"{'  ' * (d['depth'] - 1)}stack a card: {d['fn']}({', '.join(f'{k}={v}' for k, v in d['args'].items())})")
        elif e.kind == "return" and d["depth"] > 1:
            out.append(f"{'  ' * (d['depth'] - 1)}take the card back: {d['fn']} -> {d['value']}")
        elif e.kind == "result":
            v = d["value"]
            out.append(f"RESULT ${v}: " + ("carry the plan's piles to the tray, celebrate" if v == best else f"tray shows ${v} but the street pays ${best}: shake head, show the difference"))
    return out


def direct_grid(trace: Trace) -> list[str]:
    out: list[str] = []
    for e in trace.events:
        d = e.data
        if e.kind == "read" and d.get("var") == "grid":
            out.append(f"glance at cell ({d['r']},{d['c']}) = {d['value']}")
        elif e.kind == "visit":
            out.append(f"plant a flag on {d['at']}")
        elif e.kind == "push":
            out.append(f"card {d['item']} goes to the back of the queue (now {d['size']})")
        elif e.kind == "pop":
            out.append(f"take card {d['item']} from the {'back' if d.get('end') else 'front'} of the queue (left {d['size']})")
        elif e.kind == "call" and d["depth"] > 1:
            out.append(f"{'  ' * (d['depth'] - 1)}step deeper: {d['fn']}({', '.join(f'{k}={v}' for k, v in d['args'].items())})  [stack {d['depth'] - 1}]")
        elif e.kind == "return" and d["depth"] > 1:
            out.append(f"{'  ' * (d['depth'] - 1)}step back out [stack {d['depth'] - 2}]")
        elif e.kind == "scalar" and d["var"] in ("count", "islands", "steps", "dist", "level"):
            out.append(f"counter '{d['var']}' -> {d['value']}")
        elif e.kind == "result":
            out.append(f"RESULT {d['value']}")
    return out
