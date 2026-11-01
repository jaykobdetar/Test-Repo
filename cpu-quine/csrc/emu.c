/* CPU-Quine system emulator and host-side checks.
 *
 *   emu ROM run N            run N cycles and report speed and progress
 *   emu ROM frames DIR N K   run N cycles, dump every K-th drawn frame as PBM
 *   emu ROM log FILE N       run N cycles, log every drawn frame (for budget.py)
 *   emu ROM check N          checks 6, 7, 8 over N cycles (see below)
 *   emu ROM hashes N         check 10 over N cycles
 *   emu ROM profile N        instructions per replayed instruction, over N cycles
 *   emu ROM pages N          cycle of the first non-zero write to each own page
 *
 * check 6 (target code):  every time the physical machine finishes replaying
 *   one guest instruction, the guest's registers and cycle count, as held in
 *   RAM by the ROM's own stepper, must equal an independent C model of the
 *   same machine run to the same cycle; in watch mode the same holds for the
 *   full flip-flop state after every replayed clock cycle.
 * check 7 (quine property): likewise for level 2 (read through level 1's
 *   page table inside level 0's RAM) and level 3, and periodically every word
 *   of each level's RAM is compared with the model's RAM.
 * check 8 (memory hygiene): level 0 never reads a word of its working set it
 *   has not written since reset, never touches an unallocated page, and each
 *   level holds exactly its working set of own pages.
 * check 10: every drawn frame's net-state hash (debug port) equals the hash
 *   of the gate-level expansion of the model state at that guest cycle.
 */
#include <time.h>
#include "qmachine.h"
#include "qgates.h"
#include "guest.h"

/* ---- instrumented stepper for check 8 ---- */
static uint8_t wrote[QM_RAM_WORDS], touched[QM_RAM_WORDS / 256];
static long hyg_unwritten, hyg_unalloc;
static uint16_t hyg_first_unwritten = 0xFFFF, hyg_first_unalloc = 0xFFFF;

static inline uint16_t track_read(qmachine *m, uint16_t a) {
    if (a >= QM_RAM_WORDS) return 0;
    int p = a >> 8;
    touched[p] = 1;
    if (p < QE_WORKING_PAGES && !wrote[a]) { if (!hyg_unwritten++) hyg_first_unwritten = a; }
    if (p >= QE_FIRST_GUEST_PAGE && p >= m->ram[QE_nextp]) { if (!hyg_unalloc++) hyg_first_unalloc = a; }
    return m->ram[a];
}
static inline void track_write(qmachine *m, uint16_t a, uint16_t v) {
    int p = a >> 8;
    touched[p] = 1;
    wrote[a] = 1;
    /* a page is allocated by the write that follows the PTAB update */
    if (p >= QE_FIRST_GUEST_PAGE && p >= m->ram[QE_nextp]) { if (!hyg_unalloc++) hyg_first_unalloc = a; }
    m->ram[a] = v;
}
#define QM_RUN_NAME qm_run_track
#define QM_READ_ROM(a) (m->rom[(a) & 0x7FFF])
#define QM_READ_RAM(a) track_read(m, (a))
#define QM_STORE_RAM(a, v) track_write(m, (a), (v))
#include "qrun.inc"

static qmachine M;            /* the physical machine, level 0 */
#define MAXLV 4
static qmachine R[MAXLV + 1]; /* reference models for levels 1..MAXLV */
static uint8_t stopmap[QM_ROM_WORDS];

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

/* advance a reference machine to exactly cycle target */
static int ref_to(qmachine *r, uint64_t target) {
    if (r->cycle > target) return -1;
    if (r->s.MU == 0 && target > r->cycle + 4) qm_run(r, target - 3, NULL);
    while (r->cycle < target) qm_tick(r, NULL);
    return 0;
}

static void ref_reset_all(void) {
    for (int k = 1; k <= MAXLV; k++) {
        memcpy(R[k].rom, M.rom, sizeof M.rom);
        R[k].on_debug = NULL;
        R[k].total = 0;
        qm_power_on(&R[k]);
    }
}

/* ---- frame tuples from the debug port ---- */
typedef struct { uint64_t l0, l1; uint16_t hash, mode; } frame_t;
static frame_t *frames;
static size_t nframes, capframes;
static uint16_t dbg[4];
static int ndbg;
static FILE *frame_log;
static const char *frame_dir;
static long frame_every = 1;

