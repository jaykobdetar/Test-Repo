"""Switch-level reference solver and check 1 (cell equivalence).

Model: ideal switches, no strengths, no delay.  A transistor conducts when
its gate node is high (N) or low (P).  Nodes connected through conducting
transistors form a group; a group reaching GND takes 0, VDD takes 1, a
primary input takes its value, both rails is a conflict (an error), and a
group reaching no source floats and keeps its stored values.

Settling is wave ordered: in each wave every transistor gated by a node that
changed in the previous wave switches at once, then the affected groups are
recomputed.  This is unit-delay ordering, and it is what makes edge
triggering well defined: the flip-flop's master closes before any new Q can
reach another flip-flop's D (see check_cells below).
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

from .cells import CELLS, Cell


class SolverError(Exception):
    pass


class SwitchSim:
    def __init__(self, transistors, inputs):
        self.nodes: dict[str, int] = {}
        for _k, g, a, b in transistors:
            for n in (g, a, b):
                self.nodes.setdefault(n, len(self.nodes))
        for n in ("VDD", "GND", *inputs):
            self.nodes.setdefault(n, len(self.nodes))
        self.names = {i: n for n, i in self.nodes.items()}
        self.t = [(k, self.nodes[g], self.nodes[a], self.nodes[b]) for k, g, a, b in transistors]
        self.inputs = list(inputs)
        self.source = {self.nodes["VDD"]: 1, self.nodes["GND"]: 0}
        for i in inputs:
            self.source[self.nodes[i]] = 0
        self.val = [0] * len(self.nodes)
        self.val[self.nodes["VDD"]] = 1
        self.on = [False] * len(self.t)
        self.gated = [[] for _ in self.nodes]
        self.chan = [[] for _ in self.nodes]
        for i, (_k, g, a, b) in enumerate(self.t):
            self.gated[g].append(i)
            self.chan[a].append(i)
            self.chan[b].append(i)
        self.gate_nodes = {g for _k, g, _a, _b in self.t}
        self.floated_gate = False
        self.waves = 0

    def _conducts(self, i):
        k, g, _a, _b = self.t[i]
        return self.val[g] == (1 if k == "N" else 0)

    def _group(self, start):
        group, srcs, stack = {start}, set(), [start]
        while stack:
            n = stack.pop()
            for i in self.chan[n]:
                if not self.on[i]:
                    continue
                _k, _g, a, b = self.t[i]
                m = b if a == n else a
                if m in self.source:
                    srcs.add(self.source[m])
                elif m not in group:
                    group.add(m)
                    stack.append(m)
        return group, srcs

    def settle(self, changes: dict[str, int] | None = None, everything=False, max_waves=1000):
        """Apply input changes, settle, return the number of waves."""
        wave = set()
        affected = set()
        for name, v in (changes or {}).items():
            n = self.nodes[name]
            if self.source[n] != v:
                self.source[n] = v
                self.val[n] = v
                wave.add(n)
                for i in self.chan[n]:
                    affected.update(self.t[i][2:])
        if everything:
            for n, v in self.source.items():
                self.val[n] = v
            wave = set(range(len(self.nodes)))
            for i in range(len(self.t)):
                self.on[i] = self._conducts(i)
            affected = set(range(len(self.nodes)))
        waves = 0
        while wave or affected:
            for n in wave:
                for i in self.gated[n]:
                    c = self._conducts(i)
                    if c != self.on[i]:
                        self.on[i] = c
                        affected.update(self.t[i][2:])
            nxt = set()
            seen = set()
            for n in affected:
                if n in self.source or n in seen:
                    continue
                group, srcs = self._group(n)
                seen |= group
                if len(srcs) > 1:
                    raise SolverError("conflict: group " + ",".join(sorted(self.names[g] for g in group)))
                if not srcs:
                    if group & self.gate_nodes and not everything:
                        self.floated_gate = True
                    continue
                v = srcs.pop()
                for g in group:
                    if self.val[g] != v:
                        self.val[g] = v
                        nxt.add(g)
            wave, affected = nxt, set()
            waves += 1
            if waves > max_waves:
                raise SolverError("no settled state (oscillation)")
        self.waves = max(self.waves, waves)
        return waves

    def get(self, name):
        return self.val[self.nodes[name]]

    def on_states(self) -> tuple[int, ...]:
        return tuple(int(self._conducts(i)) for i in range(len(self.t)))


def pixel_mask(states, cell: Cell) -> tuple[int, int]:
    """(P-row mask, N-row mask): bit i = i-th P (or N) transistor conducts."""
    pm = nm = 0
    pi = ni = 0
    for (k, _g, _a, _b), on in zip(cell.transistors, states):
        if k == "P":
            pm |= on << pi
            pi += 1
        else:
            nm |= on << ni
            ni += 1
    return pm, nm


def check_comb(cell: Cell) -> dict:
    """Exhaustive: every transition between input combinations."""
    sim = SwitchSim(cell.transistors, cell.inputs)
    n = len(cell.inputs)
    combos = list(itertools.product([0, 1], repeat=n))
    states = {}
    sim.settle({p: v for p, v in zip(cell.inputs, combos[0])}, everything=True)
    for a in combos:
        for b in combos:
            sim.settle({p: v for p, v in zip(cell.inputs, a)})
            sim.settle({p: v for p, v in zip(cell.inputs, b)})
            vals = dict(zip(cell.inputs, b))
            if sim.get(cell.output) != cell.fn(vals):
                raise SolverError(f"{cell.name}: output wrong for {vals}")
            k = sum(v << i for i, v in enumerate(b))
            st = sim.on_states()
            if states.setdefault(k, st) != st:
                raise SolverError(f"{cell.name}: transistor states depend on history for k={k}")
    if sim.floated_gate:
        raise SolverError(f"{cell.name}: a gate node floated")
    return {k: states[k] for k in sorted(states)}


def check_dff(cell: Cell) -> dict:
    """Breadth-first over every reachable state, one input change at a time.

    Verifies Q against the ideal rising-edge flip-flop with async clear, that
    every settled transistor state is a function of (D, Q, CK, RN), and that
    no latch node ever floats or conflicts.
    """
    pins = ("D", "CK", "RN")
    sim = SwitchSim(cell.transistors, pins)
    sim.settle({"D": 0, "CK": 0, "RN": 0}, everything=True)
    start = (tuple(sim.val), tuple(sim.on), dict(sim.source), 0)
    seen = {}
    table = {}
    frontier = [(start, {"D": 0, "CK": 0, "RN": 0})]
    visited = set()
    while frontier:
        (val, on, src, qlog), ins = frontier.pop()
        key = (val, tuple(sorted(ins.items())), qlog)
        if key in visited:
            continue
        visited.add(key)
        for pin in pins:
            sim.val, sim.on, sim.source = list(val), list(on), dict(src)
            new = dict(ins)
            new[pin] ^= 1
            sim.settle({pin: new[pin]})
            q = qlog
            if new["RN"] == 0:
                q = 0
            elif pin == "CK" and new["CK"] == 1:
                q = new["D"]
            if sim.get("Q") != q:
                raise SolverError(f"DFFR: Q={sim.get('Q')} expected {q} after {pin} -> {new}")
            k = (new["D"], q, new["CK"], new["RN"])
            st = sim.on_states()
            if table.setdefault(k, st) != st:
                raise SolverError(f"DFFR: transistor states not a function of (D,Q,CK,RN) at {k}")
            seen[k] = True
            frontier.append(((tuple(sim.val), tuple(sim.on), dict(sim.source), q), new))
    if sim.floated_gate:
        raise SolverError("DFFR: a latch node floated")
    missing = [k for k in itertools.product([0, 1], repeat=4) if k not in table and not (k[3] == 0 and k[1] == 1)]
    if missing:
        raise SolverError(f"DFFR: unreachable configurations {missing}")
    return table


def build_patterns() -> dict:
    """Per-cell transistor states for every input combination (the drawing source)."""
    out = {}
    for c in CELLS.values():
        if c.sequential:
            t = check_dff(c)
            out[c.name] = {
                "kind": "dff",
                # index = D | Q<<1 | CK<<2  (RN = 1, normal operation)
                "states": [list(t[(d, q, ck, 1)]) for ck in (0, 1) for q in (0, 1) for d in (0, 1)],
            }
        else:
            t = check_comb(c)
            out[c.name] = {"kind": "comb", "states": [list(t[k]) for k in range(1 << len(c.inputs))]}
        out[c.name]["types"] = [k for k, _g, _a, _b in c.transistors]
        out[c.name]["inputs"] = list(c.inputs)
    return out


def main(argv):
    pats = build_patterns()
    if len(argv) > 1:
        Path(argv[1]).write_text(json.dumps(pats, indent=1) + "\n")
    for name, p in pats.items():
        print(
            f"  {name:6s} {len(p['types']):3d} transistors, {len(p['states'])} input states: switch level == truth table"
        )
    print("PASS")


if __name__ == "__main__":
    main(sys.argv)
