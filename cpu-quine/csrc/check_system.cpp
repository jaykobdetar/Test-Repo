// Check 9 in simulation: the FPGA system top level (rtl/system.v) against the
// C model.
//
// The whole system RTL -- core, memories, reset controller with RAM clear,
// CRC observer, debug FIFO, UART and video scan-out -- runs in Verilator with
// the C model in lockstep.  Checked:
//   - every core flip-flop, every cycle the core advances (and RAM-clear periods
//     after power-up and every self-reset);
//   - the CRC-32 stream decoded from the UART line equals the CRC the host
//     computes from the model's flip-flops (this is the check the host would
//     run against real hardware);
//   - the debug-port words decoded from the UART equal the model's;
//   - each complete VGA frame captured while the framebuffer was unchanged
//     equals the model's framebuffer, pixel doubled.
//
// usage: check_system ROM.bin CYCLES +rom=ROM.hex
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <vector>
#include "Vsystem.h"
#include "Vsystem___024root.h"
#include "verilated.h"
extern "C" {
#include "qmachine.h"
#include "qengine.h"
}

static qmachine m;
static std::deque<uint32_t> want_crc;
static std::deque<uint16_t> want_dbg;
static uint32_t crc = 0xFFFFFFFFu;
static uint32_t folds;

static void fold_state(const qstate &s) {
    uint8_t v[12];
    // {3'b0, F, MU, SP, 1'b0, PC, IR, Y, X, A}, least significant bit first
    uint64_t lo = (uint64_t)s.A | (uint64_t)s.X << 16 | (uint64_t)s.Y << 32 | (uint64_t)s.IR << 48;
    uint32_t hi = (uint32_t)s.PC | (uint32_t)s.SP << 16 | (uint32_t)s.MU << 24 | (uint32_t)s.F << 26;
    for (int i = 0; i < 8; i++) v[i] = (uint8_t)(lo >> (8 * i));
    for (int i = 0; i < 4; i++) v[8 + i] = (uint8_t)(hi >> (8 * i));
    for (int i = 0; i < 12; i++) {
        crc ^= v[i];
        for (int b = 0; b < 8; b++) crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320u : 0);
    }
    if (++folds == (1u << 20)) { want_crc.push_back(crc); folds = 0; }
}

static void on_debug(qmachine *, uint16_t v) { want_dbg.push_back(v); }

