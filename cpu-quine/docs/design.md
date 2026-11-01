> **Provenance.** This is the design as proposed (Nov 1, 2026), kept unchanged as the
> reference the build was made from. Its figures are pre-build estimates. What was
> actually built, where it differs and why, is in [as-built.md](as-built.md); measured
> numbers are in [measurements.md](measurements.md); current standing is in
> [STATUS.md](../STATUS.md).

# CPU-Quine: Revised Design

Nov 1, 2026 · @Jaykob

## Summary

The machine is a 16-bit CPU whose only program replays its own execution from reset and draws the on/off state of every transistor in it. The replay runs about 100 times slower than the machine, so what it shows sinks ever further into the past. Because the replayed machine runs the same program, the screen holds a picture of its own earlier screen, which holds an older one.

Three ideas carry the design:

- **The ROM is shared, the RAM is nested.** Every level reads the one physical ROM in place. Each level's RAM lives as sparse 256-word pages inside its parent's RAM, so memory grows by one working set (22 pages) per level and depth grows with the log of runtime.
- **Step registers, draw transistors.** The circuit is single-clock static CMOS, so every transistor's state is a pure function of the flip-flops, the clock level and the memory bus. The engine steps the machine at register level and expands to transistors only for the frames it draws.
- **One source of truth.** The RTL, both steppers and every drawing table are generated from one microarchitecture description, and each adjacent pair of models is checked for exact agreement.

At 50 MHz the picture-in-picture reaches level 2 in about 16 seconds, level 3 in half an hour and level 4 in about two days. Every performance figure here is an estimate until Phase 2 measures it.

## What changed from the research report

The report's core framing stands: a deterministic machine replays its own past from a known reset and accepts the delay. Eleven decisions change; the first two are what make the design work at all.

