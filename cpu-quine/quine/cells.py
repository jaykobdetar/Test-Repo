"""The ten-cell static CMOS library.

Each cell has a Boolean model and one fixed transistor schematic.  The
schematics use only complementary static gates, except DFFR, whose master
and slave latches use transmission gates.  Every cell drives its output for
every input combination.

Transistors are (kind, gate, a, b) with kind 'P' or 'N'.  Nodes are pin
names, 'VDD', 'GND', or cell-internal names.  A transistor conducts when its
gate node is low (P) or high (N).

DFFR is the six-NAND edge-triggered flip-flop.  Its internal latches lock
two gate delays after the rising edge, before a new Q can reach any D input,
so edge triggering is exact under the reference solver's unit-delay wave
ordering.  switchsim.check_dff verifies this exhaustively.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Cell:
    name: str
    inputs: tuple[str, ...]
    output: str
    liberty: str  # Liberty function string (combinational cells)
    fn: object  # python: inputs dict -> output bit (combinational cells)
    transistors: tuple[tuple[str, str, str, str], ...] = field(repr=False)
    sequential: bool = False

    @property
    def size(self) -> int:
        return len(self.transistors)


def _inv(a, y):
    return [("P", a, "VDD", y), ("N", a, y, "GND")]


def _nand(ins, y, tag):
    t = [("P", i, "VDD", y) for i in ins]
    prev = y
    for k, i in enumerate(ins):
        nxt = "GND" if k == len(ins) - 1 else f"{tag}{k}"
        t.append(("N", i, prev, nxt))
        prev = nxt
    return t


def _nor(ins, y, tag):
    t = []
    prev = "VDD"
    for k, i in enumerate(ins):
        nxt = y if k == len(ins) - 1 else f"{tag}{k}"
        t.append(("P", i, prev, nxt))
        prev = nxt
    t += [("N", i, y, "GND") for i in ins]
    return t


def _aoi22(a, b, c, d, y, tag):
    # y = !((a & b) | (c & d))
    return [
        ("P", a, "VDD", f"{tag}p"),
        ("P", b, "VDD", f"{tag}p"),
        ("P", c, f"{tag}p", y),
        ("P", d, f"{tag}p", y),
        ("N", a, y, f"{tag}n0"),
        ("N", b, f"{tag}n0", "GND"),
        ("N", c, y, f"{tag}n1"),
        ("N", d, f"{tag}n1", "GND"),
    ]


CELLS: dict[str, Cell] = {}


def _add(c: Cell):
    CELLS[c.name] = c


_add(Cell("INV", ("A",), "Y", "!A", lambda v: 1 - v["A"], tuple(_inv("A", "Y"))))
_add(Cell("NAND2", ("A", "B"), "Y", "!(A&B)", lambda v: 1 - (v["A"] & v["B"]), tuple(_nand(["A", "B"], "Y", "n"))))
_add(
    Cell(
        "NAND3",
        ("A", "B", "C"),
        "Y",
        "!(A&B&C)",
        lambda v: 1 - (v["A"] & v["B"] & v["C"]),
        tuple(_nand(["A", "B", "C"], "Y", "n")),
    )
)
_add(Cell("NOR2", ("A", "B"), "Y", "!(A|B)", lambda v: 1 - (v["A"] | v["B"]), tuple(_nor(["A", "B"], "Y", "p"))))
_add(
    Cell(
        "NOR3",
        ("A", "B", "C"),
        "Y",
        "!(A|B|C)",
        lambda v: 1 - (v["A"] | v["B"] | v["C"]),
        tuple(_nor(["A", "B", "C"], "Y", "p")),
    )
)
_add(
    Cell(
        "AOI21",
        ("A1", "A2", "B"),
        "Y",
        "!((A1&A2)|B)",
        lambda v: 1 - ((v["A1"] & v["A2"]) | v["B"]),
        (
            ("P", "A1", "VDD", "p"),
            ("P", "A2", "VDD", "p"),
            ("P", "B", "p", "Y"),
            ("N", "A1", "Y", "n"),
            ("N", "A2", "n", "GND"),
            ("N", "B", "Y", "GND"),
        ),
    )
)
_add(
    Cell(
        "OAI21",
        ("A1", "A2", "B"),
        "Y",
        "!((A1|A2)&B)",
        lambda v: 1 - ((v["A1"] | v["A2"]) & v["B"]),
        (
            ("P", "A1", "VDD", "p"),
            ("P", "A2", "p", "Y"),
            ("P", "B", "VDD", "Y"),
            ("N", "A1", "n", "GND"),
            ("N", "A2", "n", "GND"),
            ("N", "B", "Y", "n"),
        ),
    )
)
_add(
    Cell(
        "XNOR2",
        ("A", "B"),
        "Y",
        "!(A^B)",
        lambda v: 1 - (v["A"] ^ v["B"]),
        tuple(_inv("A", "AN") + _inv("B", "BN") + _aoi22("A", "BN", "AN", "B", "Y", "x")),
    )
)
_add(
    Cell(
        "MUX2",
        ("A", "B", "S"),
        "Y",
        "(A&!S)|(B&S)",
        lambda v: v["B"] if v["S"] else v["A"],
        tuple(_inv("S", "SN") + _aoi22("A", "SN", "B", "S", "M", "x") + _inv("M", "Y")),
    )
)
_add(
    Cell(
        "DFFR",
        ("D", "CK", "RN"),
        "Q",
        "",
        None,
        # Positive-edge D flip-flop with asynchronous active-low clear, built
        # from six static NAND gates (the classic three-latch edge-triggered
        # structure).  No transmission gates, so no node can ever be driven
        # both ways.
        tuple(
            _nand(["s4", "s2"], "s1", "a")
            + _nand(["s1", "CK", "RN"], "s2", "b")
            + _nand(["s2", "CK", "s4"], "s3", "c")
            + _nand(["s3", "D", "RN"], "s4", "d")
            + _nand(["s2", "QN"], "Q", "e")
            + _nand(["Q", "s3", "RN"], "QN", "f")
        ),
        sequential=True,
    )
)

COMB = [c for c in CELLS.values() if not c.sequential]


def gate_nodes(cell: Cell) -> list[str]:
    """Distinct gate nodes of a cell's transistors, in first-use order."""
    seen = []
    for _k, g, _a, _b in cell.transistors:
        if g not in seen:
            seen.append(g)
    return seen


