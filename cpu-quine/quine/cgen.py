"""Generate the C models of the core from arch.py and the gate netlist.

build/qmodel.h  -- both steppers, emitted from arch.CONTROL:
    qm_cycle()   microcycle stepper: exact flip-flop vector after every cycle
    QM_RUN body  instruction stepper: unrolled per opcode, exact at boundaries
build/qgates.h  -- gate-level expansion in levelized order (from the netlist)

The includer of qmodel.h's QM_RUN section defines the memory macros
QM_READ_ROM(a), QM_READ_RAM(a), QM_WRITE(a, v) and the stop hook.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import arch, netlist

COND_EXPR = [
    "1",
    "(F & 1)",
    "(~F & 1)",
    "((F >> 2) & 1)",
    "((~F >> 2) & 1)",
    "((F >> 1) & 1)",
    "((~F >> 1) & 1)",
    "(X != 0)",
]
OPMASK = sum(1 << arch.OP5.index(n) for n in arch.OPERAND_OP5)


def uses_rd(c: dict) -> bool:
    """Does this control word consume RDATA (so the previous read is live)?"""
    if c["ir"] or c["pc"] == "INC_OP":
        return True
    if c["r"] != "RD":
        return False
    return bool(c["dst"] or c["zn"] or c["cf"] == "ALU" or c["adr"] in ("ALU", "BR") or c["pc"] in ("ALU", "BR"))


def alu_c(op: str) -> list[str]:
    if op == "PASS":
        return ["res_ = L_; cout_ = 0;"]
    if op in ("ADD", "ADC", "SUB", "SBC"):
        r = "R_" if op in ("ADD", "ADC") else "(uint16_t)~R_"
        cin = {"ADD": "0", "ADC": "((F >> 2) & 1)", "SUB": "1", "SBC": "((F >> 2) & 1)"}[op]
        return [f"{{ uint32_t s_ = (uint32_t)L_ + {r} + {cin}; res_ = (uint16_t)s_; cout_ = s_ >> 16; }}"]
    return {
        "AND": ["res_ = L_ & R_; cout_ = 0;"],
        "OR": ["res_ = L_ | R_; cout_ = 0;"],
        "XOR": ["res_ = L_ ^ R_; cout_ = 0;"],
        "SHL": ["res_ = (uint16_t)(L_ << 1); cout_ = L_ >> 15;"],
        "SHR": ["res_ = L_ >> 1; cout_ = L_ & 1;"],
        "ROL": ["res_ = (uint16_t)((L_ << 1) | ((F >> 2) & 1)); cout_ = L_ >> 15;"],
        "ROR": ["res_ = (uint16_t)((L_ >> 1) | (((F >> 2) & 1) << 15)); cout_ = L_ & 1;"],
        "SWAB": ["res_ = (uint16_t)((L_ << 8) | (L_ >> 8)); cout_ = 0;"],
    }[op]


def emit_cycle(c: dict, cond_expr: str | None, read: str, memops: bool, ncyc: int = 0) -> list[str]:
    """C statements for one cycle with control word c.

    read: 'always' | 'never' | 'live' -- whether to perform the read of this cycle.
    memops: perform memory read/write via QM_* macros (instruction stepper),
            else just set the bus outputs (microcycle stepper).
    ncyc: cycles of the instruction completed at the end of this cycle.
    """
    o = ["{"]
    lv = {"0": "0", "A": "A", "X": "X", "Y": "Y", "PC": "PC", "SP": "SP"}[c["l"]]
    rv = {"RD": "rd", "ONE": "1", "OFF": "(uint16_t)((IR & 0x200) ? (IR | 0xFC00) : (IR & 0x3FF))"}[c["r"]]
    o.append(f"  uint16_t L_ = {lv}, R_ = {rv}, res_; unsigned cout_;")
    o.append("  (void)R_; (void)cout_; (void)res_;")
    o += ["  " + s for s in alu_c(c["alu"])]
    need_cond = c["adr"] == "BR" or c["pc"] == "BR"
    cond = cond_expr if cond_expr else "qm_cond(IR, F, X)"
    if need_cond:
        o.append(f"  unsigned cond_ = {cond};")
    adr = {
        "PC": "PC",
        "ALU": "res_",
        "STK": f"(uint16_t)({arch.STACK_BASE:#x} | SP)",
        "STK1": f"(uint16_t)({arch.STACK_BASE:#x} | (uint8_t)(SP + 1))",
        "BR": "(cond_ ? res_ : PC)",
    }[c["adr"]]
    wd = {"A": "A", "X": "X", "Y": "Y", "PC": "PC"}[c["wd"]]
    space = arch.FIELDS["space"].index(c["space"])
    o.append(f"  uint16_t addr_ = {adr};")
    if memops:
        if read == "always" or read == "live":
            src = "QM_READ_ROM(addr_)" if space == 0 else "QM_READ_RAM(addr_)"
            o.append(f"  uint16_t nrd_ = {src};")
        else:
            o.append("  uint16_t nrd_ = rd;  /* read result is dead */")
        if c["we"] and c["space"] == "RAM":
            o.append(f"  QM_WRITE(addr_, {wd}, {ncyc});")
    else:
        o.append(f"  bus->addr = addr_; bus->space = {space}; bus->we = {c['we']}; bus->wdata = {wd};")
    if c["dst"] == "SP":
        o.append("  SP = (uint8_t)res_;")
    elif c["dst"]:
        o.append(f"  {c['dst']} = res_;")
    if c["zn"]:
        o.append("  F = (uint8_t)((F & 4) | (res_ == 0) | ((res_ >> 15) << 1));")
    if c["cf"] == "ALU":
        o.append("  F = (uint8_t)((F & 3) | (cout_ << 2));")
    elif c["cf"] == "CLR":
        o.append("  F &= 3;")
    elif c["cf"] == "SET":
        o.append("  F |= 4;")
    if c["sp"] == "DEC":
        o.append("  SP = (uint8_t)(SP - 1);")
    elif c["sp"] == "INC":
        o.append("  SP = (uint8_t)(SP + 1);")
    if c["pc"] == "INC":
        o.append("  PC = (PC + 1) & 0x7FFF;")
    elif c["pc"] == "INC_OP":
        o.append(f"  if ((rd & 0x8000) || (({OPMASK:#x}u >> ((rd >> 10) & 31)) & 1)) PC = (PC + 1) & 0x7FFF;")
    elif c["pc"] == "ALU":
        o.append("  PC = res_ & 0x7FFF;")
    elif c["pc"] == "BR":
        o.append("  if (cond_) PC = res_ & 0x7FFF;")
    if c["ir"]:
        o.append("  IR = rd;")
    if memops:
        o.append("  rd = nrd_;")
    o.append("}")
    return o


def gen_model() -> str:
    o = []
    w = o.append
    w("/* GENERATED by quine/cgen.py from quine/arch.py -- do not edit. */")
    w("#ifndef QMODEL_H")
    w("#define QMODEL_H")
    w("#include <stdint.h>")
    w("typedef struct { uint16_t A, X, Y, PC, IR, rd; uint8_t SP, MU, F; } qstate;")
    w("typedef struct { uint16_t addr, wdata; uint8_t space, we; } qbus;")
    w(f"#define QM_ROM_WORDS {arch.ROM_WORDS}")
    w(f"#define QM_RAM_WORDS {arch.RAM_WORDS}")
    w(f"#define QM_IO_DEBUG {arch.IO_DEBUG:#x}")
    w(f"#define QM_IO_RESET {arch.IO_RESET:#x}")
    w(f"#define QM_RESET_CLEAR_CYCLES {arch.RESET_CLEAR_CYCLES}")
    w("static inline unsigned qm_cond(uint16_t IR, uint8_t F, uint16_t X) {")
    w("  switch ((IR >> 10) & 7) {")
    for i, e in enumerate(COND_EXPR):
        w(f"  case {i}: return {e};")
    w("  }")
    w("  return 0;")
    w("}")
    w("static const uint8_t qm_cycles[256] = {" + ",".join(str(arch.cycles(h)) for h in range(256)) + "};")
    names = []
    for h in range(256):
        i = arch.decode(h)
        names.append(
            i.name
            + (f" {i.mode}{'' if i.mode == 'imm' else (' rom' if i.space == 0 else ' ram')}" if i.kind == "mem" else "")
        )
    w("static const char *const qm_names[256] = {" + ",".join(f'"{n}"' for n in names) + "};")
    # microcycle stepper
    w("/* Microcycle stepper: one clock cycle; returns the bus of that cycle.  The")
    w("   caller performs the memory read (read-first) and write, then sets s->rd. */")
    w("static void qm_cycle(qstate *s, uint16_t rd, qbus *bus) {")
    w("  uint16_t A = s->A, X = s->X, Y = s->Y, PC = s->PC, IR = s->IR;")
    w("  uint8_t SP = s->SP, F = s->F, MU = s->MU;")
    w("  switch (MU) {")
    for mu, c in ((0, arch.U_FETCH), (1, arch.U_DECODE)):
        w(f"  case {mu}:")
        o.extend("    " + x for x in emit_cycle(c, None, "never", False))
        w(f"    MU = {c['mu']};")
        w("    break;")
    w("  default:")
    w("    switch (((MU & 1) << 8) | (IR >> 8)) {")
    for mu in (2, 3):
        for hb in range(256):
            c = arch.CONTROL[hb, mu]
            w(f"    case {((mu & 1) << 8) | hb:#x}:")
            o.extend("      " + x for x in emit_cycle(c, COND_EXPR[(hb >> 2) & 7], "never", False))
            w(f"      MU = {c['mu']};")
            w("      break;")
    w("    }")
    w("  }")
    w("  s->A = A; s->X = X; s->Y = Y; s->PC = PC; s->IR = IR; s->SP = SP; s->F = F; s->MU = MU;")
    w("}")
    w("#endif")
    # instruction stepper
    w("")
    w("#ifdef QM_RUN_BODY")
    w("/* Instruction stepper body.  Expects locals A,X,Y,PC,IR,rd (uint16_t), SP,F (uint8_t)")
    w("   and 'cyc' (uint64_t).  Executes one whole instruction starting at MU=0;")
    w("   QM_WRITE may 'goto qm_abort' (self-reset).  */")
    w("{")
    o.extend("  " + x for x in emit_cycle(arch.U_FETCH, None, "always", True))
    o.extend("  " + x for x in emit_cycle(arch.U_DECODE, None, "live", True))
    w("  switch (IR >> 8) {")
    for hb in range(256):
        prog = arch.program(hb)
        w(f"  case {hb:#x}: /* {names[hb]} */")
        for i, c in enumerate(prog):
            last = i == len(prog) - 1
            read = "always" if last else ("live" if uses_rd(prog[i + 1]) else "never")
            o.extend("    " + x for x in emit_cycle(c, COND_EXPR[(hb >> 2) & 7], read, True, 3 + i))
        w(f"    cyc += {arch.cycles(hb)};")
        w("    break;")
    w("  }")
    w("}")
    w("#endif")
    return "\n".join(o) + "\n"


def gen_gates(nl: netlist.Netlist, slots: dict) -> str:
    """Levelized gate evaluation over a byte-per-net array, plus net metadata."""
    idx = slots["index"]  # net -> bit index in the net-state bitset
    nn = slots["nbits"]
    o = ["/* GENERATED by quine/cgen.py from build/core_gates.json -- do not edit. */"]
    o.append("#ifndef QGATES_H\n#define QGATES_H\n#include <stdint.h>")
    o.append(f"#define QG_NBITS {nn}")
    o.append(f"#define QG_NWORDS {(nn + 15) // 16}")
    o.append(f"#define QG_NGATES {len(nl.gates)}")
    o.append(f"#define QG_NDFF {len(nl.dffs)}")
    o.append(f"#define QG_CONST1 {idx[netlist.CONST1]}")
    o.append(f"#define QG_CONST0 {idx[netlist.CONST0]}")
    expr = {
        "INV": "!{A}",
        "NAND2": "!({A} & {B})",
        "NAND3": "!({A} & {B} & {C})",
        "NOR2": "!({A} | {B})",
        "NOR3": "!({A} | {B} | {C})",
        "AOI21": "!(({A1} & {A2}) | {B})",
        "OAI21": "!(({A1} | {A2}) & {B})",
        "XNOR2": "!({A} ^ {B})",
        "MUX2": "({S} ? {B} : {A})",
    }
    regs = dict(arch.REGISTERS)
    o.append("/* n[] holds one byte per net (0/1), indexed by bitset position. */")
    o.append("static void qg_eval(uint8_t *n, const uint16_t *regs, uint16_t rdata) {")
    o.append(f"  n[QG_CONST0] = 0; n[QG_CONST1] = 1; n[{idx[nl.clk]}] = 0; n[{idx[nl.rst_n]}] = 1;")
    for i, net in enumerate(nl.inputs["rdata"]):
        o.append(f"  n[{idx[net]}] = (rdata >> {i}) & 1;")
    for q, (r, b) in nl.ff_reg.items():
        o.append(f"  n[{idx[q]}] = (regs[{[n for n, _ in arch.REGISTERS].index(r)}] >> {b}) & 1;")
    for g in nl.gates:
        e = expr[g.type].format(**{p: f"n[{idx[net]}]" for p, net in g.ins.items()})
        o.append(f"  n[{idx[g.out]}] = {e};")
    o.append("}")
    o.append("/* next-state register values from the D nets */")
    o.append("static void qg_next(const uint8_t *n, uint16_t *regs) {")
    o.append(f"  for (int i = 0; i < {len(regs)}; i++) regs[i] = 0;")
    for d in nl.dffs:
        r, b = nl.ff_reg[d.out]
        o.append(f"  regs[{[n for n, _ in arch.REGISTERS].index(r)}] |= (uint16_t)(n[{idx[d.ins['D']]}] << {b});")
    o.append("}")
    for port in ("addr", "wdata"):
        o.append(f"static const uint16_t qg_{port}[16] = {{{', '.join(str(idx[x]) for x in nl.outputs[port])}}};")
    o.append(f"#define QG_SPACE {idx[nl.outputs['space'][0]]}")
    o.append(f"#define QG_WE {idx[nl.outputs['we'][0]]}")
    o.append("#endif")
    return "\n".join(o) + "\n"


def main(argv):
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "qmodel.h").write_text(gen_model())


if __name__ == "__main__":
    main(sys.argv)


def cell_k_inputs(cell_name: str, g) -> list:
    """Nets that form a cell's input-combination index k (bit i = i-th net)."""
    from .cells import CELLS

    c = CELLS[cell_name]
    if c.sequential:
        return [g.ins["D"], g.out]  # k = D | Q<<1 | CK<<2
    return [g.ins[p] for p in c.inputs]


