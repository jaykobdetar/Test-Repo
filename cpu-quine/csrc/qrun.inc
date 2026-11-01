/* Template for the instruction-stepper loop.  Define QM_RUN_NAME,
 * QM_READ_ROM(a), QM_READ_RAM(a) and QM_STORE_RAM(a, v) before including. */
#define QM_WRITE(a, v, n) do { uint16_t a__ = (a); \
    if (a__ < QM_RAM_WORDS) QM_STORE_RAM(a__, (v)); \
    else if (a__ == QM_IO_DEBUG) { if (m->on_debug) { m->total += cyc + (n) - m->cycle; m->cycle = cyc + (n); m->on_debug(m, (v)); } } \
    else if (a__ == QM_IO_RESET) { cyc += (n); goto qm_abort; } } while (0)

static int QM_RUN_NAME(qmachine *m, uint64_t limit, const uint8_t *bpmap) {
    uint16_t A = m->s.A, X = m->s.X, Y = m->s.Y, PC = m->s.PC, IR = m->s.IR, rd = m->s.rd;
    uint8_t SP = m->s.SP, F = m->s.F;
    uint64_t cyc = m->cycle, start = m->cycle;
    int why = 0;
    uint64_t ni = 0;
    while (cyc < limit) {
        if (bpmap && bpmap[PC] && cyc != start) { why = 1; break; }
        ni++;
#define QM_RUN_BODY
#include "qmodel.h"
#undef QM_RUN_BODY
    }
    m->s.A = A; m->s.X = X; m->s.Y = Y; m->s.PC = PC; m->s.IR = IR; m->s.rd = rd;
    m->s.SP = SP; m->s.F = F; m->s.MU = 0;
    m->total += cyc - m->cycle;
    m->cycle = cyc;
    m->instrs += ni;
    return why;
qm_abort:
    /* the write cycle completed; reset asserts at the edge that ends it */
    m->total += cyc - m->cycle;
    m->cycle = cyc;
    m->instrs += ni;
    m->epoch++;
    qm_power_on(m);
    return 2;
}
#undef QM_WRITE
#undef QM_RUN_NAME
#undef QM_READ_ROM
#undef QM_READ_RAM
#undef QM_STORE_RAM
