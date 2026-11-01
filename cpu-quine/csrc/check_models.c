/* Checks 3a and 5: the generated C models agree with each other.
 *
 *   gate  : the levelized gate netlist (qgates.h) vs the microcycle stepper,
 *           every net that matters (all D inputs and the bus), every cycle;
 *   instr : the instruction stepper vs the microcycle stepper, the full
 *           flip-flop vector, RDATA and RAM at every instruction boundary.
 *
 * Programs: the given ROM image, and random ROMs (random words, so every
 * opcode, mode, self-reset and wrap-around case is exercised).
 *
 * usage: PROG ROM|- [cycles] [random-roms] [cycles-each]
 */
#include "qmachine.h"
#include "qgates.h"

static uint64_t rng = 88172645463325252ull;
static uint16_t rnd16(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (uint16_t)rng; }

static void regs_of(const qstate *s, uint16_t *r) {
    r[0] = s->A; r[1] = s->X; r[2] = s->Y; r[3] = s->SP; r[4] = s->PC; r[5] = s->IR; r[6] = s->MU; r[7] = s->F;
}

static int same_state(const qstate *a, const qstate *b) {
    return a->A == b->A && a->X == b->X && a->Y == b->Y && a->PC == b->PC && a->IR == b->IR &&
           a->rd == b->rd && a->SP == b->SP && a->F == b->F && a->MU == b->MU;
}

static void show(const char *tag, const qstate *s) {
    fprintf(stderr, "%s A=%04x X=%04x Y=%04x SP=%02x PC=%04x IR=%04x MU=%d F=%d rd=%04x\n", tag, s->A, s->X,
            s->Y, s->SP, s->PC, s->IR, s->MU, s->F, s->rd);
}

static qmachine ma, mb;
static uint8_t nets[QG_NBITS];

/* Run one program both ways.  Returns number of failures (0 or 1). */
static int run(uint64_t cycles, const char *name) {
    qm_power_on(&ma);
    memcpy(mb.rom, ma.rom, sizeof ma.rom);
    qm_power_on(&mb);
    ma.epoch = mb.epoch = 0;
    uint64_t boundaries = 0, resets = 0;
    while (boundaries < cycles) {
        /* advance the microcycle machine one instruction, checking gates each cycle */
        do {
            uint16_t regs[8], nxt[8];
            regs_of(&ma.s, regs);
            qg_eval(nets, regs, ma.s.rd);
            qbus bus;
            qstate before = ma.s;
            int reset = qm_tick(&ma, &bus);
            uint16_t ga = 0, gw = 0;
            for (int i = 0; i < 16; i++) { ga |= (uint16_t)(nets[qg_addr[i]] << i); gw |= (uint16_t)(nets[qg_wdata[i]] << i); }
            qg_next(nets, nxt);
            qstate g = before;
            g.A = nxt[0]; g.X = nxt[1]; g.Y = nxt[2]; g.SP = (uint8_t)nxt[3]; g.PC = nxt[4]; g.IR = nxt[5]; g.MU = (uint8_t)nxt[6]; g.F = (uint8_t)nxt[7];
            if (ga != bus.addr || gw != bus.wdata || nets[QG_SPACE] != bus.space || nets[QG_WE] != bus.we) {
                fprintf(stderr, "%s: gate bus mismatch at cycle %llu: addr %04x/%04x wdata %04x/%04x\n", name,
                        (unsigned long long)ma.cycle, ga, bus.addr, gw, bus.wdata);
                show("before", &before);
                return 1;
            }
            if (!reset) {
                g.rd = ma.s.rd;
                if (!same_state(&g, &ma.s)) {
                    fprintf(stderr, "%s: gate next-state mismatch at cycle %llu\n", name, (unsigned long long)ma.cycle);
                    show("before", &before); show("gates ", &g); show("model ", &ma.s);
                    return 1;
                }
            } else {
                resets++;
                break;
            }
        } while (ma.s.MU != 0);
        /* advance the instruction machine one instruction */
        uint64_t e0 = mb.epoch;
        int why = qm_run(&mb, mb.cycle + 1, NULL);
        if ((why == 2) != (mb.epoch != e0)) return 1;
        boundaries++;
        if (!same_state(&ma.s, &mb.s) || ma.cycle != mb.cycle || ma.epoch != mb.epoch ||
            memcmp(ma.ram, mb.ram, sizeof ma.ram)) {
            fprintf(stderr, "%s: instruction stepper mismatch after %llu instructions (cycle %llu/%llu)\n", name,
                    (unsigned long long)boundaries, (unsigned long long)ma.cycle, (unsigned long long)mb.cycle);
            show("micro", &ma.s); show("instr", &mb.s);
            return 1;
        }
        if (ma.s.rd != ma.rom[ma.s.PC]) {
            fprintf(stderr, "%s: RDATA != ROM[PC] at a boundary (cycle %llu)\n", name, (unsigned long long)ma.cycle);
            return 1;
        }
    }
    printf("  %-22s %10llu instructions, %llu self-resets: agree\n", name, (unsigned long long)boundaries,
           (unsigned long long)resets);
    return 0;
}

int main(int argc, char **argv) {
    int fails = 0;
    /* usage: ROM|- [cycles] [random-roms] [cycles-each] */
    const char *rom = argc > 1 ? argv[1] : "-";
    int nrand = argc > 3 ? atoi(argv[3]) : 20;
    uint64_t each = argc > 4 ? strtoull(argv[4], 0, 0) : 200000;
    if (strcmp(rom, "-")) {
        if (qm_load_rom(&ma, rom) < 0) { perror(rom); return 2; }
        fails += run(argc > 2 ? strtoull(argv[2], 0, 0) : 100000, rom);
    }
    for (int k = 0; k < nrand; k++) {
        for (int i = 0; i < QM_ROM_WORDS; i++) {
            uint16_t w = rnd16();
            /* bias towards short relative branches and low addresses so programs loop and touch RAM */
            if ((w & 0x8000) && (k & 1)) w &= 0xFFFF;
            ma.rom[i] = w;
            if ((i & 1) && (k & 2)) ma.rom[i] &= 0x03FF;
        }
        char name[64];
        snprintf(name, sizeof name, "random rom %d", k);
        fails += run(each, name);
    }
    printf(fails ? "FAIL\n" : "PASS\n");
    return fails != 0;
}