static void dump_pbm(const char *dir, size_t n) {
    char path[512];
    snprintf(path, sizeof path, "%s/frame_%06zu.pbm", dir, n);
    FILE *f = fopen(path, "wb");
    if (!f) { perror(path); exit(2); }
    fprintf(f, "P4\n320 240\n");
    for (int i = 0; i < QE_FB_WORDS; i++) {
        uint16_t w = M.ram[QE_FB + i];
        fputc(w >> 8, f);
        fputc(w & 0xFF, f);
    }
    fclose(f);
}

static void on_debug(qmachine *m, uint16_t v) {
    dbg[ndbg++] = v;
    if (ndbg < 4) return;
    ndbg = 0;
    if (nframes == capframes) {
        capframes = capframes ? 2 * capframes : 1024;
        frames = realloc(frames, capframes * sizeof *frames);
    }
    frame_t fr = {m->total, dbg[0] | (uint64_t)dbg[1] << 16 | (uint64_t)dbg[2] << 32, dbg[3], m->ram[QE_mode]};
    frames[nframes] = fr;
    if (frame_log)
        fprintf(frame_log, "%llu %llu %u %llu %u %u\n", (unsigned long long)fr.l0, (unsigned long long)m->cycle, m->epoch,
                (unsigned long long)fr.l1, fr.mode, m->ram[QE_nextp] - QE_FIRST_GUEST_PAGE);
    if (frame_dir && nframes % frame_every == 0) dump_pbm(frame_dir, nframes);
    nframes++;
}

/* ---- run modes ---- */
static void report(const char *what, uint64_t cycles, double secs) {
    gstate g1 = guest_compact(&M, 0);
    printf("%s: %llu cycles (%.3g s at 50 MHz) in %.2f s host, %.0f Mcycles/s; %zu frames; epoch %u; "
           "level 1 at cycle %llu (%.3g s); pages used %d\n",
           what, (unsigned long long)cycles, cycles / 50e6, secs, cycles / secs / 1e6, nframes, M.epoch,
           (unsigned long long)g1.cycle, g1.cycle / 50e6, M.ram[QE_nextp] - QE_FIRST_GUEST_PAGE);
}

static void run_plain(uint64_t n) {
    double t0 = now();
    uint64_t end = M.total + n;
    while (M.total < end) {
        uint64_t left = end - M.total;
        qm_run(&M, M.cycle + left, NULL);
    }
    report("run", n, now() - t0);
}

static long fails;
static long checked[MAXLV + 1], ramchecks[MAXLV + 1], latchchecks[MAXLV + 1];

static int check_level(int k, int full) {
    /* level k+1 state from level k's RAM vs reference R[k+1] */
    gstate g = full ? guest_full(&M, k) : guest_compact(&M, k);
    qmachine *r = &R[k + 1];
    if (ref_to(r, g.cycle) < 0) {
        fprintf(stderr, "level %d: cycle went backwards (%llu < %llu)\n", k + 1, (unsigned long long)g.cycle,
                (unsigned long long)r->cycle);
        return 1;
    }
    qstate rs = r->s;
    if (!full && rs.MU != 0) {
        fprintf(stderr, "level %d: replay claims an instruction boundary at cycle %llu, model is at MU=%d\n", k + 1,
                (unsigned long long)g.cycle, rs.MU);
        return 1;
    }
    if (!same_regs(&g.s, &rs)) {
        fprintf(stderr, "level %d: replayed state differs from the model at L0 cycle %llu\n", k + 1,
                (unsigned long long)M.total);
        print_state(stderr, "  replay", &g.s, g.cycle);
        print_state(stderr, "  model ", &rs, r->cycle);
        return 1;
    }
    checked[k + 1]++;
    return 0;
}

static int check_ram(int k) {
    for (uint32_t a = 0; a < QM_RAM_WORDS; a++) {
        uint16_t v = lv_read(&M, k + 1, (uint16_t)a);
        if (v != R[k + 1].ram[a]) {
            fprintf(stderr, "level %d RAM[%04x] = %04x, model %04x (cycle %llu)\n", k + 1, a, v, R[k + 1].ram[a],
                    (unsigned long long)R[k + 1].cycle);
            return 1;
        }
    }
    ramchecks[k + 1]++;
    return 0;
}