def liberty() -> str:
    o = [
        "library(quine10) {",
        "  delay_model : table_lookup;",
        '  time_unit : "1ns";',
        "  capacitive_load_unit (1, pf);",
    ]
    for c in CELLS.values():
        o.append(f"  cell({c.name}) {{")
        o.append(f"    area : {c.size};")
        if c.sequential:
            o.append('    ff(IQ, IQN) { clocked_on : "CK"; next_state : "D"; clear : "!RN"; }')
            o.append("    pin(D) { direction : input; }")
            o.append("    pin(CK) { direction : input; clock : true; }")
            o.append("    pin(RN) { direction : input; }")
            o.append('    pin(Q) { direction : output; function : "IQ"; }')
        else:
            for i in c.inputs:
                o.append(f"    pin({i}) {{ direction : input; }}")
            o.append(f'    pin({c.output}) {{ direction : output; function : "{c.liberty}"; }}')
        o.append("  }")
    o.append("}")
    return "\n".join(o) + "\n"


def verilog_models() -> str:
    """Behavioural Verilog for each cell, for gate-level simulation and equivalence."""
    expr = {
        "INV": "~A",
        "NAND2": "~(A & B)",
        "NAND3": "~(A & B & C)",
        "NOR2": "~(A | B)",
        "NOR3": "~(A | B | C)",
        "AOI21": "~((A1 & A2) | B)",
        "OAI21": "~((A1 | A2) & B)",
        "XNOR2": "~(A ^ B)",
        "MUX2": "S ? B : A",
    }
    o = ["// GENERATED by quine/cells.py -- behavioural models of the ten cells"]
    for c in CELLS.values():
        if c.sequential:
            o += [
                "module DFFR(input D, input CK, input RN, output reg Q);",
                "    always @(posedge CK or negedge RN) if (!RN) Q <= 1'b0; else Q <= D;",
                "endmodule",
            ]
        else:
            ports = ", ".join(f"input {i}" for i in c.inputs)
            o += [
                f"module {c.name}({ports}, output {c.output});",
                f"    assign {c.output} = {expr[c.name]};",
                "endmodule",
            ]
    return "\n".join(o) + "\n"


if __name__ == "__main__":
    import sys
    from pathlib import Path

    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "cells.lib").write_text(liberty())
    (out / "cells_sim.v").write_text(verilog_models())
    for c in CELLS.values():
        print(f"{c.name:6s} {c.size:3d} transistors")