def gen_switch(nl: netlist.Netlist, sl: dict, patterns: dict) -> str:
    """Transistor netlist and expected per-transistor states for check 3."""
    from .cells import CELLS

    idx = sl["index"]
    types = list(CELLS)
    # nodes: 0 GND, 1 VDD, 2..17 rdata, 18 clk, 19 rst_n, then one node per net, then internals
    node = {"GND": 0, "VDD": 1}
    for i, n in enumerate(nl.inputs["rdata"]):
        node[("net", n)] = 2 + i
    node[("net", nl.clk)] = 18
    node[("net", nl.rst_n)] = 19
    node[("net", netlist.CONST0)] = 0
    node[("net", netlist.CONST1)] = 1
    nnodes = 20
    for n in sorted(idx, key=lambda x: idx[x]):
        if ("net", n) not in node:
            node[("net", n)] = nnodes
            nnodes += 1
    trans = []
    cellrows = []
    for ci, g in enumerate(nl.cells):
        c = CELLS[g.type]
        pins = dict(g.ins)
        pins[c.output] = g.out
        first = len(trans)
        for k, gate, a, b in c.transistors:
            ids = []
            for x in (gate, a, b):
                if x in ("VDD", "GND"):
                    ids.append(node[x])
                elif x in pins:
                    ids.append(node[("net", pins[x])])
                else:
                    key = ("int", ci, x)
                    if key not in node:
                        node[key] = nnodes
                        nnodes += 1
                    ids.append(node[key])
            trans.append((1 if k == "N" else 0, *ids))
        kin = [idx[n] for n in cell_k_inputs(g.type, g)]
        cellrows.append((types.index(g.type), first, len(c.transistors), kin))
    o = ["/* GENERATED by quine/cgen.py -- transistor netlist for the switch-level check */"]
    o.append("#ifndef QSWITCH_H\n#define QSWITCH_H\n#include <stdint.h>")
    o.append(f"#define QS_NNODES {nnodes}\n#define QS_NTRANS {len(trans)}\n#define QS_NCELLS {len(cellrows)}")
    o.append("#define QS_GND 0\n#define QS_VDD 1\n#define QS_RDATA0 2\n#define QS_CLK 18\n#define QS_RSTN 19")
    o.append("/* kind (1 = NMOS), gate, a, b */")
    o.append("static const uint16_t qs_trans[QS_NTRANS][4] = {")
    o.extend(f"  {{{k},{gt},{a},{b}}}," for k, gt, a, b in trans)
    o.append("};")
    o.append("/* net bitset index -> switch node */")
    o.append(f"static const uint16_t qs_net_node[{sl['nbits']}] = {{")
    inv = {i: node[("net", n)] for n, i in idx.items()}
    o.append("  " + ",".join(str(inv.get(i, 0)) for i in range(sl["nbits"])))
    o.append("};")
    o.append("/* cells: type, first transistor, count, k-input net indices (up to 3) */")
    o.append("static const uint16_t qs_cells[QS_NCELLS][6] = {")
    for t, f, n, kin in cellrows:
        kk = (kin + [0, 0, 0])[:3]
        o.append(f"  {{{t},{f},{n},{kk[0]},{kk[1]},{kk[2]}}},")
    o.append("};")
    o.append(
        f"static const uint8_t qs_type_nk[{len(types)}] = {{"
        + ",".join(str(2 if CELLS[t].sequential else len(CELLS[t].inputs)) for t in types)
        + "};"
    )
    o.append(
        f"static const uint8_t qs_type_dff[{len(types)}] = {{"
        + ",".join(str(int(CELLS[t].sequential)) for t in types)
        + "};"
    )
    o.append("/* expected conduction per type and k (DFF: k = D | Q<<1 | CK<<2) */")
    o.append(f"static const uint32_t qs_pattern[{len(types)}][8] = {{")
    for t in types:
        st = patterns[t]["states"]
        masks = [sum(b << i for i, b in enumerate(s)) for s in st] + [0] * (8 - len(st))
        o.append("  {" + ",".join(f"{m:#x}u" for m in masks) + f"}},  /* {t} */")
    o.append("};")
    o.append("#endif")
    return "\n".join(o) + "\n"
