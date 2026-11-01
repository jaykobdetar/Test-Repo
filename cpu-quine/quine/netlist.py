"""Gate netlist: load Yosys JSON, enforce the circuit discipline, levelize.

The checks here are the "build fails" rules of the design: only the ten
library cells, one driver per net, no combinational loops, flip-flops are
DFFR on the single clock and reset, and every flip-flop is a named register
bit from arch.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import arch
from .cells import CELLS

CONST0 = "0"
CONST1 = "1"


class DisciplineError(Exception):
    pass


@dataclass
class Gate:
    name: str
    type: str
    ins: dict[str, object]  # pin -> net
    out: object  # net
    level: int = 0


@dataclass
class Netlist:
    gates: list[Gate]  # combinational, levelized order
    dffs: list[Gate]  # in arch.REGISTERS bit order
    ff_reg: dict[object, tuple[str, int]]  # Q net -> (register, bit)
    inputs: dict[str, list]  # port -> nets
    outputs: dict[str, list]
    clk: object
    rst_n: object
    nets: list = field(default_factory=list)

    @property
    def cells(self):
        return self.gates + self.dffs

    def transistor_count(self) -> int:
        return sum(CELLS[g.type].size for g in self.cells)

    def evaluate(self, regs: dict[str, int], rdata: int) -> dict:
        """Gate-level evaluation of all nets for state regs and memory data rdata."""
        v = {CONST0: 0, CONST1: 1, self.clk: 0, self.rst_n: 1}
        for i, n in enumerate(self.inputs["rdata"]):
            v[n] = (rdata >> i) & 1
        for q, (r, b) in self.ff_reg.items():
            v[q] = (regs[r] >> b) & 1
        for g in self.gates:
            v[g.out] = CELLS[g.type].fn({p: v[n] for p, n in g.ins.items()})
        return v

    def word(self, v: dict, nets: list) -> int:
        return sum(v[n] << i for i, n in enumerate(nets))

    def next_state(self, v: dict) -> dict[str, int]:
        regs = {name: 0 for name, _ in arch.REGISTERS}
        for d in self.dffs:
            r, b = self.ff_reg[d.out]
            regs[r] |= v[d.ins["D"]] << b
        return regs


def load(path: Path) -> Netlist:
    d = json.loads(Path(path).read_text())
    mod = d["modules"]["core"]
    ports = {k: p for k, p in mod["ports"].items()}
    expect_in = {"clk": 1, "rst_n": 1, "rdata": 16}
    expect_out = {"addr": 16, "space": 1, "we": 1, "wdata": 16}
    for k, w in {**expect_in, **expect_out}.items():
        if k not in ports or len(ports[k]["bits"]) != w:
            raise DisciplineError(f"port {k} missing or wrong width")
    inputs = {k: ports[k]["bits"] for k in expect_in}
    outputs = {k: ports[k]["bits"] for k in expect_out}
    clk, rst_n = inputs["clk"][0], inputs["rst_n"][0]

    drivers: dict[object, str] = {}
    gates, dffs = [], []
    for name, c in mod["cells"].items():
        t = c["type"]
        if t not in CELLS:
            raise DisciplineError(f"cell {name} has type {t} outside the library")
        cell = CELLS[t]
        conns = {p: bits[0] for p, bits in c["connections"].items()}
        pins = set(cell.inputs) | {cell.output}
        if set(conns) != pins:
            raise DisciplineError(f"cell {name} ({t}) pins {sorted(conns)} != {sorted(pins)}")
        out = conns[cell.output]
        if out in drivers or out in (CONST0, CONST1):
            raise DisciplineError(f"net {out} has more than one driver")
        drivers[out] = name
        g = Gate(name, t, {p: conns[p] for p in cell.inputs}, out)
        if cell.sequential:
            if g.ins["CK"] != clk or g.ins["RN"] != rst_n:
                raise DisciplineError(f"flip-flop {name} not on the single clock and reset")
            dffs.append(g)
        else:
            if clk in g.ins.values() or rst_n in g.ins.values():
                raise DisciplineError(f"gate {name} uses clock or reset as data")
            gates.append(g)
    for n in inputs["rdata"] + [clk, rst_n]:
        if n in drivers:
            raise DisciplineError("primary input driven by a cell")

    # registers: every flip-flop must be a named register bit
    ff_reg = {}
    by_q = {g.out: g for g in dffs}
    for r, w in arch.REGISTERS:
        bits = mod["netnames"][r]["bits"]
        if len(bits) != w:
            raise DisciplineError(f"register {r} has {len(bits)} bits, expected {w}")
        for b, net in enumerate(bits):
            if net not in by_q:
                raise DisciplineError(f"register bit {r}[{b}] is not a flip-flop output")
            ff_reg[net] = (r, b)
    if len(ff_reg) != len(dffs):
        raise DisciplineError(f"{len(dffs)} flip-flops but {len(ff_reg)} register bits")
    order = {net: i for i, net in enumerate(n for r, _ in arch.REGISTERS for n in mod["netnames"][r]["bits"])}
    dffs.sort(key=lambda g: order[g.out])

    # levelize (fails on combinational loops)
    level = {CONST0: 0, CONST1: 0, clk: 0, rst_n: 0}
    for n in inputs["rdata"]:
        level[n] = 0
    for g in dffs:
        level[g.out] = 0
    pending = {g.out: g for g in gates}
    users: dict[object, list[Gate]] = {}
    for g in gates:
        for n in g.ins.values():
            if n not in level and n not in pending:
                raise DisciplineError(f"gate {g.name} input net {n} has no driver")
            users.setdefault(n, []).append(g)
    indeg = {g.name: sum(1 for n in g.ins.values() if n in pending) for g in gates}
    ready = [g for g in gates if indeg[g.name] == 0]
    ordered = []
    while ready:
        g = ready.pop()
        g.level = 1 + max(level[n] for n in g.ins.values())
        level[g.out] = g.level
        ordered.append(g)
        for u in users.get(g.out, []):
            indeg[u.name] -= 1
            if indeg[u.name] == 0:
                ready.append(u)
    if len(ordered) != len(gates):
        raise DisciplineError("combinational loop")
    ordered.sort(key=lambda g: (g.level, g.name))
    for g in dffs:
        if g.ins["D"] not in level:
            raise DisciplineError(f"flip-flop {g.name} D has no driver")
    for k, nets in outputs.items():
        for n in nets:
            if n not in level:
                raise DisciplineError(f"output {k} bit has no driver")
    nl = Netlist(ordered, dffs, ff_reg, inputs, outputs, clk, rst_n)
    nl.nets = sorted(level, key=lambda n: (isinstance(n, str), n if isinstance(n, int) else 0, str(n)))
    return nl
