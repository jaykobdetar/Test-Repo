"""Microarchitecture description of the CPU-Quine core.

This file is the single source of truth.  The RTL generator, the C steppers,
the assembler, the ROM tables and the reference model below are all derived
from the definitions here; nothing else restates the instruction set.

Core summary
------------
* 16-bit words, word-addressed.  Separate ROM and RAM spaces of 32K words.
* Registers (all reset to zero): A, X, Y (16), SP (8), PC (15), IR (16),
  MU (2-bit microstate) and F (3 flags: bit 0 Z, bit 1 N, bit 2 C).
* Memory is synchronous: the address driven during cycle t is registered at
  the rising edge that ends cycle t, and the data appear on RDATA during
  cycle t+1.  The memory reads every cycle (read-first on a write).
* Every instruction ends with a cycle that drives the address of the next
  instruction in ROM space, so at every instruction boundary
  RDATA == ROM[PC].  The instruction-level model relies on that invariant
  and the checks verify it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Machine constants
# --------------------------------------------------------------------------

WORD_MASK = 0xFFFF
ROM_WORDS = 0x8000  # ROM reads use address bits [14:0]
RAM_WORDS = 0x8000  # RAM occupies 0x0000-0x7FFF; reads above return zero
PAGE_WORDS = 256
RAM_PAGES = RAM_WORDS // PAGE_WORDS
IO_DEBUG = 0xFFFE  # write-only: debug/hash port
IO_RESET = 0xFFFF  # write-only: self-reset
STACK_BASE = 0x0200  # stack page: push writes 0x0200+SP, then SP -= 1
RESET_CLEAR_CYCLES = 32768  # reset controller clears RAM before release

SPACE_ROM = 0
SPACE_RAM = 1

# Registers in flip-flop order: (name, width).  Every flip-flop resets to 0.
REGISTERS = [
    ("A", 16),
    ("X", 16),
    ("Y", 16),
    ("SP", 8),
    ("PC", 15),
    ("IR", 16),
    ("MU", 2),
    ("F", 3),  # F[0]=Z, F[1]=N, F[2]=C
]
REG_WIDTH = dict(REGISTERS)
FLAG_Z, FLAG_N, FLAG_C = 1, 2, 4

# --------------------------------------------------------------------------
# Instruction encoding
# --------------------------------------------------------------------------
# Memory-mode instructions:  1 oooo mm s  0000 0000   + one operand word
#   oooo = op (MEM_OPS), mm = mode (MODES), s = space (0 ROM, 1 RAM)
# Other instructions:        0 ppppp aaaaaaaaaa      [+ operand for JMP/JSR/JMPX]
#   ppppp = op (OP5), a = 10-bit signed branch offset relative to the
#   address after the opcode word.

MEM_OPS = ["LDA", "LDX", "LDY", "STA", "STX", "STY", "ADD", "ADC",
           "SUB", "SBC", "AND", "OR", "XOR", "CMP", "CPX", "CPY"]  # fmt: skip
MODES = ["imm", "abs", "absx", "absy"]
OP5 = [
    "BRA", "BEQ", "BNE", "BCS", "BCC", "BMI", "BPL", "DBNE",
    "SHL", "SHR", "ROL", "ROR", "SWAB", "TAX", "TAY", "TXA",
    "TYA", "TSX", "TXS", "INX", "INY", "DEX", "DEY", "CLC",
    "SEC", "PHA", "PLA", "RTS", None, "JMP", "JSR", "JMPX",
]  # fmt: skip
BRANCHES = OP5[:8]
OPERAND_OP5 = ("JMP", "JSR", "JMPX")
STORES = ("STA", "STX", "STY")
LOADS = ("LDA", "LDX", "LDY")
COMPARES = ("CMP", "CPX", "CPY")
ARITH = ("ADD", "ADC", "SUB", "SBC")
LOGIC = ("AND", "OR", "XOR")
REG_OF = {"LDA": "A", "LDX": "X", "LDY": "Y", "STA": "A", "STX": "X", "STY": "Y",
          "CMP": "A", "CPX": "X", "CPY": "Y"}  # fmt: skip


def encode_mem(op: str, mode: str, space: int) -> int:
    return 0x8000 | MEM_OPS.index(op) << 11 | MODES.index(mode) << 9 | space << 8


def encode_op5(op: str, arg10: int = 0) -> int:
    return OP5.index(op) << 10 | (arg10 & 0x3FF)


def has_operand(word: int) -> bool:
    """Decided from the opcode word in the decode cycle (MU=1)."""
    return bool(word & 0x8000) or OP5[(word >> 10) & 31] in OPERAND_OP5


@dataclass(frozen=True)
class OpInfo:
    hb: int  # opcode high byte, the dispatch index
    name: str  # mnemonic, or "ILL" for undefined encodings
    kind: str  # mem | branch | implied | jump | illegal
    mode: str | None = None
    space: int | None = None


def decode(hb: int) -> OpInfo:
    if hb & 0x80:
        op = MEM_OPS[(hb >> 3) & 15]
        return OpInfo(hb, op, "mem", MODES[(hb >> 1) & 3], hb & 1)
    op = OP5[(hb >> 2) & 31]
    if op is None:
        return OpInfo(hb, "ILL", "illegal")
    if op in BRANCHES:
        return OpInfo(hb, op, "branch")
    if op in OPERAND_OP5:
        return OpInfo(hb, op, "jump")
    return OpInfo(hb, op, "implied")


# --------------------------------------------------------------------------
# ALU
# --------------------------------------------------------------------------

ALU_OPS = ["PASS", "ADD", "ADC", "SUB", "SBC", "AND", "OR", "XOR",
           "SHL", "SHR", "ROL", "ROR", "SWAB"]  # fmt: skip


def alu(op: str, lhs: int, r: int, c: int) -> tuple[int, int]:
    """Return (result, carry_out).  Carry out is 0 for non-arithmetic ops."""
    if op == "PASS":
        return lhs, 0
    if op in ("ADD", "ADC", "SUB", "SBC"):
        rr = r if op in ("ADD", "ADC") else (~r) & WORD_MASK
        cin = {"ADD": 0, "ADC": c, "SUB": 1, "SBC": c}[op]
        s = lhs + rr + cin
        return s & WORD_MASK, s >> 16
    if op == "AND":
        return lhs & r, 0
    if op == "OR":
        return lhs | r, 0
    if op == "XOR":
        return lhs ^ r, 0
    if op == "SHL":
        return (lhs << 1) & WORD_MASK, lhs >> 15
    if op == "SHR":
        return lhs >> 1, lhs & 1
    if op == "ROL":
        return ((lhs << 1) | c) & WORD_MASK, lhs >> 15
    if op == "ROR":
        return (lhs >> 1) | (c << 15), lhs & 1
    if op == "SWAB":
        return ((lhs & 0xFF) << 8) | (lhs >> 8), 0
    raise ValueError(op)


# --------------------------------------------------------------------------
# Microcode
# --------------------------------------------------------------------------
# A control word says, for one cycle, what drives the memory bus and what
# every register loads at the rising edge that ends the cycle.

FIELDS = {
    # name: allowed values (index = RTL encoding)
    "adr": ["PC", "ALU", "STK", "STK1", "BR"],  # STK={02,SP} STK1={02,SP+1} BR=cond?ALU:PC
    "space": ["ROM", "RAM"],
    "we": [0, 1],
    "wd": ["A", "X", "Y", "PC"],
    "alu": ALU_OPS,
    "l": ["0", "A", "X", "Y", "PC", "SP"],
    "r": ["RD", "ONE", "OFF"],  # OFF = sign-extended IR[9:0]
    "dst": [None, "A", "X", "Y", "SP"],
    "zn": [0, 1],
    "cf": [None, "ALU", "CLR", "SET"],
    "sp": [None, "DEC", "INC"],
    "pc": [None, "INC", "INC_OP", "ALU", "BR"],  # INC_OP: +1 if RDATA has an operand
    "ir": [0, 1],
    "mu": [0, 1, 2, 3],
}
DEFAULT = {"adr": "PC", "space": "ROM", "we": 0, "wd": "A", "alu": "PASS", "l": "0", "r": "RD",
           "dst": None, "zn": 0, "cf": None, "sp": None, "pc": None, "ir": 0, "mu": 0}  # fmt: skip


def ctrl(**kw) -> dict:
    c = dict(DEFAULT)
    for k, v in kw.items():
        if k not in FIELDS or v not in FIELDS[k]:
            raise ValueError(f"bad control {k}={v!r}")
        c[k] = v
    return c


U_FETCH = ctrl(adr="PC", pc="INC", mu=1)  # MU=0: drive PC, PC += 1
U_DECODE = ctrl(adr="PC", ir=1, pc="INC_OP", mu=2)  # MU=1: IR <= RDATA
NOP = ctrl()  # drive PC, back to MU=0


def program(hb: int) -> list[dict]:
    """Control words for MU=2.. of the instruction whose high byte is hb."""
    info = decode(hb)
    n = info.name
    if info.kind == "mem":
        idx = {"imm": "0", "abs": "0", "absx": "X", "absy": "Y"}[info.mode]
        space = FIELDS["space"][info.space]
        if n in STORES:
            return [
                ctrl(adr="ALU", alu="ADD", l=idx, r="RD", space="RAM", we=1, wd=REG_OF[n], mu=3),
                ctrl(),
            ]
        if n in LOADS:
            ex = dict(alu="ADD", l="0", dst=REG_OF[n], zn=1)
        elif n in COMPARES:
            ex = dict(alu="SUB", l=REG_OF[n], zn=1, cf="ALU")
        elif n in ARITH:
            ex = dict(alu=n, l="A", dst="A", zn=1, cf="ALU")
        else:
            ex = dict(alu=n, l="A", dst="A", zn=1)
        if info.mode == "imm":
            return [ctrl(r="RD", **ex)]
        return [ctrl(adr="ALU", alu="ADD", l=idx, r="RD", space=space, mu=3), ctrl(r="RD", **ex)]
    if info.kind == "branch":
        take = ctrl(alu="ADD", l="PC", r="OFF", adr="BR", pc="BR")
        if n == "DBNE":
            return [ctrl(alu="SUB", l="X", r="ONE", dst="X", mu=3), take]
        return [take]
    if info.kind == "jump":
        if n == "JMP":
            return [ctrl(alu="ADD", l="0", r="RD", pc="ALU", adr="ALU")]
        if n == "JSR":
            return [
                ctrl(adr="STK", space="RAM", we=1, wd="PC", sp="DEC", alu="ADD", l="0", r="RD", pc="ALU", mu=3),
                ctrl(),
            ]
        if n == "JMPX":
            return [
                ctrl(adr="ALU", alu="ADD", l="X", r="RD", space="ROM", mu=3),
                ctrl(alu="ADD", l="0", r="RD", pc="ALU", adr="ALU"),
            ]
    if info.kind == "implied":
        simple = {
            "SHL": dict(alu="SHL", l="A", dst="A", zn=1, cf="ALU"),
            "SHR": dict(alu="SHR", l="A", dst="A", zn=1, cf="ALU"),
            "ROL": dict(alu="ROL", l="A", dst="A", zn=1, cf="ALU"),
            "ROR": dict(alu="ROR", l="A", dst="A", zn=1, cf="ALU"),
            "SWAB": dict(alu="SWAB", l="A", dst="A", zn=1),
            "TAX": dict(l="A", dst="X", zn=1),
            "TAY": dict(l="A", dst="Y", zn=1),
            "TXA": dict(l="X", dst="A", zn=1),
            "TYA": dict(l="Y", dst="A", zn=1),
            "TSX": dict(l="SP", dst="X", zn=1),
            "TXS": dict(l="X", dst="SP"),
            "INX": dict(alu="ADD", l="X", r="ONE", dst="X", zn=1),
            "INY": dict(alu="ADD", l="Y", r="ONE", dst="Y", zn=1),
            "DEX": dict(alu="SUB", l="X", r="ONE", dst="X", zn=1),
            "DEY": dict(alu="SUB", l="Y", r="ONE", dst="Y", zn=1),
            "CLC": dict(cf="CLR"),
            "SEC": dict(cf="SET"),
        }
        if n in simple:
            return [ctrl(**simple[n])]
        if n == "PHA":
            return [ctrl(adr="STK", space="RAM", we=1, wd="A", sp="DEC", mu=3), ctrl()]
        if n == "PLA":
            return [ctrl(adr="STK1", space="RAM", sp="INC", mu=3), ctrl(alu="ADD", l="0", r="RD", dst="A", zn=1)]
        if n == "RTS":
            return [
                ctrl(adr="STK1", space="RAM", sp="INC", mu=3),
                ctrl(alu="ADD", l="0", r="RD", pc="ALU", adr="ALU"),
            ]
    return [NOP]  # illegal encodings behave as a 3-cycle no-op


def control_table() -> dict[tuple[int, int], dict]:
    """Every (hb, MU) pair -> control word.  Unreachable pairs act as NOP."""
    table = {}
    for hb in range(256):
        prog = program(hb)
        for mu in range(4):
            if mu == 0:
                table[hb, mu] = U_FETCH
            elif mu == 1:
                table[hb, mu] = U_DECODE
            else:
                table[hb, mu] = prog[mu - 2] if mu - 2 < len(prog) else NOP
    return table


def cycles(hb: int, taken: bool = False) -> int:
    """Cycles taken by the instruction with this high byte (always fixed)."""
    return 2 + len(program(hb))


CONTROL = control_table()


def _check_microcode():
    for hb in range(256):
        prog = program(hb)
        assert 1 <= len(prog) <= 2, hb
        for i, c in enumerate(prog):
            last = i == len(prog) - 1
            assert c["mu"] == (0 if last else i + 3), (hb, i)
            if last:  # the last cycle drives the next instruction's address in ROM
                assert c["space"] == "ROM" and c["we"] == 0, hb
                assert (c["adr"], c["pc"]) in (("PC", None), ("ALU", "ALU"), ("BR", "BR")), hb
            assert not (c["dst"] == "SP" and c["sp"]), hb


_check_microcode()


# --------------------------------------------------------------------------
# Reference model (slow, obviously correct; the generated models are
# checked against it and against each other)
# --------------------------------------------------------------------------


def sext10(v: int) -> int:
    v &= 0x3FF
    return (v - 0x400 if v & 0x200 else v) & WORD_MASK


def condition(s: dict) -> int:
    f = s["F"]
    z, n, c = f & 1, (f >> 1) & 1, (f >> 2) & 1
    t = (s["IR"] >> 10) & 7
    return [1, z, 1 - z, c, 1 - c, n, 1 - n, int(s["X"] != 0)][t]


@dataclass
class Bus:
    addr: int
    space: int
    we: int
    wdata: int


def reset_state() -> dict:
    return {name: 0 for name, _ in REGISTERS}


def step(s: dict, rd: int) -> tuple[dict, Bus]:
    """One clock cycle at register level: (S(t), RDATA(t)) -> (S(t+1), bus(t))."""
    c = CONTROL[s["IR"] >> 8, s["MU"]]
    carry = (s["F"] >> 2) & 1
    lv = {"0": 0, "A": s["A"], "X": s["X"], "Y": s["Y"], "PC": s["PC"], "SP": s["SP"]}[c["l"]]
    rv = {"RD": rd, "ONE": 1, "OFF": sext10(s["IR"])}[c["r"]]
    res, cout = alu(c["alu"], lv, rv, carry)
    cond = condition(s)
    addr = {
        "PC": s["PC"],
        "ALU": res,
        "STK": STACK_BASE | s["SP"],
        "STK1": STACK_BASE | ((s["SP"] + 1) & 0xFF),
        "BR": res if cond else s["PC"],
    }[c["adr"]]
    bus = Bus(
        addr,
        FIELDS["space"].index(c["space"]),
        c["we"],
        {"A": s["A"], "X": s["X"], "Y": s["Y"], "PC": s["PC"]}[c["wd"]],
    )
    n = dict(s)
    if c["dst"] == "SP":
        n["SP"] = res & 0xFF
    elif c["dst"]:
        n[c["dst"]] = res
    f = s["F"]
    if c["zn"]:
        f = (f & FLAG_C) | (FLAG_Z if res == 0 else 0) | (FLAG_N if res & 0x8000 else 0)
    if c["cf"] == "ALU":
        f = (f & 3) | (cout << 2)
    elif c["cf"] == "CLR":
        f &= 3
    elif c["cf"] == "SET":
        f |= FLAG_C
    n["F"] = f
    if c["sp"] == "DEC":
        n["SP"] = (s["SP"] - 1) & 0xFF
    elif c["sp"] == "INC":
        n["SP"] = (s["SP"] + 1) & 0xFF
    pc = c["pc"]
    if pc == "INC" or (pc == "INC_OP" and has_operand(rd)):
        n["PC"] = (s["PC"] + 1) & 0x7FFF
    elif pc == "ALU" or (pc == "BR" and cond):
        n["PC"] = res & 0x7FFF
    if c["ir"]:
        n["IR"] = rd
    n["MU"] = c["mu"]
    return n, bus


@dataclass
class Machine:
    """The modelled system: core + ROM + RAM + I/O, cycle by cycle."""

    rom: list[int]
    ram: list[int] = field(default_factory=lambda: [0] * RAM_WORDS)
    state: dict = field(default_factory=reset_state)
    rd: int = 0
    cycle: int = 0
    debug: list[tuple[int, int]] = field(default_factory=list)
    reset_requested: bool = False

    def __post_init__(self):
        self.rom = list(self.rom) + [0] * (ROM_WORDS - len(self.rom))
        self.rd = self.rom[0]  # the address bus shows PC=0 in ROM during reset

    def read(self, addr: int, space: int) -> int:
        if space == SPACE_ROM:
            return self.rom[addr & 0x7FFF]
        return self.ram[addr] if addr < RAM_WORDS else 0

    def tick(self) -> Bus:
        self.state, bus = step(self.state, self.rd)
        nrd = self.read(bus.addr, bus.space)  # read-first
        if bus.we and bus.space == SPACE_RAM:
            if bus.addr < RAM_WORDS:
                self.ram[bus.addr] = bus.wdata
            elif bus.addr == IO_DEBUG:
                self.debug.append((self.cycle, bus.wdata))
            elif bus.addr == IO_RESET:
                self.reset_requested = True
        self.rd = nrd
        self.cycle += 1
        return bus
