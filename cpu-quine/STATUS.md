# CPU-Quine status

[README](README.md) · [As built](docs/as-built.md) · [Measurements](docs/measurements.md) · [Design](docs/design.md)

This is the only place that records what the CPU-Quine can do today. Every claim
links to the check or measurement that supports it; `make check` reruns all
checks and CI runs it on every change under `cpu-quine/`.

Last updated: November 1, 2026.

**Summary.** Phases 1 and 2 of the [design](docs/design.md) are built and
verified on the host: the core, its transistor netlist, the replay engine in the
CPU's own assembly, and the quine property itself, checked three levels deep.
Nothing has run on an FPGA; phase 3 has not started.

## Works and verified

| Capability | Scope of the claim | Evidence |
| --- | --- | --- |
| Microarchitecture, one source of truth | RTL, C steppers, stepper handlers, ROM tables and the ISA reference are generated from `quine/arch.py` | `make`; CI regenerates `docs/isa.md` and fails on a difference |
| Cell library | each of the ten cells' transistor schematics equals its truth table under the switch-level solver, over every input transition; DFFR over every reachable state and both clock levels | check 1, `make check1` |
| Synthesis | 1,513 cells, 9,378 transistors, 92 flip-flops; only library cells, no loops, latches or tristates; formally equivalent to the RTL (145 points) | check 2, `make check2`; `build/netlist_summary.json` |
| Transistor-level exactness | all 9,378 transistors simulated at switch level, closed loop: every net and every transistor at the end of every half-cycle equals the gate-level expansion (3.3×10⁹ transistor states compared per run) | check 3, `make check3` |
| RTL vs C model | every flip-flop and bus signal every cycle, 2×10⁷ cycles of the ROM and 10 random ROMs including self-resets | check 4, `make check4` |
| Instruction vs microcycle stepper | full state, RDATA = ROM[PC] and all RAM at every boundary; the gate netlist every cycle; the ROM and 20 random ROMs | check 5, `make check5` |
| Replay engine | the ROM boots, alternates 64 watched cycles and about 44 s of replay, draws the die map, the guest's screen at half scale and the status panel | `make frames`; [screenshots](docs/images/) |
| ROM steppers vs C (check 6) | at every replayed instruction (7.6 million in 30 machine-seconds) and every watched cycle, level 1's state held by the ROM equals an independent C model | `make check6-8` |
| Quine property (check 7) | real show script, 30 machine-seconds: level 2 equals the model at 146,927 boundaries and 66 watched cycles, its whole RAM at 21 comparisons. Fast-paced script, 60 machine-seconds: level 3 at 13,460 boundaries and 6 watched cycles, its whole RAM at 18 comparisons. Holds across self-resets (468 epochs) | `make check6-8`, `check6-8-deep`, `check6-8-epochs` |
| Memory hygiene (check 8) | no read of an unwritten working-set word, no access to an unallocated page; 21 own pages per level | same |
| Drawn frames (check 10) | every drawn frame's net-state hash equals the C expansion of the model at that guest cycle | `make check10` |
| Measured costs | 25.9 instructions per replayed instruction; replay cost 24.5 cycles per guest cycle; drawn frame 5.3 ms; 60.0 s show loop | [measurements](docs/measurements.md), `make measure` |
| Nesting timeline | levels 2, 3 and 4 appear at 16.3 s, 28.3 s and 7.5 min (measured in the emulator); later levels and the epoch length are extrapolated | [measurements](docs/measurements.md) |

## Implemented but unverified

| Item | What is missing |
| --- | --- |
| Guest self-reset (`guest_reset` in `engine/main.s`) | no run reaches it: a guest reaches its out-of-memory point only after its host has reset |
| Level 4 and deeper replay correctness | check 7 reaches level 3; deeper levels run the same code but are not compared |
| Epoch length (about 5.7 months) | extrapolated from 13 simulated minutes with `quine/budget.py`; no run has reached an epoch end on the real show script |

## Not started

| Work | Notes |
| --- | --- |
| FPGA build (design phase 3) | system top level: RAM-clear reset controller, video scan-out, CRC observer, UART; board bring-up |
| Check 9 (hardware CRC stream) | needs the FPGA build |
| Silicon | PDK cells, real placement, memory interface across the pins |

## Deviations from the design

The build follows the design with 17 documented deviations, among them: no T
register (92 flip-flops), a six-NAND static flip-flop instead of a
transmission-gate one (check 1 found the latter fights itself during reset), the
transistor expansion doubling as the ROM's microcycle stepper, lazy flags in the
replay, and a 21-page working set. The full table with reasons is in
[as-built.md](docs/as-built.md#deviations-from-the-design).
