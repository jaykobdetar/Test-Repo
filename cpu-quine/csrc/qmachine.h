/* The modelled system: core + 32K-word ROM + 32K-word RAM + I/O.
 *
 * Memory semantics (fixed by the spec, shared by every model):
 *   - the address driven in cycle t is read at the edge ending cycle t and the
 *     data appear on RDATA in cycle t+1; a read happens every cycle;
 *   - ROM reads use address bits [14:0]; RAM reads above 0x7FFF return 0;
 *   - a write (RAM space only) is read-first: the read returns the old word;
 *   - writes to 0xFFFE go to the debug port, 0xFFFF requests a self-reset,
 *     other writes above 0x7FFF are ignored;
 *   - during reset the core drives PC=0 in ROM space, so RDATA = ROM[0] at
 *     cycle 0, the first rising edge after reset release.
 */
#ifndef QMACHINE_H
#define QMACHINE_H
#include <stdint.h>
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include "qmodel.h"

typedef struct qmachine qmachine;
typedef void (*qdebug_fn)(qmachine *m, uint16_t value);

struct qmachine {
    qstate s;
    uint16_t rom[QM_ROM_WORDS];
    uint16_t ram[QM_RAM_WORDS];
    uint64_t cycle;   /* cycles since the last reset release */
    uint64_t total;   /* physical cycles, including RAM-clear periods */
    uint64_t instrs;  /* instructions completed by qm_run (statistics) */
    uint32_t epoch;
    int reset_req;
    qdebug_fn on_debug;
    void *user;
};

static inline void qm_power_on(qmachine *m) {
    memset(&m->s, 0, sizeof m->s);
    memset(m->ram, 0, sizeof m->ram);
    m->s.rd = m->rom[0];
    m->cycle = 0;
    m->total += QM_RESET_CLEAR_CYCLES;
    m->reset_req = 0;
}

static inline int qm_load_rom(qmachine *m, const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    memset(m->rom, 0, sizeof m->rom);
    size_t n = fread(m->rom, 2, QM_ROM_WORDS, f);
    fclose(f);
    return (int)n;
}

static inline uint16_t qm_read(const qmachine *m, uint16_t addr, int space) {
    if (!space) return m->rom[addr & 0x7FFF];
    return addr < QM_RAM_WORDS ? m->ram[addr] : 0;
}

static inline void qm_write(qmachine *m, uint16_t addr, uint16_t v) {
    if (addr < QM_RAM_WORDS) m->ram[addr] = v;
    else if (addr == QM_IO_DEBUG) { if (m->on_debug) m->on_debug(m, v); }
    else if (addr == QM_IO_RESET) m->reset_req = 1;
}

/* One clock cycle with the microcycle stepper. Returns 1 if a self-reset happened. */
static inline int qm_tick(qmachine *m, qbus *bus_out) {
    qbus bus;
    qm_cycle(&m->s, m->s.rd, &bus);
    uint16_t nrd = qm_read(m, bus.addr, bus.space);
    if (bus.we && bus.space) qm_write(m, bus.addr, bus.wdata);
    m->s.rd = nrd;
    m->cycle++;
    m->total++;
    if (bus_out) *bus_out = bus;
    if (m->reset_req) {
        m->epoch++;
        qm_power_on(m);
        return 1;
    }
    return 0;
}

/* Instruction stepper: run whole instructions (state must be at MU=0) until
 * cycle >= limit, bpmap[PC] at a boundary, or a self-reset.  Returns
 * 0 = limit, 1 = breakpoint, 2 = self-reset.  The body is in qrun.inc so a
 * caller can instantiate variants with instrumented memory access. */
#define QM_RUN_NAME qm_run
#define QM_READ_ROM(a) (m->rom[(a) & 0x7FFF])
#define QM_READ_RAM(a) ((a) < QM_RAM_WORDS ? m->ram[(a)] : 0)
#define QM_STORE_RAM(a, v) (m->ram[(a)] = (v))
#include "qrun.inc"
#endif
