/* Check 3: the whole chip at switch level against the gate-level model.
 *
 * Every transistor of the core is simulated with the reference switch-level
 * solver (ideal switches, unit-delay waves, see quine/switchsim.py).  At the
 * end of every half-cycle the value of every net node and the conduction of
 * every transistor must equal what the gate-level expansion predicts from the
 * register state and RDATA alone.  The memory bus seen by the switch-level
 * chip drives its own copy of the memory, so the run is closed-loop.
 *
 * usage: PROG ROM|- [cycles] [random-roms] [cycles-each]
 */
#include "qmachine.h"
#include "qgates.h"
#include "qswitch.h"

static uint8_t val[QS_NNODES], is_src[QS_NNODES], on[QS_NTRANS];
static uint32_t gated_off[QS_NNODES + 1], chan_off[QS_NNODES + 1];
static uint16_t gated_list[QS_NTRANS], chan_list[2 * QS_NTRANS];
static uint32_t stamp[QS_NNODES], cur_stamp;
static uint16_t wave[QS_NNODES], nwave, nxt[QS_NNODES], nnxt, aff[2 * QS_NTRANS + 64], naff;
static uint8_t in_nxt[QS_NNODES], is_gate[QS_NNODES];
static uint16_t grp[QS_NNODES], stk[QS_NNODES];
static long conflicts, floats, max_waves;

static void build(void) {
    for (int i = 0; i < QS_NTRANS; i++) {
        gated_off[qs_trans[i][1] + 1]++;
        chan_off[qs_trans[i][2] + 1]++;
        chan_off[qs_trans[i][3] + 1]++;
        is_gate[qs_trans[i][1]] = 1;
    }
    for (int n = 0; n < QS_NNODES; n++) { gated_off[n + 1] += gated_off[n]; chan_off[n + 1] += chan_off[n]; }
    static uint32_t gp[QS_NNODES], cp[QS_NNODES];
    for (int n = 0; n < QS_NNODES; n++) { gp[n] = gated_off[n]; cp[n] = chan_off[n]; }
    for (int i = 0; i < QS_NTRANS; i++) {
        gated_list[gp[qs_trans[i][1]]++] = (uint16_t)i;
        chan_list[cp[qs_trans[i][2]]++] = (uint16_t)i;
        chan_list[cp[qs_trans[i][3]]++] = (uint16_t)i;
    }
    for (int n = 0; n < 20; n++) is_src[n] = 1;
}

static inline int conducts(int i) { return val[qs_trans[i][1]] == qs_trans[i][0]; }

static void add_aff(int i) {
    aff[naff++] = qs_trans[i][2];
    aff[naff++] = qs_trans[i][3];
    if (naff > 2 * QS_NTRANS) { fprintf(stderr, "affected overflow\n"); exit(2); }
}