| Topic | Research report | This design | Why |
| --- | --- | --- | --- |
| Self-reference | "No infinite recursion"; only the CPU's transistor graph is modeled | ROM shared and read in place; each level's RAM nested as paged guest memory | The replayed CPU runs the replay, so its RAM holds a model of its own RAM |
| Replay method | Switch-level settling of every transistor, every cycle, on the device | Register-level stepping; transistor expansion only for drawn frames | Static CMOS makes the expansion exact (see Circuit discipline) |
| Replay cost | Illustrative R = 10⁴ cycles per replayed cycle | Fast-forward about 50–100 cycles per replayed cycle; about 3×10⁵ cycles per drawn frame | A switch-level solver ported to an 8-bit CPU would cost roughly 4×10⁶–1.3×10⁷, scaled from the [perfect6502 README](https://git.applefritter.com/6502/perfect6502/blame/branch/master/README.md) (1/30 of a 1 MHz 6502 on a 2025 desktop) |
| Word size | 8-bit | 16-bit words, word-addressed | Net references, pointers and 16-pixel screen words each fit one word |
| Address space | 12–16-bit bus carrying 64–128 KiB ROM plus 32–64 KiB RAM, which does not fit | Separate ROM and RAM spaces of 32K words each | Fits with headroom; guest ROM reads need no translation |
| Circuit style | Hand-built switch-level or unspecified standard cells | Single clock, static CMOS, edge-triggered flip-flops, ten-cell library | Makes every transistor a function of register state |
| Boot | Software RAM initialization | Hardware clears RAM during reset; the program touches only a 22-page working set | Every page the program touches is stored again at every nesting level |
| Checkpoints | 1 KiB snapshots, seek and rewind | None on the device; replay only moves forward | A resumable state includes the guest's RAM, 22 pages per level |
| Inputs | Logged buttons or serial | Reset only; pacing by counted delay loops | Nothing to log, store or replay |
| Display | Grid of transistor dots | Die map plus the guest's own screen at half scale | The inset makes the self-reference visible to a viewer |
| Verification | A list of suggested checks | A ladder of ten checks, from per-cell switch-level proofs to a per-cycle hardware hash | Every link from RTL to pixel is tested against its neighbour |

## Goals and the exact claim

The machine proves one claim: after a deterministic reset, it shows, for any cycle *t* of its own past that it chooses to draw, the exact on/off state of every transistor in its CPU at *t*, computed only from the CPU design, the ROM and the reset state.

- **Transistor state** means conducting or not, as set by the settled level of its gate net at the end of a half-cycle. This is the idealized switch model: no resistance, capacitance or delay.
- **Exact** means equal to a switch-level simulation of the CPU's transistor netlist for the same cycle. On the FPGA, "the CPU" is the transistor netlist that the FPGA logic is proven equivalent to; on silicon it is the fabricated netlist.
- **Past** means the drawn cycle trails the physical one, and the gap widens every second.

Goals:

1. Exact replay at that fidelity, verified end to end.
2. A self-reference a viewer can see without being told.
3. Unattended operation for months, with a deterministic self-restart.
4. Small enough to understand completely: about 10,000 transistors and 5,000 words of code.

Non-goals:

- Analog fidelity: voltages, currents, propagation delay, glitches.
- A zero-lag view of the present, which no self-hosted replay can give when R > 1.
- The FPGA's own transistors; the FPGA hosts the design, not its transistors.
- Interrupts, general I/O, or any input besides reset.
- Seek and rewind on the device.

## How self-replay works

The machine emulates itself, and the one hard part is RAM: every level can share the ROM, but each level's RAM must be stored inside its parent's. Below, L0 is the physical machine, L1 the machine it replays, L2 the machine L1 replays, and so on.

**State and delay.** The CPU's state S is its flip-flop vector, and it evolves as S(t+1) = F(S(t), D(t)), where D(t) is the memory data the CPU sees at cycle t. The program keeps a copy of S for its guest and applies F to it. Each guest cycle costs R physical cycles, so the drawn cycle falls further behind every second, and the design accepts that.

**Shared ROM is the quine trick.** Guest instruction fetches and table reads go to the guest's ROM, which is the physical ROM. The engine reads the real ROM at the same address, with no copy. One description serves as both the running program and the replayed program's data, which is how a software quine works.

**RAM nests.** The guest is running the replay engine, so its RAM holds its own model of its guest's RAM, and so on down. A full copy is impossible: no machine can fit its whole RAM plus anything else into that same RAM. Logging RAM reads as inputs does not escape this, because the log fills at bus speed and drains R times slower.

**Paging makes it finite.** A guest address splits into a page number and an offset, and a 128-entry page table maps each guest page to a 256-word physical page. Pages are allocated on the first non-zero write; unmapped pages read as zero, and zero-writes to them are dropped. Each level's page table and pages sit inside its parent's pages, so the structure nests by itself.

&#91;embedded content: RAM at the end of an epoch · five resident levels in 128 pages\]

L0 stores L1's 88 pages as ordinary guest pages, L1 does the same for L2 inside them, and so on down to L4. The ROM is never copied: every level's fetches and table reads are served from the one physical ROM.

**Depth grows with the log of runtime.** From level 3 on, each level appears about R\_avg ≈ 109 times later than the one before, where R\_avg is the average replay cost including drawing and pacing. Every running level costs one 22-page working set, so the 128-page RAM holds five levels, L0 to L4. When L5 starts and the allocator runs out, about 8 months in at 50 MHz, the machine resets itself and begins a new epoch.

**The replay never shows its own failure.** A guest would reach the same out-of-memory point only R\_avg times later than the physical machine, which never happens because the physical machine resets first. Every level runs the same deterministic program, so the self-reset needs no special case.

## Circuit discipline

The CPU is built so that every transistor's state is a pure function of the flip-flops, the clock level and the memory data bus. That is what lets the engine skip transistor simulation without approximating anything.

1. **One clock, one edge.** All state lives in rising-edge D flip-flops with asynchronous reset. No latches, no gated or derived clocks.
2. **Fully static complementary CMOS.** Every cell output is driven high or low for every input combination. No tristate buses, precharged or dynamic nodes, ratioed loads or wired logic.
3. **A ten-cell library:** INV, NAND2, NAND3, NOR2, NOR3, AOI21, OAI21, XNOR2, MUX2 and DFFR, each with one fixed transistor schematic. Transmission gates appear only inside XNOR2, MUX2 and DFFR, whose outputs are always driven.
4. **No undefined state.** Every flip-flop has a reset value.
5. **No combinational loops**, checked after synthesis.
6. **Known inputs only.** The CPU's inputs are clock, reset and memory read data, and the model knows all three.

**What the rules buy.** At a settled half-cycle, every combinational net is a Boolean function of the flip-flop outputs and the memory data. Flip-flop internal nodes are functions of Q, D and the clock level. A transistor conducts exactly when its gate net is high (NMOS) or low (PMOS), so gate-level evaluation gives the same transistor states as switch-level settling; the verification plan checks this exhaustively for each cell.

**The one exception is harmless.** A node between two series transistors floats when both are off. It drives no gate and no output, so it never changes any transistor's state, and the die map draws transistors, not nodes.

**Cell schematics.** The FPGA phases use textbook schematics for the ten cells, since the netlist is a design artifact there. A silicon phase swaps in a real PDK's cells for the same ten functions and regenerates every table.

## Machine architecture

The CPU is a 16-bit, word-addressed, multi-cycle machine with separate ROM and RAM spaces of 32K words each, zero-wait block RAM, and video scan-out outside the modeled core.

&#91;embedded content: model boundary · core replayed, memory known, outputs one-way\]

The replay needs only what crosses into the core: reset, ROM data and RAM data, all of which the model knows. The scan-out, CRC observer and debug port only read, so nothing they do can change what the CPU computes.

### Address spaces

- **ROM space**, 32K words (64 KiB): reset vector at address 0, code, model tables and font. Read by instruction fetch and by loads with the ROM bit set; never written.
- **RAM space**, 32K words (64 KiB) in 128 pages of 256 words, with the same layout at every level (table below).
- **I/O**: two write-only addresses above RAM, 0xFFFE for the debug hash and 0xFFFF for self-reset. Reads outside RAM return zero, in hardware and in the model.

| Pages | Contents | Words used |
| --- | --- | --- |
| 0 | Guest CPU state, show-script state, net-state bitset | 189 |
| 1 | Guest page table (128 entries), scratch | 256 |
| 2 | Stack | 256 |
| 3–21 | Framebuffer, 320×240 at 1 bit per pixel | 4,800 |
| 22–127 | Guest pages, allocated upward | as needed |

### Registers and instruction set (v0)

- **Visible:** A (accumulator), X and Y (index), SP (8-bit, stack in page 2), PC, flags Z, N and C.
- **Internal:** IR, T (operand and memory data), a 3-bit microstate and a few control bits; about 116 flip-flops in all.
- **Format:** a 16-bit opcode word, optionally followed by one 16-bit operand word. Modes are implied, immediate, absolute, absolute+X and absolute+Y; a space bit picks ROM or RAM for loads and ALU operands.
- **About 40 instructions:** loads and stores for A, X and Y; ADD, ADC, SUB, SBC, AND, OR, XOR, CMP; SHL, SHR, ROL, ROR and SWAB (byte swap); transfers, increments, PHA and PLA; branches on six conditions plus always, with 10-bit relative offsets in one word; JMP, JSR, RTS; and DBNE X (decrement X, branch if not zero).
- **Why these:** each serves the engine's inner loops. Indexed walks cover ROM tables, SWAB yields a page number in one step, AND and OR apply bit masks, and a DBNE delay loop can be skipped by the stepper in one step.

### Timing and memory

- Block RAM is synchronous: the address is registered at a rising edge and data is valid in the next cycle. That read-data register sits outside the CPU, but it is a known function of the previous cycle, so the model treats it as state.
- The CPU latches memory data only in read cycles, so the RAM's read-during-write mode never matters; the spec still fixes it.
- v0 timing: opcode fetch takes 2 cycles, each operand word 1 more and each data access 2 more, about 3.75 cycles per instruction on the expected mix (estimate).
- One 50 MHz clock drives the CPU. Video scan-out runs on its own pixel clock through the RAM's second port, and nothing flows back.

### Reset and epochs

Power-up and a write to the self-reset address start the same sequence. The reset controller holds the CPU in reset, clears all RAM (32,768 cycles), then releases reset at a defined edge; cycle 0 is the first rising edge after release. The program self-resets when the guest page allocator runs out, which ends the epoch.

### Size

About 116 flip-flops and 1,400 combinational cells, roughly 9,000–12,000 transistors. This is an estimate; Phase 1 measures it after synthesis.

## Replay engine

The engine steps the guest at register level and expands to transistors only for the frames it draws. A fast-forwarded guest cycle costs about 50–100 cycles; a drawn frame about 3.0×10⁵ cycles, or 6 ms at 50 MHz.

### Tier 1: two steppers from one microcode table

- **Microcycle stepper:** the exact flip-flop vector after every guest cycle. Used in watch mode and to land on a chosen cycle; about 40–80 instructions per guest cycle (estimate).
- **Instruction stepper:** the exact flip-flop vector at instruction boundaries, including IR, T, microstate and the cycle count. Used for fast-forward. Host and guest run the same instruction mix, so its cost per guest cycle is roughly its instruction count per emulated instruction: 50–100 (estimate; Phase 2 measures it).
- **Agreement:** both are generated from the microcode table and checked against each other at every instruction boundary.
- **Idle skip:** a delay loop is one DBNE instruction looping on itself, so the stepper can advance n iterations in a single step.

### Guest memory

- Guest ROM reads and fetches go straight to physical ROM.
- Guest RAM: the page number is the address's high byte (SWAB, then AND), the offset its low byte. Page-table entry 0 means unmapped, since physical page 0 is never a guest page.
- An unmapped read returns 0, a zero write to an unmapped page is dropped, and a non-zero write allocates the next free page. Pages are never freed and RAM is cleared at reset, so a fresh page is already zero.
- Allocation failure triggers the self-reset. Guest writes to the debug port are ignored; a guest self-reset would re-initialize the guest, but is never reached in practice.
- Cost: about 8 instructions per guest RAM access (estimate).

### Tier 2: transistor expansion

The input is the guest's flip-flop vector, its memory data register and the clock level.

1. Copy flip-flop outputs and memory data into the 2,000-bit net-state bitset.
2. Evaluate the 1,400 combinational cells once each, in levelized order from a ROM table, by truth-table lookup.
3. Read each transistor's state from its gate net; flip-flop internals follow from Q, D and the clock level.

### Drawing

- **Screen:** 320×240 at 1 bit per pixel, doubled to 640×480 by the scan-out.
- **Top half, 320×120: die map** of the guest CPU, in cell rows like a standard-cell die. One pixel per transistor, PMOS row above NMOS row, lit when conducting.
- **Bottom left, 160×120: the guest's own screen** at half scale, read through guest paging with each 2×2 block OR-ed into one pixel. This is the picture-in-picture, and it holds the next level's in turn.
- **Bottom right, 160×120: status text:** guest cycle number, the instruction in the guest's IR, guest pages in use.
- **Cost per frame:** expansion 28k, die map 18k, inset 30k, text and flip-flops 3k, about 79k instructions or 3.0×10⁵ cycles (estimate).

### Show script and pacing

A deterministic 60-second loop with no inputs:

1. **Watch, 16 s:** draw 64 consecutive guest cycles, each held for 0.25 s. If the guest is idling in a delay loop, skip to its end first, so the watched cycles show real work.
2. **Fast-forward, 44 s:** step the guest with the instruction stepper, drawing a frame every 0.1 s (6% of the time).

Pacing uses counted DBNE loops, so the timing is itself part of the replayed history. The loop averages R\_avg ≈ 109 at a fast-forward cost of 75.

## Budgets

Everything fits with headroom: ROM is 62% used, each nesting level takes 22 of 128 RAM pages, and level 4, three insets deep, appears after about two days. The figures come from budget.py, a small model shared alongside this doc; its inputs stay estimates until Phase 2 measures them.

### ROM

| ROM contents | Words |
| --- | --- |
| Code: steppers, paging, expansion, drawing, show script | 5,000 |
| Levelized cell table, 1,400 cells × 4.2 words | 5,880 |
| Flip-flop table, 116 × 4 words | 464 |
| Drawing tables: per-cell slots and pre-shifted pixel patterns | 8,152 |
| Font, disassembler and bit tables | 956 |
| **Total** | **20,452 of 32,768 (62%)** |

### RAM

Each level uses 22 pages (layout under Address spaces), so 128 pages hold five resident levels with 18 pages spare. A 64K-word RAM needs a second page-table page, 23 pages per level, and holds eleven.

### Cycles

| Operation | Cost | Basis |
| --- | --- | --- |
| Fast-forward, per guest cycle | 50–100 cycles | Instructions per emulated instruction (estimate) |
| Watch step, per guest cycle | 150–300 cycles | 40–80 instructions at 3.75 cycles each |
| Guest RAM access | About 8 extra instructions | Page-table lookup |
| One drawn frame | 2.98×10⁵ cycles, 6.0 ms at 50 MHz | 79,352 instructions at 3.75 cycles each |
| Drawing during fast-forward | 6.0% of cycles | 10 frames per second |
| Show-loop average, R\_avg | 72, 109 or 145 | Fast-forward cost of 50, 75 or 100 |

### Nesting timeline

&#91;embedded content: budget model · fast-forward cost 50–100 cycles per guest cycle, 60-second show loop\]

The spread comes from the fast-forward cost, which Phase 2 measures. With the 64K-word RAM option eleven levels fit, so in practice an epoch never runs out of memory.

## Toolchain

One Python description of the microarchitecture generates the RTL, both steppers and every ROM table, so the model and the machine cannot drift apart unnoticed.

1. **Microarchitecture description:** registers with widths and reset values, datapath operations, the microcode table (opcode × step → control signals) and memory timing. Plain Python or an HDL library such as Amaranth.
2. **RTL generator:** emits Verilog for the core.
3. **Synthesis:** Yosys maps the RTL onto the ten-cell Liberty library. The build fails on any latch, loop, tristate or cell outside the library.
4. **Cell expansion:** turns the gate netlist into a transistor netlist for the reference solver and the die map.
5. **Placement:** assigns die-map coordinates, synthetic cell rows in the FPGA phases and the real placement on silicon.
6. **Table generator:** emits the levelized cell table, flip-flop table, drawing tables and disassembler.
7. **Stepper generator:** emits both steppers twice, in C for the host and in the CPU's own assembly for the ROM.
8. **Assembler and linker:** builds the ROM image from code and tables and records its hash with the build.
9. **FPGA build:** the same Verilog and ROM image, plus the RAM-clear controller, scan-out, observer and UART.

The project writes two tools of its own in C: the switch-level reference solver and a fast whole-system emulator for long host runs. Everything else is standard: Yosys, Verilator, and nextpnr or Vivado for the board.

## Verification plan

Every link from RTL to pixel is checked against its neighbour, and the quine property itself is tested on the host before any hardware exists.

| # | Check | Compares | Method | Runs |
| --- | --- | --- | --- | --- |
| 1 | Cell equivalence | Each cell's transistor schematic vs its truth table | Switch-level solver over all inputs, stored states and both clock levels | Every library change |
| 2 | Synthesis | Gate netlist vs RTL | Yosys formal equivalence | Every build |
| 3 | Whole-chip switch level | Transistor netlist vs gate netlist | Co-simulation on random programs and the real ROM | Every build, sampled |
| 4 | Microcycle stepper | Stepper vs Verilator | Lockstep on every flip-flop, every cycle | Every build |
| 5 | Instruction stepper | Instruction stepper vs microcycle stepper | Full flip-flop vector at every instruction boundary | Every build |
| 6 | Target code | Steppers in the CPU's assembly vs their C twins | Same guest stepped both ways in the system emulator | Every build |
| 7 | Quine property | L0's replay of L1 at cycle t vs L0's own recorded state at t; likewise L1's replay of L2 | Long host runs, sampled t | Every release |
| 8 | Memory hygiene | No read of never-written RAM; 22 pages per level | Emulator instrumentation | Every release |
| 9 | Hardware vs model | Running CRC of all flip-flops, folded every cycle | Observer sends the CRC every 2²⁰ cycles over UART; the host checks it, live or offline | Phase 3 on, continuous |
| 10 | Live replay | Hash of each drawn frame's net states | Program writes it to the debug port; the host compares | Phase 3 on, continuous |

Check 7 matters most: it shows, before any hardware, that the machine's picture of its past equals its past. Checks 9 and 10 then tie the physical machine to that result.

## Hardware platform

Any FPGA with 64 free 18-Kbit block RAMs, a video output and a 50 MHz clock can host the machine; silicon is a later, optional phase.

- **Block RAM:** 32K words of ROM and 32K words of RAM at 16 bits fill 64 blocks of 1K×18, or 1,152 Kbit counting the unused parity bits. They must be initialized at configuration, with a second port for scan-out.
- **Logic:** the CPU, RAM-clear controller, scan-out, CRC observer and UART, a small fraction of any part below (estimate).
- **Avoid for v1:** parts whose large RAM cannot be preloaded or has a single port. They need a loader and arbitration, which add timing the model would have to include.

| Part | Block RAM | Fits the 32K-word RAM | Fits the 64K-word RAM option |
| --- | --- | --- | --- |
| [Lattice LFE5U-25](https://media.latticesemi.com/zh-CN/Products/FPGAandCPLD/ECP5) | 1,008 Kbit, 56 blocks | No, 8 blocks short | No |
| [Lattice LFE5U-45](https://media.latticesemi.com/zh-CN/Products/FPGAandCPLD/ECP5) | 1,944 Kbit, 108 blocks ([Arrow](https://www.arrow.com/en/products/lfe5um-45f-6bg554c/lattice-semiconductor.html)) | Yes, with the open Yosys and nextpnr flow | Yes, 96 of 108 blocks |
| [Lattice LFE5U-85](https://media.latticesemi.com/zh-CN/Products/FPGAandCPLD/ECP5) | 3,744 Kbit, 208 blocks | Yes | Yes |
| [AMD XC7A35T](https://eng.auburn.edu/~nelson/courses/elec4200/FPGA/ds180_7Series_Overview.pdf) | 1,800 Kb, 50 blocks of 36 Kb | Yes, with Vivado | Yes, 48 of 50 blocks |
| [AMD XC7A100T](https://eng.auburn.edu/~nelson/courses/elec4200/FPGA/ds180_7Series_Overview.pdf) | 4,860 Kb, 135 blocks of 36 Kb | Yes | Yes |

**Silicon, optional.** The CPU core alone can go on a multi-project wafer shuttle, with ROM, RAM and video staying on the FPGA. The tables are then regenerated from the PDK's cells and the real placement, and the die map shows physical transistors. The memory interface must stay fixed-latency across the chip pins; a narrow multiplexed bus on a small slot changes the timing model, so memory timing stays a parameter of the description. A discrete-transistor build of about 10,000 transistors is out of scope.

## Milestones

Each phase ends at a gate that demands exact agreement, and everything that can be proven on the host is proven there first.

&#91;embedded content: plan · six phases, five gates, effort estimates\]

G2 proves the idea before any hardware exists: the machine's replay of its own past matches its recorded past over 10¹¹ cycles. Everything after it ties the physical machine to that result.

## Risks and open questions

The largest risk is a fast-forward cost above 100, which delays every level's arrival; the largest open choice is how much of the instruction set to spend on the engine's inner loops.

| Risk | Effect | Mitigation |
| --- | --- | --- |
| Fast-forward cost above 100 | Every level arrives later (Budgets shows the spread) | Profile in Phase 2 and tune the ISA; idle skip; accept a slower descent |
| A generator bug makes a stepper disagree with the hardware | The replay is silently wrong | Checks 4–6 on every build; the hardware CRC stream (check 9) |
| Transistor count well above 12,000 | Drawing tables and die map overflow | 12,316 ROM words spare; drop Y; denser die map or a scrolling viewport |
| Working set above 22 pages | Fewer resident levels, shorter epochs | 64K-word RAM option; smaller framebuffer |
| Die map unreadable at one pixel per transistor | A weak exhibit | Zoomed viewport in watch mode; 2 bits per pixel |
| Synthesis emits a cell outside the discipline | Expansion no longer exact | Restricted Liberty file; the build fails on any such cell |

Open questions:

- [ ] Keep Y, or one index register plus an indirect mode? Decide from Phase 2 instruction counts.
- [ ] An 8-bit SP stack, or a link register for calls?
- [ ] Page size of 256 words, or 128 to cut fragmentation?
- [ ] Show both half-cycles in watch mode, at twice the frames per watched cycle?
- [ ] Use 2 bits per pixel to highlight what changed this cycle? It doubles the framebuffer to 38 pages at every level, leaving room for three levels.
- [ ] Run each epoch until the allocator fails, about 8 months, or reset earlier so visitors see the start more often?
- [ ] Which silicon path, and what memory interface across its pins?

## Sources

- [perfect6502 README](https://git.applefritter.com/6502/perfect6502/blame/branch/master/README.md): switch-level speed, 1/30 of a 1 MHz 6502 on a 2025 desktop.
- [Lattice ECP5 and ECP5-5G product page](https://media.latticesemi.com/zh-CN/Products/FPGAandCPLD/ECP5): block RAM per device.
- [Arrow, LFE5UM-45F-6BG554C](https://www.arrow.com/en/products/lfe5um-45f-6bg554c/lattice-semiconductor.html): 1,944 Kbit in 108 blocks.
- [AMD DS180, 7 Series FPGAs Overview](https://eng.auburn.edu/~nelson/courses/elec4200/FPGA/ds180_7Series_Overview.pdf): Artix-7 block RAM per device.
- The research report being revised, deep-research-report (2).md, supplied in chat.