// UART receiver: samples the line once per core cycle
struct Uart {
    int div, state = 0, count = 0, bit = 0;
    uint8_t byte = 0;
    std::vector<uint8_t> bytes;
    explicit Uart(int d) : div(d) {}
    void sample(int tx) {
        if (state == 0) {
            if (!tx) { state = 1; count = div / 2; }
        } else if (--count == 0) {
            count = div;
            if (state == 1) { if (tx) state = 0; else { state = 2; bit = 0; byte = 0; } }
            else if (state == 2) { byte |= (uint8_t)(tx << bit); if (++bit == 8) state = 3; }
            else { bytes.push_back(byte); state = 0; }
        }
    }
};

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: check_system ROM.bin CYCLES\n"); return 2; }
    Verilated::commandArgs(argc, argv);
    if (qm_load_rom(&m, argv[1]) < 0) { perror(argv[1]); return 2; }
    uint64_t cycles = strtoull(argv[2], 0, 0);
    m.on_debug = on_debug;
    qm_power_on(&m);
    Vsystem *v = new Vsystem;
    auto *r = v->rootp;
    Uart uart(434);
    long fails = 0, advanced = 0, resets = 0, crc_ok = 0, dbg_ok = 0, frames_ok = 0, frames_skipped = 0;
    // power-on reset
    v->por_n = 1; v->clk = 0; v->pix_clk = 0; v->eval();
    v->por_n = 0; v->eval();  // a falling edge, as at power-up
    for (int i = 0; i < 4; i++) { v->clk = 1; v->eval(); v->clk = 0; v->eval(); }
    v->por_n = 1; v->eval();
    // VGA capture
    static uint8_t shot[480][640];
    static uint16_t fb_start[QE_FB_WORDS];
    int vx = 0, vy = -1, prev_vs = 0, capturing = 0;  // a falling edge needs a high first
    size_t parsed = 0;
    for (uint64_t t = 0; t < cycles && !fails; t++) {
        bool adv = r->system__DOT__core_rst_n;
        if (adv) {
            // compare the flip-flops before the edge, fold them, step the model
            qstate s = m.s;
            const uint32_t *st = r->system__DOT__state.data();
            uint32_t w0 = st[0], w1 = st[1], w2 = st[2];
            uint16_t A = w0 & 0xFFFF, X = w0 >> 16, Y = w1 & 0xFFFF, SP = (w1 >> 16) & 0xFF;
            uint16_t PC = (uint16_t)(((w1 >> 24) | (w2 << 8)) & 0x7FFF);
            uint16_t IR = (w2 >> 7) & 0xFFFF;
            uint8_t MU = (w2 >> 23) & 3, F = (w2 >> 25) & 7;
            if (A != s.A || X != s.X || Y != s.Y || SP != s.SP || PC != s.PC || IR != s.IR || MU != s.MU || F != s.F ||
                r->system__DOT__rdata != s.rd) {
                fprintf(stderr, "flip-flops differ at core cycle %llu: rtl PC=%04x IR=%04x MU=%d, model PC=%04x IR=%04x MU=%d\n",
                        (unsigned long long)m.cycle, PC, IR, MU, s.PC, s.IR, s.MU);
                fails++;
                break;
            }
            fold_state(s);
            if (qm_tick(&m, nullptr)) resets++;
            advanced++;
        }
        // one core clock cycle; the pixel clock runs at half the rate
        v->clk = 1;
        if (t & 1) v->pix_clk = 1;
        v->eval();
        v->clk = 0;
        if (t & 1) v->pix_clk = 0;
        v->eval();
        uart.sample(v->uart_tx);
        // video: after each pixel-clock edge the outputs belong to one pixel.
        // The vsync falling edge is pixel (0, 490); count from there.
        if (t & 1) {
            int vs = v->vga_vsync;
            if (prev_vs && !vs) {
                if (capturing) {
                    if (!memcmp(fb_start, m.ram + QE_FB, sizeof fb_start)) {
                        for (int y = 0; y < 480 && !fails; y++)
                            for (int x = 0; x < 640; x++) {
                                uint16_t w = fb_start[(y / 2) * 20 + (x / 2) / 16];
                                int want = (w >> (15 - (x / 2) % 16)) & 1;
                                if (shot[y][x] != want) {
                                    fprintf(stderr, "VGA pixel (%d,%d) = %d, framebuffer %d\n", x, y, shot[y][x], want);
                                    fails++;
                                    break;
                                }
                            }
                        frames_ok += !fails;
                    } else frames_skipped++;
                }
                capturing = vy >= 0;
                memcpy(fb_start, m.ram + QE_FB, sizeof fb_start);
                vx = 0;
                vy = 490;
            } else if (vy >= 0 && ++vx == 800) {
                vx = 0;
                if (++vy == 525) vy = 0;
            }
            if (vy >= 0) {
                int hs_want = !(vx >= 656 && vx < 752), vs_want = !(vy >= 490 && vy < 492);
                if (v->vga_hsync != hs_want || vs != vs_want) {
                    fprintf(stderr, "VGA sync wrong at (%d,%d)\n", vx, vy);
                    fails++;
                }
                if (vx < 640 && vy < 480) shot[vy][vx] = v->vga_pixel;
            }
            prev_vs = vs;
        }
        // decode UART messages
        while (parsed < uart.bytes.size()) {
            uint8_t tag = uart.bytes[parsed];
            if (tag == 0xA5 && uart.bytes.size() >= parsed + 5) {
                uint32_t c = 0;
                for (int i = 0; i < 4; i++) c |= (uint32_t)uart.bytes[parsed + 1 + i] << (8 * i);
                if (want_crc.empty() || want_crc.front() != c) {
                    fprintf(stderr, "UART CRC %08x, host expects %08x\n", c, want_crc.empty() ? 0 : want_crc.front());
                    fails++;
                } else { want_crc.pop_front(); crc_ok++; }
                parsed += 5;
            } else if (tag == 0x5A && uart.bytes.size() >= parsed + 3) {
                uint16_t w = (uint16_t)(uart.bytes[parsed + 1] | uart.bytes[parsed + 2] << 8);
                if (want_dbg.empty() || want_dbg.front() != w) {
                    fprintf(stderr, "UART debug word %04x, model %04x\n", w, want_dbg.empty() ? 0 : want_dbg.front());
                    fails++;
                } else { want_dbg.pop_front(); dbg_ok++; }
                parsed += 3;
            } else if (tag != 0xA5 && tag != 0x5A) {
                fprintf(stderr, "UART: unexpected byte %02x\n", tag);
                fails++;
                parsed++;
            } else break;
        }
    }
    printf("  %s: %llu clock cycles, core advanced %ld, %ld self-resets\n", argv[1], (unsigned long long)cycles, advanced,
           resets);
    printf("  every flip-flop equal to the model whenever the core advanced\n");
    printf("  UART: %ld CRC messages and %ld debug words equal to the host's; VGA: %ld frames equal to the framebuffer"
           " (%ld skipped while it changed)\n", crc_ok, dbg_ok, frames_ok, frames_skipped);
    if (!crc_ok || !frames_ok) { fprintf(stderr, "too short to check the UART and video\n"); fails++; }
    printf(fails ? "FAIL\n" : "PASS\n");
    delete v;
    return fails != 0;
}