/* settle after the source nodes in wave[] changed (or everything) */
static void settle(int everything) {
    long waves = 0;
    naff = 0;
    if (everything) {
        for (int i = 0; i < QS_NTRANS; i++) on[i] = (uint8_t)conducts(i);
        nwave = 0;
        for (int n = 0; n < QS_NNODES; n++) if (!is_src[n]) { aff[naff++] = (uint16_t)n; }
    } else {
        for (int k = 0; k < nwave; k++)
            for (uint32_t j = chan_off[wave[k]]; j < chan_off[wave[k] + 1]; j++) add_aff(chan_list[j]);
    }
    while (nwave || naff) {
        for (int k = 0; k < nwave; k++) {
            int n = wave[k];
            for (uint32_t j = gated_off[n]; j < gated_off[n + 1]; j++) {
                int i = gated_list[j];
                uint8_t c = (uint8_t)conducts(i);
                if (c != on[i]) { on[i] = c; add_aff(i); }
            }
        }
        cur_stamp++;
        nnxt = 0;
        for (int a = 0; a < naff; a++) {
            int s = aff[a];
            if (is_src[s] || stamp[s] == cur_stamp) continue;
            int ng = 0, sp = 0, src0 = 0, src1 = 0, hasgate = 0;
            stamp[s] = cur_stamp; grp[ng++] = (uint16_t)s; stk[sp++] = (uint16_t)s;
            while (sp) {
                int n = stk[--sp];
                hasgate |= is_gate[n];
                for (uint32_t j = chan_off[n]; j < chan_off[n + 1]; j++) {
                    int i = chan_list[j];
                    if (!on[i]) continue;
                    int m = qs_trans[i][2] == n ? qs_trans[i][3] : qs_trans[i][2];
                    if (is_src[m]) { if (val[m]) src1 = 1; else src0 = 1; continue; }
                    if (stamp[m] != cur_stamp) { stamp[m] = cur_stamp; grp[ng++] = (uint16_t)m; stk[sp++] = (uint16_t)m; }
                }
            }
            if (src0 && src1) { conflicts++; continue; }
            if (!src0 && !src1) { if (hasgate && !everything) floats++; continue; }
            uint8_t v = (uint8_t)src1;
            for (int g = 0; g < ng; g++)
                if (val[grp[g]] != v) { val[grp[g]] = v; if (!in_nxt[grp[g]]) { in_nxt[grp[g]] = 1; nxt[nnxt++] = grp[g]; } }
        }
        naff = 0;
        nwave = nnxt;
        for (int k = 0; k < nnxt; k++) { wave[k] = nxt[k]; in_nxt[nxt[k]] = 0; }
        if (++waves > 10000) { fprintf(stderr, "oscillation\n"); exit(2); }
    }
    if (waves > max_waves) max_waves = waves;
}

static void set_input(int n, int v) {
    if (val[n] != v) { val[n] = (uint8_t)v; wave[nwave++] = (uint16_t)n; }
}

static void set_rdata(uint16_t rd) {
    nwave = 0;
    for (int i = 0; i < 16; i++) set_input(QS_RDATA0 + i, (rd >> i) & 1);
    settle(0);
}

static void set_one(int n, int v) { nwave = 0; set_input(n, v); settle(0); }

static uint16_t node_word(const uint16_t *nets) {
    uint16_t w = 0;
    for (int i = 0; i < 16; i++) w |= (uint16_t)(val[qs_net_node[nets[i]]] << i);
    return w;
}

static uint8_t expect[QG_NBITS];
static long compared_t, compared_n;

static int compare(qmachine *m, int ck, const char *name) {
    uint16_t regs[8] = {m->s.A, m->s.X, m->s.Y, m->s.SP, m->s.PC, m->s.IR, m->s.MU, m->s.F};
    qg_eval(expect, regs, m->s.rd);
    for (int i = 0; i < QG_NBITS; i++) {
        if (i == QG_CONST0 || i == QG_CONST1) continue;
        int n = qs_net_node[i];
        if (n == QS_CLK || n == QS_RSTN) continue;
        if (val[n] != expect[i]) {
            fprintf(stderr, "%s: net bit %d differs (switch %d, gates %d) at cycle %llu ck=%d\n", name, i, val[n], expect[i],
                    (unsigned long long)m->cycle, ck);
            return 1;
        }
        compared_n++;
    }
    for (int c = 0; c < QS_NCELLS; c++) {
        int t = qs_cells[c][0], k = 0;
        for (int j = 0; j < qs_type_nk[t]; j++) k |= expect[qs_cells[c][3 + j]] << j;
        if (qs_type_dff[t]) k |= ck << 2;
        uint32_t want = qs_pattern[t][k];
        for (int j = 0; j < qs_cells[c][2]; j++) {
            int i = qs_cells[c][1] + j;
            if ((uint32_t)conducts(i) != ((want >> j) & 1)) {
                fprintf(stderr, "%s: transistor %d (cell %d) differs at cycle %llu ck=%d\n", name, i, c,
                        (unsigned long long)m->cycle, ck);
                return 1;
            }
            compared_t++;
        }
    }
    return 0;
}

static qmachine m;
static uint16_t ram[QM_RAM_WORDS];