static void run_check(uint64_t n) {
    double t0 = now();
    for (int i = 0; i < QM_ROM_WORDS; i++) stopmap[i] = qe_syncmap[i] | qe_latchmap[i];
    ref_reset_all();
    uint64_t end = M.total + n, next_ram[MAXLV + 1] = {0};
    uint32_t epoch = M.epoch;
    while (M.total < end && !fails) {
        int why = qm_run_track(&M, M.cycle + (end - M.total), stopmap);
        if (why == 2 || M.epoch != epoch) {
            epoch = M.epoch;
            ref_reset_all();
            memset(wrote, 0, sizeof wrote);
            continue;
        }
        if (why != 1) break;
        /* Walk down the levels.  Level k at a sync point: level k+1's compact
         * state is complete and level k+1 is at an instruction boundary, so
         * descend.  Level k at a watch-mode latch point: level k+1's full
         * state (mid-instruction) is complete; stop there. */
        for (int k = 0; k < MAXLV && !fails; k++) {
            uint16_t pc = k == 0 ? M.s.PC : lv_read(&M, k - 1, QE_gPC);
            pc &= 0x7FFF;
            if (qe_latchmap[pc]) {
                latchchecks[k + 1]++;
                fails += check_level(k, 1);
                break;
            }
            if (!qe_syncmap[pc]) break;
            fails += check_level(k, 0);
            if (!fails && M.total >= next_ram[k + 1]) {
                next_ram[k + 1] = M.total + (1ull << (22 + 2 * k));
                fails += check_ram(k);
            }
        }
    }
    double secs = now() - t0;
    report("check", n, secs);
    printf("  epochs: %u self-resets during the run\n", M.epoch);
    printf("  check 6: level 1 matches the model at %ld instruction boundaries and %ld watched cycles\n", checked[1],
           latchchecks[1]);
    for (int k = 2; k <= MAXLV; k++)
        printf("  check 7: level %d matches the model at %ld boundaries and %ld watched cycles\n", k,
               checked[k] - latchchecks[k], latchchecks[k]);
    printf("  check 7: whole-RAM comparisons per level:");
    for (int k = 1; k <= MAXLV; k++) printf(" L%d %ld", k, ramchecks[k]);
    printf("\n");
    /* check 8 */
    int pages[MAXLV + 1] = {0};
    for (int p = 0; p < QE_WORKING_PAGES; p++) pages[0] += touched[p];
    /* an own page g < 22 of level k is mapped to a physical page >= 22, so its
     * delta is neither 0 nor $8000 (unmapped); 0 means the table is not there */
    for (int k = 1; k <= MAXLV; k++)
        for (int g = 0; g < QE_WORKING_PAGES; g++) {
            uint16_t e = lv_read(&M, k - 1, (uint16_t)(QE_PTAB + g));
            pages[k] += e != 0x8000 && e != 0;
        }
    printf("  check 8: own pages in use per level:");
    for (int k = 0; k <= MAXLV; k++) printf(" L%d %d", k, pages[k]);
    printf(" (budget %d); reads of unwritten words %ld; accesses to unallocated pages %ld\n", QE_WORKING_PAGES,
           hyg_unwritten, hyg_unalloc);
    if (hyg_unwritten) fprintf(stderr, "first unwritten read at %04x\n", hyg_first_unwritten);
    if (hyg_unalloc) fprintf(stderr, "first unallocated access at %04x\n", hyg_first_unalloc);
    for (int k = 0; k <= MAXLV; k++)
        if (pages[k] > QE_WORKING_PAGES) { fprintf(stderr, "level %d exceeds its working set\n", k); fails++; }
    if (hyg_unwritten || hyg_unalloc) fails++;
    if (!checked[1]) { fprintf(stderr, "no level-1 boundaries were checked\n"); fails++; }
    printf(fails ? "FAIL\n" : "PASS\n");
}

/* Profile the replay: host instructions and cycles per replayed instruction,
 * measured over n cycles after the first watch phase. */
static void run_profile(uint64_t n) {
    run_plain(17ull * 50000000);  /* past the first watch phase */
    uint64_t c0 = M.total, i0 = M.instrs, l1i = 0;
    gstate g0 = guest_compact(&M, 0);
    while (M.total < c0 + n) {
        int why = qm_run(&M, M.cycle + (c0 + n - M.total), qe_syncmap);
        if (why == 1) l1i++;
        if (why == 2) break;
    }
    gstate g1 = guest_compact(&M, 0);
    double hi = (double)(M.instrs - i0), hc = (double)(M.total - c0), gc = (double)(g1.cycle - g0.cycle);
    printf("profile over %.3g cycles of replay:\n", hc);
    printf("  host: %.0f instructions, %.3f cycles per instruction\n", hi, hc / hi);
    printf("  guest: %llu instructions replayed, %.3f cycles per instruction (idle-skipped cycles included)\n",
           (unsigned long long)l1i, gc / l1i);
    printf("  host instructions per replayed guest instruction: %.1f\n", hi / l1i);
    printf("  host cycles per replayed guest instruction: %.1f\n", hc / l1i);
}

