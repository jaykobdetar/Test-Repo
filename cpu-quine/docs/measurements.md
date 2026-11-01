# CPU-Quine measurements

[README](../README.md) · [Status](../STATUS.md) · [As built](as-built.md) · [Design](design.md)

Every number here was measured on the host with the system emulator, which is
cycle-exact for the machine (checks 4-6 tie it to the RTL), unless marked
*extrapolated*. Times assume the design's 50 MHz clock. The timeline comes from
one run of 1.2×10¹² cycles (6.7 simulated hours, `make measure-long`); the cost
figures agree with the shorter `make measure` run to three digits.

## Measured against the design's estimates

| Quantity | Design estimate | Measured | Source |
| --- | --- | --- | --- |
| Flip-flops | about 116 | 92 | `build/netlist_summary.json` |
| Combinational cells | about 1,400 | 1,421 | same |
| Transistors | 9,000-12,000 | 9,378 | same |
| ROM used | 20,452 words (62%) | 27,745 words (85%) | `build/rom_summary.json` |
| Own RAM pages per level | 22 | 21 | check 8 |
| Cycles per instruction | 3.75 | 3.56 | `emu profile` |
| Instructions per replayed instruction | 50-100 | 25.9 | `emu profile` |
| Replay cost, cycles per replayed guest cycle of real work | 50-100 | 24.5 | `quine/budget.py` |
| Drawn frame | 2.98×10⁵ cycles, 6.0 ms | 2.69×10⁵ cycles, 5.4 ms | same |
| Drawing share of replay | 6% at 10 frames/s | 5.3% at 9.8 frames/s | same |
| Show loop | 16 s + 44 s | 16.1 s + 44.0 s = 60.1 s | same |
| Long-run R_avg, own cycles per guest cycle | 72-145 (109) | 26.2 | same |
| Emulator speed | — | 0.7-1.0×10⁹ cycles/s on one core | `emu run` |

## Nesting timeline

Level *k* is visible once the physical screen shows level *k*'s die map,
inside *k* − 1 nested insets.

| Level | Design | Measured |
| --- | --- | --- |
| 1 | — | 4.5 ms (first frame) |
| 2 | about 16 s | 16.3 s |
| 3 | about 30 min | 28.3 s |
| 4 | about 2 days | 7.5 min |
| 5 | — | 3.2 h |
| 6 | — | 3.5 days *(extrapolated)* |
| Epoch end (self-reset) | about 8 months | about 72 days *(extrapolated)* |
| 7 | — | never: it would need about 3 months |

Five levels fit in RAM at once (5 × 21 = 105 of 106 guest pages). Level 6
becomes visible after 3.5 days while it is still in its first watch phase and
holds one page. When level 5 finishes its own first watch phase, level 6 begins
real work, writes its second page, and the allocator runs out: the machine
resets after about 72 days and starts again. The deepest level ever shown is
level 6.

**Why level 3 comes so early.** The design's model has each level appear about
R_avg times later than the one before. But a replayed machine spends most of its
watch phase in delay loops, and the idle skip replays a whole delay loop in one
step. So the physical machine replays level 1's 16-second watch phase in about
12 seconds and level 3 appears 12 seconds after level 2. From level 4 on, levels
must be replayed through real work and the ratio approaches R_avg = 26.2: the
replay cost of 24.5, divided by the share of time spent replaying.

## How the timeline is computed

Every level runs the same program, so the function f(T), the guest cycle shown
by the last frame completed by a machine's own cycle T, is the same at every
level. Level 1 is visible from the first frame, A₁; level k+1 from the first
own cycle T with f(T) ≥ A_k. `quine/budget.py` reads f from a frame log of the
physical machine (`emu ROM log`), so A₂ to A₅ come straight from the measured
run. Beyond the log it extends f with its long-run slope 1/R_avg; those rows are
marked extrapolated. With only 13 simulated minutes of log the slope was not yet
settled (R_avg 35.4), which put the epoch end at about 5.7 months; the 6.7-hour
log is the better estimate.

The epoch end uses the measured own-page schedule (`emu ROM pages`): a level
writes its 2nd own page at its cycle 2,957 and all 21 by cycle 223,664, its first
frame. Level 0 holds the sum over levels of that schedule evaluated at each
level's cycle, and the allocator fails when the sum exceeds 106 pages.

## FPGA (design phase 3)

`make fpga` synthesizes `rtl/system.v` with Yosys `synth_ecp5` and places and
routes it with nextpnr-ecp5 0.6 for an LFE5U-45F (CABGA381), pins unconstrained:

| Quantity | Measured |
| --- | --- |
| Block RAMs (DP16KD) | 61 of 108 |
| Logic cells / flip-flops | 2,613 / 685 of 43,848 (6% / 2%) |
| Core clock, speed grade 8 | 52.4 MHz: meets 50 MHz |
| Core clock, speed grade 6 | 39.2 MHz: misses 50 MHz |
| Pixel clock (25 MHz needed) | 70 MHz or more |

The critical path is the core's: block-RAM output (5.6 ns) → operand → address
adder → address → block-RAM address, 25.2 ns at speed grade 6. It exists
because the as-built timing feeds the operand word straight into the address
adder (see [as-built.md](as-built.md#deviations-from-the-design)). On a speed
grade 6 part the machine can run at 37.5 MHz instead: every cycle count above
holds and the times stretch by 4/3.

## Reproducing

```sh
make measure          # profile, page schedule, 13 simulated minutes, timeline
make measure-long     # the 6.7-hour run, about 20 minutes on one core
make fpga             # ECP5 synthesis and place-and-route (needs nextpnr-ecp5)
build/emu build/rom.bin profile 1000000000
python3 -m quine.budget build/frames_long.log build/rom_summary.json build/page_schedule.json
```