static void chip_reset(void) {
    nwave = 0;
    set_input(QS_RSTN, 0);
    set_input(QS_CLK, 0);
    settle(0);
    set_rdata(m.rom[0]);
    set_one(QS_CLK, 1);
    set_one(QS_CLK, 0);
    set_one(QS_RSTN, 1);
}

static int run(uint64_t cycles, const char *name) {
    qm_power_on(&m);
    memset(ram, 0, sizeof ram);
    memset(val, 0, sizeof val);
    val[QS_VDD] = 1;
    for (int i = 0; i < 16; i++) val[QS_RDATA0 + i] = (m.rom[0] >> i) & 1;
    val[QS_CLK] = 0;
    val[QS_RSTN] = 0;
    settle(1);
    chip_reset();
    long resets = 0;
    if (compare(&m, 0, name)) return 1;
    for (uint64_t t = 0; t < cycles; t++) {
        uint16_t a = node_word(qg_addr), wd = node_word(qg_wdata);
        int sp = val[qs_net_node[QG_SPACE]], we = val[qs_net_node[QG_WE]];
        qbus bus;
        int reset = qm_tick(&m, &bus);
        if (a != bus.addr || wd != bus.wdata || sp != bus.space || we != bus.we) {
            fprintf(stderr, "%s: bus differs at cycle %llu\n", name, (unsigned long long)t);
            return 1;
        }
        uint16_t nrd = sp ? (a < QM_RAM_WORDS ? ram[a] : 0) : m.rom[a & 0x7FFF];
        int chip_reset_req = 0;
        if (we && sp) { if (a < QM_RAM_WORDS) ram[a] = wd; else if (a == QM_IO_RESET) chip_reset_req = 1; }
        set_one(QS_CLK, 1);
        if (chip_reset_req != reset) { fprintf(stderr, "%s: reset disagreement\n", name); return 1; }
        if (reset) {
            resets++;
            memset(ram, 0, sizeof ram);
            chip_reset();
            if (compare(&m, 0, name)) return 1;
            continue;
        }
        set_rdata(nrd);
        if (compare(&m, 1, name)) return 1;
        set_one(QS_CLK, 0);
        if (compare(&m, 0, name)) return 1;
    }
    if (memcmp(ram, m.ram, sizeof ram)) { fprintf(stderr, "%s: RAM differs\n", name); return 1; }
    if (conflicts || floats) {
        fprintf(stderr, "%s: %ld conflicts, %ld floating gate nodes\n", name, conflicts, floats);
        return 1;
    }
    printf("  %-22s %8llu cycles, %ld self-resets: all nets and transistors agree\n", name,
           (unsigned long long)cycles, resets);
    return 0;
}

static uint64_t rng = 0x2545F4914F6CDD1Dull;
static uint16_t rnd16(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (uint16_t)rng; }

int main(int argc, char **argv) {
    build();
    int fails = 0;
    /* usage: ROM|- [cycles] [random-roms] [cycles-each] */
    const char *rom = argc > 1 ? argv[1] : "-";
    int nrand = argc > 3 ? atoi(argv[3]) : 4;
    uint64_t each = argc > 4 ? strtoull(argv[4], 0, 0) : 20000;
    if (strcmp(rom, "-")) {
        if (qm_load_rom(&m, rom) < 0) { perror(rom); return 2; }
        fails += run(argc > 2 ? strtoull(argv[2], 0, 0) : 100000, rom);
    }
    for (int k = 0; k < nrand; k++) {
        for (int i = 0; i < QM_ROM_WORDS; i++) {
            uint16_t w = rnd16();
            m.rom[i] = ((k & 1) && (i & 1)) ? (w & 0x03FF) : w;
        }
        if (k == 0) { m.rom[0] = 0x8000; m.rom[1] = 0x1234; m.rom[2] = 0x9B00; m.rom[3] = 0xFFFF; }
        char name[64];
        snprintf(name, sizeof name, "random rom %d", k);
        fails += run(each, name);
    }
    printf("  compared %ld transistor states and %ld net values; longest settle %ld waves\n", compared_t, compared_n,
           max_waves);
    printf(fails ? "FAIL\n" : "PASS\n");
    return fails != 0;
}
