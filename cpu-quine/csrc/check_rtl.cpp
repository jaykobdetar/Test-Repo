// Check 4: the Verilog RTL (via Verilator) against the C microcycle stepper,
// lockstep on every flip-flop and every bus signal, every cycle.
//
// usage: check_rtl ROM|- [cycles] [random-roms] [cycles-each]
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include "Vcore.h"
#include "Vcore___024root.h"
#include "verilated.h"
extern "C" {
#include "qmachine.h"
}

static uint64_t rng = 0x9E3779B97F4A7C15ull;
static uint16_t rnd16() { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (uint16_t)rng; }

static qmachine m;

static int run(Vcore *v, uint64_t cycles, const char *name) {
    qm_power_on(&m);
    m.epoch = 0;
    // memory model for the RTL side: separate RAM copy, same ROM
    static uint16_t ram[QM_RAM_WORDS];
    memset(ram, 0, sizeof ram);
    v->clk = 0;
    v->rst_n = 0;
    v->rdata = m.rom[0];
    v->eval();
    v->clk = 1; v->eval(); v->clk = 0; v->eval();  // a clock edge in reset
    v->rst_n = 1;
    v->eval();
    uint64_t resets = 0;
    for (uint64_t t = 0; t < cycles; t++) {
        auto *r = v->rootp;
        // compare state and bus at the start of cycle t
        qbus bus;
        qstate s = m.s;
        bool same = r->core__DOT__A == s.A && r->core__DOT__X == s.X && r->core__DOT__Y == s.Y &&
                    r->core__DOT__SP == s.SP && r->core__DOT__PC == s.PC && r->core__DOT__IR == s.IR &&
                    r->core__DOT__MU == s.MU && r->core__DOT__F == s.F && v->rdata == s.rd;
        // the C side computes the bus of this cycle
        qstate tmp = s;
        qm_cycle(&tmp, s.rd, &bus);
        same = same && v->addr == bus.addr && v->space == bus.space && v->we == bus.we && v->wdata == bus.wdata;
        if (!same) {
            fprintf(stderr, "%s: mismatch at cycle %llu\n", name, (unsigned long long)t);
            fprintf(stderr, " rtl A=%04x X=%04x Y=%04x SP=%02x PC=%04x IR=%04x MU=%d F=%d rd=%04x addr=%04x sp=%d we=%d wd=%04x\n",
                    r->core__DOT__A, r->core__DOT__X, r->core__DOT__Y, r->core__DOT__SP, r->core__DOT__PC,
                    r->core__DOT__IR, r->core__DOT__MU, r->core__DOT__F, v->rdata, v->addr, v->space, v->we, v->wdata);
            fprintf(stderr, " c   A=%04x X=%04x Y=%04x SP=%02x PC=%04x IR=%04x MU=%d F=%d rd=%04x addr=%04x sp=%d we=%d wd=%04x\n",
                    s.A, s.X, s.Y, s.SP, s.PC, s.IR, s.MU, s.F, s.rd, bus.addr, bus.space, bus.we, bus.wdata);
            return 1;
        }
        // RTL-side memory: read-first, then write, at the rising edge
        uint16_t a = v->addr;
        uint16_t nrd = v->space ? (a < QM_RAM_WORDS ? ram[a] : 0) : m.rom[a & 0x7FFF];
        bool rtl_reset = false;
        if (v->we && v->space) {
            if (a < QM_RAM_WORDS) ram[a] = v->wdata;
            else if (a == QM_IO_RESET) rtl_reset = true;
        }
        v->clk = 1;
        v->eval();
        v->rdata = nrd;
        v->eval();
        v->clk = 0;
        v->eval();
        int c_reset = qm_tick(&m, nullptr);
        if (rtl_reset != (bool)c_reset) { fprintf(stderr, "%s: reset disagreement\n", name); return 1; }
        if (rtl_reset) {
            resets++;
            memset(ram, 0, sizeof ram);
            v->rst_n = 0; v->eval();
            v->rdata = m.rom[0];
            v->rst_n = 1; v->eval();
        }
    }
    if (memcmp(ram, m.ram, sizeof ram)) { fprintf(stderr, "%s: RAM differs\n", name); return 1; }
    printf("  %-22s %10llu cycles, %llu self-resets: agree\n", name, (unsigned long long)cycles,
           (unsigned long long)resets);
    return 0;
}

int main(int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    Vcore *v = new Vcore;
    int fails = 0;
    /* usage: ROM|- [cycles] [random-roms] [cycles-each] */
    const char *rom = argc > 1 ? argv[1] : "-";
    int nrand = argc > 3 ? atoi(argv[3]) : 10;
    uint64_t each = argc > 4 ? strtoull(argv[4], 0, 0) : 200000;
    if (strcmp(rom, "-")) {
        if (qm_load_rom(&m, rom) < 0) { perror(rom); return 2; }
        fails += run(v, argc > 2 ? strtoull(argv[2], 0, 0) : 100000, rom);
    }
    for (int k = 0; k < nrand; k++) {
        for (int i = 0; i < QM_ROM_WORDS; i++) {
            uint16_t w = rnd16();
            m.rom[i] = ((k & 1) && (i & 1)) ? (w & 0x03FF) : w;
        }
        // one ROM exercises the self-reset path
        if (k == 0) { m.rom[0] = 0x8000 | (0 << 11); m.rom[1] = 0x1234; m.rom[2] = 0x8000 | (3 << 11) | (1 << 9) | (1 << 8); m.rom[3] = 0xFFFF; }
        char name[64];
        snprintf(name, sizeof name, "random rom %d", k);
        fails += run(v, each, name);
    }
    printf(fails ? "FAIL\n" : "PASS\n");
    delete v;
    return fails != 0;
}
