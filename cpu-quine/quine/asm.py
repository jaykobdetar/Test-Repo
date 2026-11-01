"""Two-pass assembler for the CPU-Quine instruction set.

Syntax summary (one statement per line, ';' starts a comment):

    label:  LDA #expr            immediate (operand word read from ROM)
            LDA expr             absolute, RAM space
            LDA expr,X           absolute + X (or ,Y), RAM space
            LDA rom expr,X       the same in ROM space
            STA expr,Y           stores always write RAM
            BEQ target           10-bit relative branches (also BRA, DBNE, ...)
            JMP target / JSR target / JMPX table   (JMPX: PC <= ROM[table+X])
            TAX                  implied instructions
    NAME = expr                  constant (also .equ NAME, expr)
    .org expr / .align n / .word e, ... / .fill n[, v] / .ascii "txt"
    .section rom|ram / .res n    RAM variables (no ROM output)
    .include "file" / .assert expr[, "msg"]
    .mark NAME                   global symbol here, without starting a new local scope
    .macro NAME a, b ... .endm   textual macros; \\a substitutes, \\@ is unique

Labels that start with '.' are local to the preceding global label.
Numbers: 123, 0x7B, $7B, 0b1111011, 'c'.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import arch

IDENT = re.compile(r"[A-Za-z_.][A-Za-z0-9_.]*")


class AsmError(Exception):
    pass


@dataclass
class Line:
    file: str
    num: int
    text: str
    label: str | None = None
    op: str | None = None
    args: str = ""
    scope: str = ""
    addr: int = 0
    section: str = "rom"


@dataclass
class Program:
    rom: list[int]
    symbols: dict[str, int]
    listing: list[tuple[int, list[int], str]]
    used: int  # highest ROM address used + 1
    lines: dict[int, str] = field(default_factory=dict)  # address -> source location


def _mangle(name: str) -> str:
    return name.replace(".", "__dot__")


class Assembler:
    def __init__(self, defines: dict[str, int] | None = None, include_dirs: list[Path] | None = None):
        self.equs: dict[str, tuple[str, Line]] = {}
        self.labels: dict[str, int] = {}
        self.values: dict[str, int] = dict(defines or {})
        self.macros: dict[str, tuple[list[str], list[str]]] = {}
        self.include_dirs = include_dirs or []
        self.lines: list[Line] = []
        self._uniq = 0

    # ---------------- source reading ----------------
    def _read(self, path: Path, depth: int = 0) -> list[tuple[str, int, str]]:
        if depth > 16:
            raise AsmError("include nesting too deep")
        out = []
        for i, raw in enumerate(path.read_text().splitlines(), 1):
            s = raw.split(";", 1)[0].strip() if '"' not in raw else _strip_comment(raw)
            m = re.match(r'\.include\s+"([^"]+)"', s)
            if m:
                inc = self._find(m.group(1), path.parent)
                out.extend(self._read(inc, depth + 1))
            else:
                out.append((str(path), i, s))
        return out

    def _find(self, name: str, base: Path) -> Path:
        for d in [base, *self.include_dirs]:
            p = Path(d) / name
            if p.exists():
                return p
        raise AsmError(f"include not found: {name}")

    def _expand_macros(self, src: list[tuple[str, int, str]]) -> list[tuple[str, int, str]]:
        out = []
        i = 0
        while i < len(src):
            f, n, s = src[i]
            m = re.match(r"\.macro\s+(\w+)\s*(.*)$", s)
            if m:
                params = [p.strip() for p in m.group(2).split(",") if p.strip()]
                body = []
                i += 1
                while i < len(src) and not src[i][2].startswith(".endm"):
                    body.append(src[i][2])
                    i += 1
                self.macros[m.group(1).upper()] = (params, body)
                i += 1
                continue
            out.extend(self._expand_line(f, n, s, 0))
            i += 1
        return out

    def _expand_line(self, f, n, s, depth):
        label, op, args = _split(s)
        if op and op.upper() in self.macros:
            if depth > 16:
                raise AsmError(f"{f}:{n}: macro recursion")
            params, body = self.macros[op.upper()]
            vals = _split_args(args)
            if len(vals) != len(params):
                raise AsmError(f"{f}:{n}: macro {op} expects {len(params)} args")
            self._uniq += 1
            res = [(f, n, f"{label}:")] if label else []
            for b in body:
                t = b.replace("\\@", f"M{self._uniq}")
                for p, v in sorted(zip(params, vals), key=lambda pv: -len(pv[0])):
                    t = t.replace("\\" + p, v)
                res.extend(self._expand_line(f, n, t, depth + 1))
            return res
        return [(f, n, s)]

    # ---------------- expressions ----------------
    def qualify(self, name: str, scope: str) -> str:
        return scope + name if name.startswith(".") else name

    def value(self, name: str, stack=()) -> int:
        if name in self.values:
            return self.values[name]
        if name in self.labels:
            return self.labels[name]
        if name in self.equs:
            if name in stack:
                raise AsmError(f"circular definition of {name}")
            expr, line = self.equs[name]
            v = self.eval(expr, line, stack + (name,))
            self.values[name] = v
            return v
        raise KeyError(name)

    def eval(self, expr: str, line: Line, stack=()) -> int:
        e = expr.strip()
        e = re.sub(r"\$([0-9A-Fa-f]+)", r"0x\1", e)
        e = re.sub(r"'(.)'", lambda m: str(ord(m.group(1))), e)
        names = {}

        def sub(m):
            q = self.qualify(m.group(0), line.scope)
            k = _mangle(q)
            names[k] = q
            return k

        e = IDENT.sub(sub, e)
        try:
            tree = ast.parse(e, mode="eval")
        except SyntaxError as ex:
            raise AsmError(f"{line.file}:{line.num}: bad expression {expr!r}") from ex

        def ev(node):
            if isinstance(node, ast.Expression):
                return ev(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, int):
                return node.value
            if isinstance(node, ast.Name):
                try:
                    return self.value(names[node.id], stack)
                except KeyError:
                    raise AsmError(f"{line.file}:{line.num}: undefined symbol {names[node.id]}") from None
            if isinstance(node, ast.UnaryOp):
                v = ev(node.operand)
                return {ast.USub: lambda: -v, ast.UAdd: lambda: v, ast.Invert: lambda: ~v}[type(node.op)]()
            if isinstance(node, ast.BinOp):
                a, b = ev(node.left), ev(node.right)
                ops = {
                    ast.Add: lambda: a + b,
                    ast.Sub: lambda: a - b,
                    ast.Mult: lambda: a * b,
                    ast.FloorDiv: lambda: a // b,
                    ast.Div: lambda: a // b,
                    ast.Mod: lambda: a % b,
                    ast.BitAnd: lambda: a & b,
                    ast.BitOr: lambda: a | b,
                    ast.BitXor: lambda: a ^ b,
                    ast.LShift: lambda: a << b,
                    ast.RShift: lambda: a >> b,
                }
                return ops[type(node.op)]()
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                a, b = ev(node.left), ev(node.comparators[0])
                cmp = {
                    ast.Eq: a == b,
                    ast.NotEq: a != b,
                    ast.Lt: a < b,
                    ast.LtE: a <= b,
                    ast.Gt: a > b,
                    ast.GtE: a >= b,
                }
                return int(cmp[type(node.ops[0])])
            raise AsmError(f"{line.file}:{line.num}: unsupported expression {expr!r}")

        return ev(tree)

    # ---------------- instruction parsing ----------------
    def size(self, line: Line) -> int:
        op = line.op.upper()
        if op in arch.MEM_OPS or op in arch.OPERAND_OP5:
            return 2
        if op in arch.OP5:
            return 1
        raise AsmError(f"{line.file}:{line.num}: unknown mnemonic {line.op}")

    def encode(self, line: Line) -> list[int]:
        op = line.op.upper()
        a = line.args.strip()
        loc = f"{line.file}:{line.num}"
        if op in arch.MEM_OPS:
            if a.startswith("#"):
                if op in arch.STORES:
                    raise AsmError(f"{loc}: store cannot be immediate")
                return [arch.encode_mem(op, "imm", 0), self.eval(a[1:], line) & 0xFFFF]
            space = arch.SPACE_RAM
            if re.match(r"rom\s", a, re.I):
                space = arch.SPACE_ROM
                a = a[3:].strip()
            mode = "abs"
            m = re.match(r"(.*),\s*([XYxy])$", a)
            if m:
                a, mode = m.group(1), "absx" if m.group(2).upper() == "X" else "absy"
            if op in arch.STORES:
                if space == arch.SPACE_ROM:
                    raise AsmError(f"{loc}: stores always write RAM")
                space = arch.SPACE_RAM
            return [arch.encode_mem(op, mode, space), self.eval(a, line) & 0xFFFF]
        if op in arch.BRANCHES:
            target = self.eval(a, line)
            off = target - (line.addr + 1)
            if not -512 <= off <= 511:
                raise AsmError(f"{loc}: branch out of range ({off})")
            return [arch.encode_op5(op, off)]
        if op in arch.OPERAND_OP5:
            if re.match(r"rom\s", a, re.I):
                a = a[3:].strip()
            return [arch.encode_op5(op), self.eval(a, line) & 0xFFFF]
        if op in arch.OP5:
            if a:
                raise AsmError(f"{loc}: {op} takes no operand")
            return [arch.encode_op5(op)]
        raise AsmError(f"{loc}: unknown mnemonic {line.op}")

    # ---------------- driver ----------------
    def assemble(self, path: Path) -> Program:
        src = self._expand_macros(self._read(Path(path)))
        scope = ""
        addr = {"rom": 0, "ram": 0}
        section = "rom"
        lines = []
        # pass 1: addresses and symbols
        for f, n, s in src:
            label, op, args = _split(s)
            ln = Line(f, n, s, label, op, args)
            m = re.match(r"([A-Za-z_.][\w.]*)\s*=\s*(.+)$", s)
            if m and not op:
                ln.scope = scope
                self.equs[self.qualify(m.group(1), scope)] = (m.group(2), ln)
                continue
            if label:
                if not label.startswith("."):
                    scope = label
                q = self.qualify(label, scope)
                if q in self.labels or q in self.equs:
                    raise AsmError(f"{f}:{n}: duplicate symbol {q}")
                self.labels[q] = addr[section]
            ln.scope = scope
            ln.section = section
            ln.addr = addr[section]
            if not op:
                continue
            o = op.lower()
            if o == ".equ":
                name, expr = [x.strip() for x in args.split(",", 1)]
                self.equs[self.qualify(name, scope)] = (expr, ln)
                continue
            if o == ".mark":  # a global symbol that does not open a new local scope
                q = args.strip()
                if q in self.labels or q in self.equs:
                    raise AsmError(f"{f}:{n}: duplicate symbol {q}")
                self.labels[q] = addr[section]
                continue
            if o == ".section":
                section = args.strip().lower()
                if section not in addr:
                    raise AsmError(f"{f}:{n}: unknown section {args}")
                continue
            if o == ".org":
                addr[section] = self.eval(args, ln)
                if label:
                    self.labels[self.qualify(label, scope)] = addr[section]
                continue
            if o == ".align":
                al = self.eval(args, ln)
                addr[section] = (addr[section] + al - 1) // al * al
                continue
            if o == ".res":
                if section != "ram":
                    raise AsmError(f"{f}:{n}: .res only in the ram section")
                addr[section] += self.eval(args, ln)
                continue
            if section == "ram":
                if o == ".assert":
                    lines.append(ln)
                    continue
                raise AsmError(f"{f}:{n}: only labels, .org and .res in the ram section")
            if o == ".word":
                addr["rom"] += len(_split_args(args))
            elif o == ".fill":
                addr["rom"] += self.eval(_split_args(args)[0], ln)
            elif o == ".ascii":
                addr["rom"] += len(_string(args, ln))
            elif o == ".assert":
                pass
            elif o.startswith("."):
                raise AsmError(f"{f}:{n}: unknown directive {op}")
            else:
                addr["rom"] += self.size(ln)
            lines.append(ln)
        # pass 2: emit
        rom = [None] * arch.ROM_WORDS
        listing = []
        locs = {}
        for ln in lines:
            o = ln.op.lower()
            if o == ".assert":
                parts = _split_args(ln.args)
                if not self.eval(parts[0], ln):
                    msg = parts[1] if len(parts) > 1 else parts[0]
                    raise AsmError(f"{ln.file}:{ln.num}: assertion failed: {msg}")
                continue
            if o == ".word":
                words = [self.eval(x, ln) & 0xFFFF for x in _split_args(ln.args)]
            elif o == ".fill":
                parts = _split_args(ln.args)
                v = self.eval(parts[1], ln) & 0xFFFF if len(parts) > 1 else 0
                words = [v] * self.eval(parts[0], ln)
            elif o == ".ascii":
                words = [ord(ch) for ch in _string(ln.args, ln)]
            else:
                words = self.encode(ln)
            for i, w in enumerate(words):
                a = ln.addr + i
                if not 0 <= a < arch.ROM_WORDS:
                    raise AsmError(f"{ln.file}:{ln.num}: address {a:#x} outside ROM")
                if rom[a] is not None:
                    raise AsmError(f"{ln.file}:{ln.num}: overlapping output at {a:#x}")
                rom[a] = w
                locs[a] = f"{Path(ln.file).name}:{ln.num}"
            listing.append((ln.addr, words, ln.text))
        used = max((i for i, w in enumerate(rom) if w is not None), default=-1) + 1
        rom = [w or 0 for w in rom]
        symbols = {}
        for k in list(self.labels) + list(self.equs):
            try:
                symbols[k] = self.value(k)
            except AsmError:
                pass
        return Program(rom, symbols, listing, used, locs)


def _strip_comment(raw: str) -> str:
    out, q = [], False
    for ch in raw:
        if ch == '"':
            q = not q
        if ch == ";" and not q:
            break
        out.append(ch)
    return "".join(out).strip()


def _split(s: str):
    label = None
    m = re.match(r"([A-Za-z_.][\w.]*):\s*(.*)$", s)
    if m:
        label, s = m.group(1), m.group(2)
    if not s:
        return label, None, ""
    if re.match(r"[A-Za-z_.][\w.]*\s*=", s):
        return label, None, s
    parts = s.split(None, 1)
    return label, parts[0], parts[1] if len(parts) > 1 else ""


def _split_args(args: str) -> list[str]:
    out, depth, cur, q = [], 0, [], False
    for ch in args:
        if ch == '"':
            q = not q
        if ch in "([" and not q:
            depth += 1
        if ch in ")]" and not q:
            depth -= 1
        if ch == "," and depth == 0 and not q:
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def _string(args: str, ln: Line) -> str:
    m = re.match(r'"(.*)"$', args.strip())
    if not m:
        raise AsmError(f"{ln.file}:{ln.num}: expected a string")
    return m.group(1)


def assemble(path, defines=None, include_dirs=None) -> Program:
    return Assembler(defines, include_dirs).assemble(Path(path))


def assemble_text(text: str, defines=None) -> Program:
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".s", delete=False) as f:
        f.write(text)
        name = f.name
    try:
        return assemble(name, defines)
    finally:
        Path(name).unlink()


def disassemble(word: int, operand: int | None = None) -> str:
    info = arch.decode(word >> 8)
    if info.kind == "mem":
        if info.mode == "imm":
            return f"{info.name} #${operand or 0:04X}"
        sp = "rom " if info.space == arch.SPACE_ROM and info.name not in arch.STORES else ""
        ix = {"abs": "", "absx": ",X", "absy": ",Y"}[info.mode]
        return f"{info.name} {sp}${operand or 0:04X}{ix}"
    if info.kind == "branch":
        off = word & 0x3FF
        off = off - 0x400 if off & 0x200 else off
        return f"{info.name} {off:+d}"
    if info.kind == "jump":
        return f"{info.name} ${operand or 0:04X}"
    return info.name
