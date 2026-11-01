# CPU-Quine build.  Everything under build/ is generated from quine/arch.py,
# quine/cells.py, the synthesized netlist and engine/main.s.
#
#   make            ROM image, C models and the system emulator
#   make check      the verification ladder: checks 1-8 and 10
#   make measure    timing measurements and the nesting timeline (13 simulated minutes)
#   make measure-long  the same over 6.7 simulated hours (about 20 minutes)
#   make frames     screenshots of the running machine (build/frames/*.png)
#   make fpga       synthesize and place-and-route the system for an ECP5 LFE5U-45
#                   (needs nextpnr-ecp5)
#
# Requires: python3 (standard library only), gcc, g++, yosys, verilator.

PY      ?= python3
CC      ?= gcc
CXX     ?= g++
CFLAGS  ?= -O3 -Wall -Wno-unused-function -Wno-unused-variable
B        = build
INC      = -I$(B) -Icsrc

QUINE    = $(wildcard quine/*.py)
HDRS     = csrc/qmachine.h csrc/qrun.inc $(B)/qmodel.h $(B)/qgates.h

# A fast-paced show script (4 watched cycles, tiny pauses, 2 frames per replay
# phase) reaches deep nesting levels quickly; the checks hold for any script.
FAST     = WATCH_CYCLES=4 HOLD_OUTER=1 HOLD_INNER=1 FF_FRAMES=2 FRAME_EVERY=1
# The same with only 14 guest pages: the allocator runs out within seconds, so
# the self-reset and the start of the next epoch are exercised.
EPOCH    = $(FAST) PAGE_LIMIT=36

all: $(B)/rom.bin $(B)/emu $(B)/check_models $(B)/check_switch

$(B):
	mkdir -p $(B)

# ---- RTL, cell library, synthesis --------------------------------------------
$(B)/core.v: quine/arch.py quine/rtlgen.py | $(B)
	$(PY) -m quine.rtlgen $@

$(B)/cells.lib $(B)/cells_sim.v &: quine/cells.py | $(B)
	$(PY) -m quine.cells $(B) > /dev/null

$(B)/core_gates.json $(B)/core_gates.v &: $(B)/core.v $(B)/cells.lib rtl/synth.ys
	yosys -q -l $(B)/synth.log rtl/synth.ys > /dev/null

# check 1 also produces the per-cell transistor-state tables used downstream
$(B)/cell_patterns.json: quine/cells.py quine/switchsim.py | $(B)
	$(PY) -m quine.switchsim $@ > /dev/null

$(B)/qmodel.h $(B)/qgates.h $(B)/qswitch.h $(B)/netlist_summary.json &: $(QUINE) $(B)/core_gates.json $(B)/cell_patterns.json
	$(PY) -m quine.build models > /dev/null

# ---- ROM images ---------------------------------------------------------------
$(B)/rom.bin $(B)/qengine.h $(B)/rom_summary.json &: $(QUINE) engine/main.s $(B)/core_gates.json $(B)/cell_patterns.json
	$(PY) -m quine.build rom $(FAST) > /dev/null
	cp $(B)/rom.bin $(B)/rom_fast.bin
	$(PY) -m quine.build rom $(EPOCH) > /dev/null
	cp $(B)/rom.bin $(B)/rom_epoch.bin
	cp $(B)/rom.hex $(B)/rom_epoch.hex
	$(PY) -m quine.build rom

$(B)/rom_fast.bin $(B)/rom_epoch.bin $(B)/rom.hex $(B)/rom_epoch.hex: $(B)/rom.bin

$(B)/crc96.v: quine/sysgen.py | $(B)
	$(PY) -m quine.sysgen $@

# ---- C tools ------------------------------------------------------------------
$(B)/check_models: csrc/check_models.c $(HDRS)
	$(CC) $(CFLAGS) $(INC) -o $@ $<

$(B)/check_switch: csrc/check_switch.c $(HDRS) $(B)/qswitch.h
	$(CC) $(CFLAGS) $(INC) -o $@ $<

$(B)/emu: csrc/emu.c csrc/guest.h $(HDRS) $(B)/qengine.h
	$(CC) $(CFLAGS) $(INC) -o $@ $<

$(B)/vcore/check_rtl: $(B)/core.v csrc/check_rtl.cpp $(HDRS)
	verilator --cc $(B)/core.v --exe $(CURDIR)/csrc/check_rtl.cpp --public-flat-rw -Wno-fatal \
	  -Wno-UNUSEDPARAM --Mdir $(B)/vcore -CFLAGS "-O2 -I$(CURDIR)/$(B) -I$(CURDIR)/csrc" \
	  --build -j 4 -o check_rtl > $(B)/verilator.log

$(B)/vsys/check_system: $(B)/core.v $(B)/crc96.v rtl/system.v csrc/check_system.cpp $(HDRS) $(B)/qengine.h
	verilator --cc $(B)/core.v $(B)/crc96.v rtl/system.v --top-module system --exe $(CURDIR)/csrc/check_system.cpp \
	  --public-flat-rw -Wno-fatal -Wno-lint -Wno-UNUSEDPARAM +define+SIMULATION --Mdir $(B)/vsys \
	  -CFLAGS "-O2 -I$(CURDIR)/$(B) -I$(CURDIR)/csrc" --build -j 4 -o check_system > $(B)/vsys.log

# ---- the verification ladder ---------------------------------------------------
.PHONY: check check1 check2 check3 check4 check5 check6-8 check6-8-deep check6-8-epochs check9-sim check10 \
	measure measure-long frames fpga clean

check: check1 check2 check3 check4 check5 check6-8 check6-8-deep check6-8-epochs check9-sim check10
	@echo "== all checks passed"

check1: $(B)/cell_patterns.json
	@echo "== check 1: each cell's transistors at switch level vs its truth table"
	$(PY) -m quine.switchsim

check2: $(B)/core_gates.v $(B)/cells_sim.v rtl/equiv.ys
	@echo "== check 2: gate netlist formally equivalent to the RTL"
	yosys -q -l $(B)/equiv.log rtl/equiv.ys > /dev/null
	@grep "Equivalence successfully proven" $(B)/equiv.log

check3: $(B)/check_switch $(B)/rom.bin
	@echo "== check 3: whole chip at switch level vs the gate-level expansion"
	$(B)/check_switch $(B)/rom.bin 100000 4 20000

check4: $(B)/vcore/check_rtl $(B)/rom.bin
	@echo "== check 4: microcycle stepper vs the Verilog RTL (Verilator), every flip-flop every cycle"
	$(B)/vcore/check_rtl $(B)/rom.bin 20000000 10 1000000

check5: $(B)/check_models $(B)/rom.bin
	@echo "== check 5: instruction stepper vs microcycle stepper, and the gate netlist every cycle"
	$(B)/check_models $(B)/rom.bin 2000000 20 200000

check6-8: $(B)/emu $(B)/rom.bin
	@echo "== checks 6-8: the ROM's replay vs the model (levels 1-2), memory hygiene; real show script"
	$(B)/emu $(B)/rom.bin check 1500000000

check6-8-deep: $(B)/emu $(B)/rom_fast.bin
	@echo "== checks 6-8: the same on the fast-paced script, reaching level 3"
	$(B)/emu $(B)/rom_fast.bin check 3000000000

check6-8-epochs: $(B)/emu $(B)/rom_epoch.bin
	@echo "== checks 6-8 across self-resets: a ROM whose allocator runs out after 14 pages"
	$(B)/emu $(B)/rom_epoch.bin check 1500000000

check9-sim: $(B)/vsys/check_system $(B)/rom.hex $(B)/rom_epoch.hex
	@echo "== check 9 in simulation: FPGA system RTL (UART CRC stream, debug port, VGA) vs the model"
	$(B)/vsys/check_system $(B)/rom.bin 30000000 +rom=$(B)/rom.hex
	$(B)/vsys/check_system $(B)/rom_epoch.bin 30000000 +rom=$(B)/rom_epoch.hex

check10: $(B)/emu $(B)/rom.bin
	@echo "== check 10: every drawn frame's net-state hash vs the model's expansion"
	$(B)/emu $(B)/rom.bin hashes 1500000000

# ---- measurements and pictures -------------------------------------------------
measure: $(B)/emu $(B)/rom.bin
	$(B)/emu $(B)/rom.bin profile 1000000000
	$(B)/emu $(B)/rom.bin pages 2000000 | $(PY) -c "import sys,json; print(json.dumps([int(x) for x in sys.stdin.read().split()[1:]]))" > $(B)/page_schedule.json
	$(B)/emu $(B)/rom.bin log $(B)/frames.log 40000000000
	$(PY) -m quine.budget $(B)/frames.log $(B)/rom_summary.json $(B)/page_schedule.json

# the 6.7-hour run that measures level 5 directly (about 20 minutes on one core)
measure-long: $(B)/emu $(B)/rom.bin
	$(B)/emu $(B)/rom.bin log $(B)/frames_long.log 1200000000000
	$(PY) -m quine.budget $(B)/frames_long.log $(B)/rom_summary.json $(B)/page_schedule.json

frames: $(B)/emu $(B)/rom.bin
	rm -rf $(B)/frames && mkdir -p $(B)/frames
	$(B)/emu $(B)/rom.bin frames $(B)/frames 1500000000 50
	$(PY) -m quine.png $(B)/frames

fpga: $(B)/core.v $(B)/crc96.v rtl/system.v $(B)/rom.hex
	cd $(B) && yosys -q -l ecp5.log -p "read_verilog core.v crc96.v ../rtl/system.v; synth_ecp5 -top system -json system_ecp5.json" > /dev/null
	cd $(B) && nextpnr-ecp5 --45k --speed 8 --package CABGA381 --json system_ecp5.json --freq 50 \
	  --lpf-allow-unconstrained --seed 1 > pnr.log 2>&1
	@grep -E "DP16KD:|TRELLIS_COMB:" $(B)/pnr.log; grep -E "Max frequency" $(B)/pnr.log | tail -2

clean:
	rm -rf $(B)
