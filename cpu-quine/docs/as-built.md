# CPU-Quine as built

[README](../README.md) · [Status](../STATUS.md) · [Design](design.md) · [Measurements](measurements.md) · [ISA](isa.md)

This describes the machine that exists in this directory: a 16-bit CPU core, its
transistor netlist, and the ROM program that replays the machine's own execution
and draws every transistor. It follows the [design](design.md); where it differs,
the [deviations](#deviations-from-the-design) table says how and why. Current
standing and evidence are in [STATUS.md](../STATUS.md).

## One source of truth

Everything is generated from two Python descriptions and one assembly file.

| Source | Generates |
| --- | --- |
| `quine/arch.py`: registers, encoding, ALU, microcode table, memory semantics, reference model | `build/core.v` (RTL), `build/qmodel.h` (both C steppers), the stepper handlers and dispatch table in the ROM, `docs/isa.md` |
| `quine/cells.py`: the ten cells, their Boolean functions and transistor schematics | `build/cells.lib` (Liberty), `build/cells_sim.v`, `build/cell_patterns.json` (per-cell transistor states, via the switch-level solver) |
| `engine/main.s`: the hand-written part of the replay engine | with the generated `build/gen_*.s`, the ROM image `build/rom.bin` |

The pipeline: `rtlgen` writes `core.v`; Yosys maps it onto the ten-cell library
(`rtl/synth.ys`); `quine/netlist.py` loads the gate netlist and enforces the
circuit discipline; `quine/slots.py` assigns every net a bit of the net-state
bitset; `quine/cgen.py` and `quine/engine_gen.py` emit the C models and the
engine's tables and code; `quine/asm.py` assembles the ROM. `make` runs it all.

## The core

| Item | As built |
| --- | --- |
| Registers | A, X, Y (16), SP (8), PC (15), IR (16), MU (2), F = Z, N, C (3): **92 flip-flops**, all reset to 0 |
| Instructions | 47: 16 memory instructions in 4 modes and 2 spaces, 7 branches, DBNE, 17 implied, PHA/PLA/RTS, JMP/JSR/JMPX ([isa.md](isa.md)) |
| Timing | fetch (drive PC) and decode (IR ← RDATA) cycles, then 1 or 2 more: 3 cycles for implied, immediate and branches, 4 for absolute, stores, stack, JSR, JMPX and DBNE |
| Control | one control word per (MU, opcode high byte), emitted as a 512-entry case table |
| Memory bus | address, space (ROM/RAM), write enable, write data; RDATA registered by the memory |

**Memory semantics** (identical in every model): the address driven in cycle *t*
is read at the edge ending cycle *t* and appears on RDATA in cycle *t*+1. A read
happens every cycle; a write is read-first. ROM reads use address bits [14:0];
RAM reads above $7FFF return 0; writes to $FFFE go to the debug port, to $FFFF
request a self-reset. During reset the core drives PC = 0 in ROM space, so RDATA
is ROM[0] at cycle 0, the first rising edge after release.

**The boundary invariant.** Every instruction's last cycle drives the address of
the next instruction in ROM space (`arch._check_microcode` asserts it). So at
every instruction boundary MU = 0 and RDATA = ROM[PC], and an instruction-level
state is just the registers. Check 5 verifies this at every boundary.

## The circuit

The library has ten cells, all fully static complementary CMOS; none uses a
transmission gate. Transistor counts: INV 2, NAND2 4, NAND3 6, NOR2 4, NOR3 6,
AOI21 6, OAI21 6, XNOR2 12 and MUX2 12 (both built from an AOI22 with input
inverters), and DFFR 32.

**DFFR** is the six-NAND positive-edge flip-flop with asynchronous active-low
clear. Its internal latches lock two gate delays after the rising edge, before a
new Q can reach any D input, so edge triggering is exact under the reference
solver's wave ordering (below).

**Synthesis** (Yosys 0.33, ABC `amap`, area = transistor count): **1,421 gates +
92 flip-flops = 1,513 cells and 9,378 transistors**, logic depth 58. The mix is
INV 246, NAND2 167, NAND3 39, NOR2 387, NOR3 111, AOI21 156, OAI21 315; ABC used
no XNOR2 or MUX2. The build fails on any cell outside the library, a net with
two drivers, a combinational loop, a flip-flop off the single clock and reset,
or a flip-flop that is not a named register bit.

**Reference switch-level model** (`quine/switchsim.py`, and the same algorithm in
C in `csrc/check_switch.c`): ideal switches, no strengths. Nodes joined through
conducting transistors form a group; a group reaching GND is 0, VDD is 1, both
is a conflict (an error), neither floats and keeps its values. Settling is wave
ordered: every transistor gated by a node that changed in the previous wave
switches at once, then affected groups are recomputed. This unit-delay ordering
is what defines edge triggering. The memory changes RDATA after the clock edge
has settled.

## The replay engine

The ROM program replays the machine from reset (the *guest*), and draws the
guest's transistors. The guest runs the same ROM, so it does the same to its own
guest.

### RAM layout (every level)

| Pages | Contents |
| --- | --- |
| 0 | engine variables and the guest's state ($00-$31), the 101-word net-state bitset ($40-$A4), a 40-word copy buffer ($D8-$FF) |
| 1 | guest page table: 256 entries |
| 2 | stack |
| 3-21 | framebuffer, 320×240 at 1 bit per pixel, 20 words per row, bit 15 leftmost |
| 22-127 | guest pages, allocated upward |

Page 11 (framebuffer rows 103-115, between the die map and the inset) is never
written, so each level's working set is **21 pages**.

### Guest memory

A guest address *a* maps to *a* + PTAB[*a* >> 8]. A mapped entry holds the
delta (*p* − *g*) << 8 from guest page *g* to physical page *p*; an unmapped
entry holds $8000, which moves the read above $7FFF where reads return 0; entries
128-255 are 0, so guest I/O-space reads also return 0. A guest read is therefore
8 instructions with no branch (the `GREAD` macro). A guest write (`gwrite`)
drops zeros to unmapped pages, allocates the next free page on the first
non-zero write, ignores the guest's debug port, and flags a guest self-reset.
When the allocator reaches `PAGE_LIMIT` (128), the engine writes $FFFF: the
machine resets and a new epoch begins. Guest ROM reads go straight to the
physical ROM.

### Instruction stepper (replay)

One handler per opcode high byte (identical handlers shared), generated by
`quine/engine_gen.py` from `arch.py`: encodings, operand use and the cycle count
of each instruction come from the microcode table; the code templates per
instruction class are in `engine_gen.py`. Dispatch is threaded: each handler
ends with `NEXT n`, which adds the instruction's cycles to the 48-bit guest
cycle count and dispatches the next instruction through `JMPX` on the opcode's
high byte. The compact guest state keeps Z and N lazily (the last result, `gRES`)
and C as a word; `materialize` converts it to the flip-flop state for drawing.
Measured cost: 25.9 host instructions per replayed instruction.

**Idle skip.** A guest sitting on `DBNE` to itself runs all its iterations in one
step (X → 0, cycles += 4·X). Pacing loops are exactly this, so replaying a
guest's pauses costs almost nothing. Skipped cycles do not advance the frame
clock, which counts guest cycles of real work.

### Watch mode (clock-cycle stepping)

The ROM's microcycle stepper is the transistor expansion itself: the same pass
that evaluates every gate for drawing yields every flip-flop's D input and the
memory bus. `latch` performs the cycle's memory read (read-first) and write
through guest paging, then loads every register from its D word.

### Transistor expansion and the die map

`expand` clears the bitset, copies the guest registers and RDATA into their
word-aligned Q and RDATA words, and walks the 1,421 levelized cell records. Each
record names the cell's handler, the (word, mask) of each input and of its
output, and its die-map slot. A handler is a decision tree over its inputs; each
leaf knows the input combination *k*, sets the output bit if the cell's
function is 1 and ORs the cell's pixels into the current die-map word. For
every cell actually used, a transistor's state is set by one input: NMOS *i*
conducts when input *i* is 1, PMOS *i* when it is 0 (checked against the
switch-level patterns when the tables are built). A flush record after each
word writes the N row and derives the P row. Flip-flops are drawn from their D
and Q by `draw_dffs`.

Die map (top 120 rows): combinational cells in levelized order in 4-pixel slots,
80 per row, 18 rows of 4 pixel rows (PMOS, NMOS, two blank) from row 2; below,
one row per register (A, X, Y, PC, IR, then SP, MU, F) of 16-pixel flip-flops,
MSB on the left, rows 78-101. One pixel per transistor, lit when conducting,
drawn for the clock-low half of the cycle.

### Screen

| Region | Contents |
| --- | --- |
| Rows 0-119 | die map of the guest CPU |
| Rows 120-239, x 0-159 | the guest's own screen at half scale, read through guest paging, each 2×2 block OR-ed |
| Rows 120-239, x 160-319 | status panel: guest cycle (48-bit hex), PC, IR, A, X, Y, SP, Z N C, MU, the instruction in IR, guest pages in use, WATCH or REPLAY |

Each drawn frame also writes the guest cycle and a hash of the net-state bitset
to the debug port ($FFFE); check 10 compares them with the model.

### Show script and pacing

| Phase | As built |
| --- | --- |
| Watch | if the guest sits on `DBNE` to itself, skip it; then draw 64 consecutive guest cycles, each held 0.25 s by a counted delay (47 × 65,536 iterations of `DBNE`) |
| Replay | step instruction by instruction, drawing a frame every 3 × 65,536 guest cycles of real work (about 10 frames/s), for 433 frames |

The loop measures 16.1 s + 44.0 s = 60.1 s. Parameters are build-time defines
(`quine/build.py`, `ROM_PARAMS`).

### ROM use

27,745 of 32,768 words (85%): engine core 513, stepper handlers 6,169, dispatch
and branch-offset tables 1,280, expansion code 1,268, cell records 13,187,
flip-flop table and code 534, latch and hash 557, status panel 2,509, inset 1,728.

## FPGA system top level

`rtl/system.v` wraps the generated core for an FPGA (design phase 3). Nothing
outside the core feeds back into it except RDATA and reset, both modelled.

| Block | As built |
| --- | --- |
| Memory | 32K-word ROM loaded from `rom.hex`, 32K-word RAM; both synchronous and read-first; a registered space and I/O select picks RDATA |
| Reset controller | power-up and a write to $FFFF clear the RAM for 32,768 cycles with the core in reset, then release reset at a falling edge, so cycle 0 is the next rising edge |
| Observer | registers the core's 92 flip-flops (an observation-only `state` port on the core) every cycle the core advances and folds them into a CRC-32; every 2²⁰ cycles the CRC is queued for the UART. The CRC update is a flat GF(2) matrix generated by `quine/sysgen.py` |
| Debug port | writes to $FFFE go into a 16-word FIFO for the UART |
| UART | 8N1 at 115,200 baud: `A5` + CRC (4 bytes), `5A` + debug word (2 bytes), least significant byte first |
| Video | 640×480 at 60 Hz from a 25 MHz pixel clock on the RAM's second port, each framebuffer pixel shown as 2×2 |

The core's `state` port is wiring only: the synthesized cell netlist is the same
with or without it. On an ECP5 LFE5U-45 the system uses 61 block RAMs and meets
50 MHz at speed grade 8, not at grade 6 ([measurements](measurements.md#fpga-design-phase-3)).
It has not been run on a board.

## Verification ladder

| # | Check | How | `make` target |
| --- | --- | --- | --- |
| 1 | Cell equivalence | Python switch-level solver, every input transition of each combinational cell; breadth-first over every reachable state of DFFR with one input change at a time; output = truth table, no conflict, no floating gate node, transistor states a function of the inputs (and Q) | `check1` |
| 2 | Synthesis | Yosys `equiv_make` / `equiv_simple` / `equiv_induct` on RTL vs gate netlist, registers matched by name (145 points proven) | `check2` |
| 3 | Whole chip at switch level | C switch-level solver on all 9,378 transistors, closed loop with its own memory; at the end of every half-cycle every net and every transistor equals the gate-level expansion. The ROM plus 4 random ROMs | `check3` |
| 4 | Microcycle stepper vs RTL | Verilator, every flip-flop and bus signal every cycle: the ROM for 2×10⁷ cycles plus 10 random ROMs, one of them self-resetting every 6 cycles | `check4` |
| 5 | Instruction stepper vs microcycle stepper | full state, RDATA invariant and all RAM at every boundary, and the gate netlist (levelized C) every cycle; the ROM plus 20 random ROMs | `check5` |
| 6 | ROM steppers vs C | in the system emulator, at every replayed instruction the guest state held in RAM equals an independent C model at the same cycle; in watch mode the full flip-flop state after every replayed cycle | `check6-8` |
| 7 | Quine property | the same for levels 2 and 3, read through nested page tables, plus whole-RAM comparisons per level; on the real show script (levels 1-2), a fast-paced script (levels 1-3) and across self-resets | `check6-8`, `check6-8-deep`, `check6-8-epochs` |
| 8 | Memory hygiene | level 0 never reads a working-set word it has not written, never touches an unallocated page; each level's own pages ≤ 22 | same |
| 9 | Hardware CRC (in simulation) | the whole system RTL in Verilator with the C model in lockstep: every flip-flop whenever the core advances, the CRC stream and debug words decoded from the UART line, and every VGA frame captured while the framebuffer was unchanged; the ROM and the self-resetting ROM, 3×10⁷ cycles each. On hardware the host would run the same comparison on the real UART stream | `check9-sim` |
| 10 | Drawn frames | every frame's net-state hash from the debug port equals the hash of the C gate-level expansion of the model state at that guest cycle | `check10` |

The checks that compare against "the model" use the generated C steppers, which
checks 4 and 5 tie to the RTL, which check 2 ties to the gate netlist, which
check 3 ties to the transistors.

## Deviations from the design

| Topic | Design | As built | Why |
| --- | --- | --- | --- |
| T register | operand and memory-data register | removed | operands flow from RDATA straight into the ALU and address path; 92 flip-flops instead of about 116 |
| Microstate | 3 bits | 2 bits | no instruction needs more than 4 cycles |
| Timing | operand +1, data access +2 cycles | absolute access 4 cycles in all | the operand word feeds the address adder in the cycle it arrives |
| Boundary state | not specified | RDATA = ROM[PC] at every boundary | the instruction stepper then needs no memory-data register |
| Reset values | SP = $FF implied | all flip-flops reset to 0; stack grows down from $0200 within page 2 | DFFR has only a clear input |
| Instruction set | about 40 | 47, adding JMPX (jump through a ROM table), CPX and CPY | JMPX makes the stepper's dispatch one instruction |
| DFFR | transmission-gate master-slave | six static NANDs, 32 transistors | check 1 found the transmission-gate flip-flop drives a latch node both ways during reset under the ideal switch model |
| XNOR2, MUX2 | transmission gates | static AOI22 with inverters | no node is ever driven both ways; ABC used neither cell anyway |
| Switch-level model | settled levels | settled levels with unit-delay wave ordering | edge triggering needs an ordering; the flip-flop locks before any new Q arrives |
| Microcycle stepper in ROM | separate generated stepper, 40-80 instructions per cycle | the transistor expansion plus latch, about 31,000 instructions per cycle | exact by construction and shared with drawing; watch mode is bound by its 0.25 s hold, not by stepping |
| Core clock on an ECP5 | 50 MHz | 52.4 MHz at speed grade 8; 39.2 MHz at grade 6 | the operand-to-address path through the block RAMs is long; grade 6 parts can run at 37.5 MHz with every cycle count unchanged |
| Stepper generation | both steppers generated from the microcode table, in C and assembly | C: both generated from the table. Assembly: per-class templates in `engine_gen.py`, with encodings and cycle counts from the table | symbolic translation to assembly was not needed; check 6 compares the result with C at every instruction |
| Flags in replay | flip-flop vector | lazy Z/N (last result) and C; materialized for drawing | fewer instructions per replayed instruction |
| Page table | page numbers, 0 = unmapped | deltas, $8000 = unmapped; 256 entries | an 8-instruction branch-free guest read |
| Working set | 22 pages | 21 pages | page 11 lies in the unused band between die map and inset |
| Frame clock in replay | every 0.1 s | every 3 × 65,536 guest cycles of real work (about 0.1 s) | there is no timer; idle-skipped cycles would otherwise trigger needless frames |
| Delay-loop skip in watch mode | skip before the watched cycles | the same, once at the start of the watch phase | the 64 watched cycles stay consecutive |
| Status text | cycle, instruction, pages | also PC, A, X, Y, SP, flags, MU and the mode; hexadecimal | cheap and informative |

## Not built yet

- **Board bring-up (phase 3 onward):** pin constraints for a specific board,
  a bitstream, and check 9 against a real UART stream. The system RTL exists and
  is checked in simulation.
- **Silicon:** no PDK cells or real placement.
- **Guest self-reset** (`guest_reset`) exists but no check reaches it: a guest
  could only reach its out-of-memory point after its host has already reset.

Open questions from the design, as they stand: Y is kept and no indirect mode
was added (25.9 instructions per replayed instruction met the budget); the stack
uses an 8-bit SP; pages stay 256 words; watch mode shows the clock-low half
only; 1 bit per pixel; epochs run until the allocator fails.
