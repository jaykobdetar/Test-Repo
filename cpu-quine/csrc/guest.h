/* Reading the nested machines out of the physical machine's RAM.
 *
 * Level 0 is the physical machine.  Level k+1's RAM lives in level k's RAM
 * behind level k's page table (PTAB at $0100): guest address a maps to
 * a + PTAB[a >> 8] in level k's address space, and anything at or above
 * $8000 reads as zero, exactly as the engine's GREAD macro does.
 */
#ifndef GUEST_H
#define GUEST_H
#include "qmachine.h"
#include "qengine.h"

static uint8_t qe_syncmap[QM_ROM_WORDS], qe_latchmap[QM_ROM_WORDS];

static void qe_init_maps(void) {
    for (int i = 0; i < QE_NSYNC; i++) qe_syncmap[qe_sync[i]] = 1;
    for (int i = 0; i < QE_NLATCH; i++) qe_latchmap[qe_latched[i]] = 1;
}

/* word at address a of level k's RAM (k = 0: physical) */
static uint16_t lv_read(const qmachine *m, int k, uint16_t a) {
    if (a >= QM_RAM_WORDS) return 0;
    if (k == 0) return m->ram[a];
    uint16_t entry = lv_read(m, k - 1, (uint16_t)(QE_PTAB + (a >> 8)));
    uint16_t phys = (uint16_t)(a + entry);
    return phys < QM_RAM_WORDS ? lv_read(m, k - 1, phys) : 0;
}

typedef struct {
    qstate s;  /* registers; F materialized from the lazy flags */
    uint64_t cycle;
} gstate;

static uint8_t qe_flags(uint16_t res, uint16_t c) {
    return (uint8_t)((res == 0) | ((res >> 15) << 1) | ((c & 1) << 2));
}

/* compact (instruction-boundary) state of level k+1, stored in level k's RAM */
static gstate guest_compact(const qmachine *m, int k) {
    gstate g;
    memset(&g, 0, sizeof g);
    g.s.A = lv_read(m, k, QE_gA);
    g.s.X = lv_read(m, k, QE_gX);
    g.s.Y = lv_read(m, k, QE_gY);
    g.s.SP = (uint8_t)lv_read(m, k, QE_gSP);
    g.s.PC = lv_read(m, k, QE_gPC);
    g.s.IR = lv_read(m, k, QE_gIR);
    g.s.MU = 0;
    g.s.F = qe_flags(lv_read(m, k, QE_gRES), lv_read(m, k, QE_gC));
    g.s.rd = m->rom[g.s.PC & 0x7FFF];
    g.cycle = lv_read(m, k, QE_gCYC0) | (uint64_t)lv_read(m, k, QE_gCYC1) << 16 | (uint64_t)lv_read(m, k, QE_gCYC2) << 32;
    return g;
}

/* full (watch-mode) state of level k+1 */
static gstate guest_full(const qmachine *m, int k) {
    gstate g = guest_compact(m, k);
    g.s.MU = (uint8_t)lv_read(m, k, QE_gMU);
    g.s.F = (uint8_t)lv_read(m, k, QE_gF);
    g.s.rd = lv_read(m, k, QE_gRD);
    return g;
}

/* Is level k at an instruction boundary with its guest's compact state
 * complete?  Level 0 must be stopped at a boundary; deeper levels are at a
 * boundary when their parent is at a sync point. */
static int at_sync(const qmachine *m, int k) {
    if (!qe_syncmap[m->s.PC & 0x7FFF]) return 0;
    for (int j = 0; j < k; j++)
        if (!qe_syncmap[lv_read(m, j, QE_gPC) & 0x7FFF]) return 0;
    return 1;
}

static int same_regs(const qstate *a, const qstate *b) {
    return a->A == b->A && a->X == b->X && a->Y == b->Y && a->SP == b->SP && a->PC == b->PC && a->IR == b->IR &&
           a->F == b->F && a->MU == b->MU && a->rd == b->rd;
}

static void print_state(FILE *f, const char *tag, const qstate *s, uint64_t cyc) {
    fprintf(f, "%s cycle=%llu A=%04x X=%04x Y=%04x SP=%02x PC=%04x IR=%04x F=%d MU=%d rd=%04x\n", tag,
            (unsigned long long)cyc, s->A, s->X, s->Y, s->SP, s->PC, s->IR, s->F, s->MU, s->rd);
}
#endif