/* The own-page schedule: the cycle at which the machine first writes a
 * non-zero word to each of its own pages.  Same for every level. */
static void run_pages(uint64_t n) {
    static uint64_t first[QM_RAM_WORDS / 256];
    uint64_t end = M.total + n;
    int count = 0;
    while (M.total < end && count < QE_WORKING_PAGES) {
        qm_run(&M, M.cycle + 64, NULL);
        for (int p = 0; p < QE_WORKING_PAGES; p++) {
            if (first[p]) continue;
            for (int a = 0; a < 256; a++)
                if (M.ram[p * 256 + a]) { first[p] = M.cycle; count++; break; }
        }
    }
    uint64_t sorted[QE_WORKING_PAGES];
    int ns = 0;
    for (int p = 0; p < QE_WORKING_PAGES; p++) if (first[p]) sorted[ns++] = first[p];
    for (int i = 0; i < ns; i++) for (int j = i + 1; j < ns; j++) if (sorted[j] < sorted[i]) { uint64_t t = sorted[i]; sorted[i] = sorted[j]; sorted[j] = t; }
    printf("pages");
    for (int i = 0; i < ns; i++) printf(" %llu", (unsigned long long)sorted[i]);
    printf("\n");
}

static uint16_t hash_of(const qmachine *r) {
    static uint8_t n[QG_NBITS];
    uint16_t regs[8] = {r->s.A, r->s.X, r->s.Y, r->s.SP, r->s.PC, r->s.IR, r->s.MU, r->s.F};
    qg_eval(n, regs, r->s.rd);
    uint16_t h = 0;
    for (int w = 0; w < QG_NWORDS; w++) {
        uint16_t word = 0;
        for (int b = 0; b < 16 && 16 * w + b < QG_NBITS; b++) word |= (uint16_t)(n[16 * w + b] << b);
        if (w == QE_W_MISC) word |= QE_MISC_INIT;
        h = (uint16_t)((h << 1) | (h >> 15));
        h ^= word;
    }
    return h;
}

static void run_hashes(uint64_t n) {
    run_plain(n);
    ref_reset_all();
    long ok = 0, watch = 0;
    for (size_t i = 0; i < nframes; i++) {
        if (ref_to(&R[1], frames[i].l1) < 0) { fprintf(stderr, "frame %zu: cycle went backwards\n", i); fails++; break; }
        uint16_t h = hash_of(&R[1]);
        if (h != frames[i].hash) {
            fprintf(stderr, "frame %zu (level-1 cycle %llu): hash %04x, model %04x\n", i,
                    (unsigned long long)frames[i].l1, frames[i].hash, h);
            fails++;
            break;
        }
        ok++;
        watch += frames[i].mode == 0;
    }
    printf("  check 10: %ld drawn frames (%ld in watch mode) match the gate-level expansion of the model\n", ok, watch);
    if (!nframes) fails++;
    printf(fails ? "FAIL\n" : "PASS\n");
}

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: emu ROM run|frames|log|check|hashes ...\n");
        return 2;
    }
    if (qm_load_rom(&M, argv[1]) < 0) { perror(argv[1]); return 2; }
    qe_init_maps();
    M.on_debug = on_debug;
    qm_power_on(&M);
    const char *mode = argv[2];
    if (!strcmp(mode, "run")) run_plain(strtoull(argv[3], 0, 0));
    else if (!strcmp(mode, "frames") && argc >= 6) {
        frame_dir = argv[3];
        frame_every = atol(argv[5]);
        run_plain(strtoull(argv[4], 0, 0));
        dump_pbm(frame_dir, 999999);
    } else if (!strcmp(mode, "log") && argc >= 5) {
        frame_log = fopen(argv[3], "w");
        if (!frame_log) { perror(argv[3]); return 2; }
        run_plain(strtoull(argv[4], 0, 0));
        fclose(frame_log);
    } else if (!strcmp(mode, "check")) run_check(strtoull(argv[3], 0, 0));
    else if (!strcmp(mode, "hashes")) run_hashes(strtoull(argv[3], 0, 0));
    else if (!strcmp(mode, "profile")) run_profile(strtoull(argv[3], 0, 0));
    else if (!strcmp(mode, "pages")) run_pages(strtoull(argv[3], 0, 0));
    else { fprintf(stderr, "unknown mode %s\n", mode); return 2; }
    return fails != 0;
}
