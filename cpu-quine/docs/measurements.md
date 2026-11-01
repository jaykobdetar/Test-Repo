# CPU-Quine measurements

[README](../README.md) · [Status](../STATUS.md) · [As built](as-built.md) · [Design](design.md)

Every number here was measured on the host with the system emulator, which is
cycle-exact for the 50 MHz machine (checks 4-6 tie it to the RTL), unless
marked *extrapolated*. Reproduce them with `make measure`.

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
| Drawn frame | 2.98×10⁵ cycles, 6.0 ms | 2.67×10⁵ cycles, 5.3 ms | same |
| Drawing share of replay | 6% at 10 frames/s | 5.3% at 9.8 frames/s | same |
| Show loop | 16 s + 44 s | 16.1 s + 43.9 s = 60.0 s | same |
| Long-run R_avg, own cycles per guest cycle | 72-145 (109) | 35.4 | same |
| Emulator speed | — | 0.7-1.0×10⁹ cycles/s on one core | `emu run` |

## Nesting timeline

Level *k* is "visible" once the physical screen shows level *k*'s die map,
inside *k* − 1 nested insets.

| Level | Design | Measured |
| --- | --- | --- |
| 1 | — | 4.5 ms (first frame) |
| 2 | about 16 s | 16.3 s |
| 3 | about 30 min | 28.3 s |
| 4 | about 2 days | 7.5 min |
| 5 | — | 4.3 h *(extrapolated)* |
| 6 | — | 6.3 days *(extrapolated)* |
| Epoch end (self-reset) | about 8 months | about 5.7 months *(extrapolated)* |

Five levels fit in RAM at once (5 × 21 = 105 of 106 guest pages); the epoch ends
when level 6 writes its second page, which happens after level 6 has appeared.
Level 7 would appear at about 7.3 months, after the epoch has ended, so the
deepest level ever shown is level 6.

**Why level 3 comes so early.** The design's model has each level appear about
R_avg times later than the one before. But a replayed machine spends most of its
watch phase in delay loops, and the idle skip replays a whole delay loop in one
step. So the physical machine replays level 1's 16-second watch phase in about
12 seconds and level 3 appears 12 seconds after level 2. From level 4 on, levels
must be replayed through real work, and the ratio approaches R_avg = 35.4.

## How the timeline is computed

Every level runs the same program, so the function f(T), the guest cycle shown
by the last frame completed by a machine's own cycle T, is the same at every
level. Level 1 is visible from the first frame, A₁; level k+1 from the first
own cycle T with f(T) ≥ A_k. `quine/budget.py` reads f from a frame log of the
physical machine (`emu ROM log`), so A₂, A₃ and A₄ come straight from the
measured run (40×10⁹ cycles, 13.3 simulated minutes). Beyond the log it extends
f with its long-run slope 1/R_avg; those rows are marked extrapolated.

The epoch end uses the measured own-page schedule (`emu ROM pages`): a level
writes its 2nd own page at its cycle 2,957 and all 21 by cycle 223,664, its first
frame. Level 0 holds the sum over levels of that schedule evaluated at each
level's cycle, and the allocator fails when the sum exceeds 106 pages.

## Reproducing

```sh
make measure          # profile, page schedule, 13 simulated minutes, timeline
build/emu build/rom.bin profile 1000000000
build/emu build/rom.bin log build/frames.log 40000000000
python3 -m quine.budget build/frames.log build/rom_summary.json build/page_schedule.json
```
