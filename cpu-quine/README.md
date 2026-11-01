# CPU-Quine

[Status](STATUS.md) · [As built](docs/as-built.md) · [Measurements](docs/measurements.md) · [ISA](docs/isa.md) · [Design](docs/design.md)

A 16-bit CPU whose only program replays its own execution from reset and draws
the on/off state of every transistor in it. The replayed machine runs the same
program, so the screen holds a picture of its own earlier screen, which holds an
older one.

![The machine replaying itself, 7.5 minutes after reset: the die map of the
replayed CPU on top, the replayed machine's own screen at half scale bottom
left (holding its own replay, and that one's), and the status panel.](docs/images/replay-7min.png)

This directory holds the whole machine as a host-side build: the microarchitecture
description, the generated RTL, the ten-cell transistor library, the synthesized
netlist, the replay engine in the CPU's own assembly, cycle-exact models, and a
verification ladder that ties every link from transistors to pixels together. It
corresponds to phases 1 and 2 of the [design](docs/design.md); nothing here has
run on an FPGA yet. [STATUS.md](STATUS.md) says exactly what works and how it was
checked.

## Quick start

Needs Linux with `python3` (standard library only), `gcc`, `g++`, `make`,
`yosys` and `verilator` (Ubuntu 24.04: `apt install yosys verilator`).

```sh
cd cpu-quine
make            # RTL, synthesis, netlist checks, ROM image, C models, emulator
make check      # the verification ladder, about a minute on 4 cores
make frames     # PNG screenshots of the running machine in build/frames/
make measure    # profile, page schedule, 13 simulated minutes, nesting timeline
```

The emulator runs the physical machine at roughly 0.7-1 billion cycles per
second on one core, about 15-20 times faster than the 50 MHz hardware would:

```sh
build/emu build/rom.bin run 3000000000                 # one minute of machine time
build/emu build/rom.bin frames build/frames 1500000000 20
build/emu build/rom.bin check 1500000000               # checks 6-8
```

## How it fits together

```text
quine/arch.py ──► rtlgen ──► build/core.v ──► Yosys (rtl/synth.ys) ──► build/core_gates.json
      │                                         ▲ cells.lib               │
      │           quine/cells.py ──► switchsim ─┴─► cell_patterns.json    │
      │                                                │                  ▼
      ├──► cgen ──► build/qmodel.h (C steppers)        └──► slots, engine_gen ──► build/gen_*.s
      │                                                                          │
      └──► engine_gen (stepper handlers) ──────────────────────────────────────► │
                                                    engine/main.s ──► asm ──► build/rom.bin
```

| Path | What it is |
| --- | --- |
| `quine/arch.py` | the microarchitecture: registers, encoding, ALU, microcode table, memory semantics, reference model |
| `quine/rtlgen.py` | Verilog generator for the core |
| `quine/cells.py`, `quine/switchsim.py` | the ten cells with transistor schematics; switch-level solver and check 1 |
| `quine/netlist.py`, `quine/slots.py` | gate-netlist loading, discipline checks, levelization; net-state bitset layout |
| `quine/cgen.py` | generated C: both steppers, levelized gate evaluation, transistor netlist |
| `quine/engine_gen.py`, `quine/font.py` | generated assembly: stepper handlers, cell tables, die map, status panel, inset |
| `quine/asm.py` | assembler for the CPU's instruction set |
| `quine/budget.py` | the nesting timeline from measured frame logs |
| `quine/isadoc.py`, `quine/png.py` | ISA reference generator; PBM-to-PNG converter |
| `engine/main.s` | the hand-written part of the replay engine |
| `rtl/synth.ys`, `rtl/equiv.ys` | synthesis onto the library; formal equivalence (check 2) |
| `csrc/` | the memory system (`qmachine.h`), the system emulator and checks (`emu.c`), switch-level chip check, model and RTL lockstep checks |
| `docs/` | as-built description, measurements, ISA reference, the original design |

## Documentation

- [STATUS.md](STATUS.md): what works today, with the evidence for each claim.
- [docs/as-built.md](docs/as-built.md): how the machine and its engine work, and
  every deviation from the design with its reason.
- [docs/measurements.md](docs/measurements.md): measured costs and the nesting
  timeline, and how to reproduce them.
- [docs/isa.md](docs/isa.md): instruction set, generated from `quine/arch.py`.
- [docs/design.md](docs/design.md): the design this was built from, unchanged.

Change `quine/arch.py` or `engine/main.s`, then run `make check`. When behaviour
or a measured number changes, update STATUS.md and the affected document in the
same commit; regenerate `docs/isa.md` with `python3 -m quine.isadoc > docs/isa.md`
(CI fails if it is stale).
