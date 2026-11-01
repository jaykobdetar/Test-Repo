"""Generate every derived artifact from arch.py and the synthesized netlist.

usage: python3 -m quine.build <step> [args]   (see the Makefile)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import cgen, netlist, slots
from .cells import CELLS

BUILD = Path("build")


def models():
    (BUILD / "qmodel.h").write_text(cgen.gen_model())
    nl = netlist.load(BUILD / "core_gates.json")
    sl = slots.assign(nl)
    (BUILD / "qgates.h").write_text(cgen.gen_gates(nl, sl))
    pats = json.loads((BUILD / "cell_patterns.json").read_text())
    (BUILD / "qswitch.h").write_text(cgen.gen_switch(nl, sl, pats))
    summary = {
        "gates": len(nl.gates),
        "flip_flops": len(nl.dffs),
        "cells": len(nl.cells),
        "transistors": nl.transistor_count(),
        "transistors_by_type": {t: sum(1 for c in nl.cells if c.type == t) * CELLS[t].size for t in CELLS},
        "logic_depth": max(g.level for g in nl.gates),
        "net_bits": sl["nbits"],
        "net_words": sl["nwords"],
    }
    (BUILD / "netlist_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


ROM_PARAMS = {
    # the show script: watch 64 cycles held 0.25 s each, then replay for ~44 s
    # (values tuned against the measured replay cost; see docs/measurements.md)
    "WATCH_CYCLES": 64,
    "HOLD_OUTER": 47,  # 47 x 65536 x 4 cycles = 0.246 s at 50 MHz
    "HOLD_INNER": 0,  # 0 = 65536 iterations
    "FF_FRAMES": 433,  # ~44 s of replay at ~10 frames per second
    "FRAME_EVERY": 3,  # a frame per 3 x 65536 guest cycles of real work
    "PAGE_LIMIT": 128,  # the allocator may use every RAM page
}


def rom(*overrides):
    from . import asm, engine_gen

    params = dict(ROM_PARAMS)
    for kv in overrides:
        k, v = kv.split("=")
        params[k] = int(v, 0)
    nl = netlist.load(BUILD / "core_gates.json")
    sl = slots.assign(nl)
    pats = json.loads((BUILD / "cell_patterns.json").read_text())
    (BUILD / "gen_layout.s").write_text(engine_gen.layout_asm(sl))
    (BUILD / "gen_stepper.s").write_text(engine_gen.stepper_asm())
    cells_s, info = engine_gen.cells_asm(nl, sl, pats)
    (BUILD / "gen_cells.s").write_text(cells_s)
    (BUILD / "gen_text.s").write_text(engine_gen.text_asm())
    (BUILD / "gen_inset.s").write_text(engine_gen.inset_asm())
    prog = asm.assemble(Path("engine/main.s"), params, [BUILD])
    import struct

    (BUILD / "rom.bin").write_bytes(struct.pack(f"<{len(prog.rom)}H", *prog.rom))
    (BUILD / "rom.hex").write_text("".join(f"{w:04x}\n" for w in prog.rom))
    (BUILD / "rom.sym").write_text(
        "".join(f"{v:04x} {k}\n" for k, v in sorted(prog.symbols.items(), key=lambda kv: kv[1]))
    )
    with open(BUILD / "rom.lst", "w") as f:
        for a, words, text in prog.listing:
            f.write(f"{a:04x}  {' '.join(f'{w:04x}' for w in words[:4]):20s} {text}\n")
    (BUILD / "qengine.h").write_text(engine_gen.c_header(sl, prog.symbols, info))
    sections = {}
    marks = [
        "reset",
        "h_ill",
        "DISPATCH_TABLE",
        "expand",
        "CELLS",
        "draw_dffs",
        "latch_regs",
        "text_static",
        "HEXG",
        "draw_inset",
        "CT12",
        "ROM_END",
    ]
    for a, b in zip(marks, marks[1:]):
        sections[f"{a}..{b}"] = prog.symbols[b] - prog.symbols[a]
    summary = {"rom_words_used": prog.used, "params": params, **info, "sections": sections}
    (BUILD / "rom_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: summary[k] for k in ("rom_words_used", "cell_records", "cell_table_words")}))


if __name__ == "__main__":
    {"models": models, "rom": rom}[sys.argv[1]](*sys.argv[2:])
