"""Budget model, driven by measurements of the running machine.

Input: a frame log from `emu ROM log FILE N` -- one line per drawn frame:
    total_cycles  cycle_since_release  epoch  guest_cycle_drawn  mode  pages

Because every level runs the same program, the function
    f(T) = the guest cycle shown by the last frame completed by own cycle T
is the same at every level.  Level 1 is on screen from the first frame (A1);
level k+1 is on screen from the first own cycle T with f(T) >= A_k.  So
    A_{k+1} = min { T : f(T) >= A_k }
is exact wherever the log covers it; beyond the log, f is extended with its
measured long-run slope 1/R_avg.

The epoch ends when the level-0 allocator runs out: level k (k >= 1) holds the
own pages it has written so far, P(c_k), where c_k is its current cycle,
c_1 = f(T), c_{k+1} = f(c_k), and P is again the same function at every level.

usage: python3 -m quine.budget FRAMELOG ROM_SUMMARY [PAGE_SCHEDULE_JSON]
"""

from __future__ import annotations

import bisect
import json
import sys
from pathlib import Path

HZ = 50e6
RAM_PAGES = 128


def load(path):
    rows = []
    for line in Path(path).read_text().split("\n"):
        if line.strip():
            t, c, e, g, mode, pages = map(int, line.split())
            if e == 0:
                rows.append((c, g, mode, pages))
    return rows


def fmt_time(cycles: float) -> str:
    s = cycles / HZ
    if s < 120:
        return f"{s:.1f} s"
    if s < 7200:
        return f"{s / 60:.1f} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h"
    if s < 90 * 86400:
        return f"{s / 86400:.1f} days"
    return f"{s / 86400 / 30.44:.1f} months"


class Model:
    def __init__(self, rows, params):
        self.T = [r[0] for r in rows]  # frame completion, own cycles since release
        self.G = [r[1] for r in rows]  # guest cycle drawn
        self.mode = [r[2] for r in rows]
        self.pages_l0 = [r[3] for r in rows]
        self.params = params
        # long-run slope from whole show loops: use the last 60% of the log
        i0 = len(self.T) * 2 // 5
        self.r_avg = (self.T[-1] - self.T[i0]) / max(1, self.G[-1] - self.G[i0])

    def f(self, T):
        """guest cycle on screen at own cycle T (0 before the first frame)"""
        if T < self.T[0]:
            return 0
        if T <= self.T[-1]:
            i = bisect.bisect_right(self.T, T) - 1
            return self.G[i]
        return self.G[-1] + (T - self.T[-1]) / self.r_avg

    def f_inv(self, x):
        """first own cycle at which the screen shows guest cycle >= x"""
        if x <= 0:
            return self.T[0]
        i = bisect.bisect_left(self.G, x)
        # G is non-decreasing within an epoch
        if i < len(self.G):
            return self.T[i]
        return self.T[-1] + (x - self.G[-1]) * self.r_avg

    def arrivals(self, n):
        a = [self.T[0]]
        for _ in range(n - 1):
            a.append(self.f_inv(a[-1]))
        return a, [x > self.T[-1] for x in a]

    def pages_held(self, T, schedule, depth=40):
        """guest pages held by level 0 at own cycle T: sum over levels k >= 1 of
        P(c_k), with c_1 = f(T), c_{k+1} = f(c_k) and P the own-page schedule"""
        total, c = 0, T
        for _ in range(depth):
            c = self.f(c)
            n = bisect.bisect_right(schedule, c)
            if n == 0:
                break
            total += n
        return total

    def epoch_end(self, schedule, guest_pages):
        lo, hi = self.T[0], self.T[0]
        while self.pages_held(hi, schedule) <= guest_pages:
            lo, hi = hi, hi * 2
            if hi > 1e20:
                return None
        while hi - lo > max(1.0, lo * 1e-6):
            mid = (lo + hi) / 2
            if self.pages_held(mid, schedule) <= guest_pages:
                lo = mid
            else:
                hi = mid
        return hi


def main(argv):
    rows = load(argv[1])
    summary = json.loads(Path(argv[2]).read_text()) if len(argv) > 2 else {}
    params = summary.get("params", {})
    m = Model(rows, params)
    every = params.get("FRAME_EVERY", 3)
    hold = params.get("HOLD_OUTER", 47) * (params.get("HOLD_INNER", 0) or 65536) * 4
    # frame cost and replay cost
    watch = [m.T[i + 1] - m.T[i] for i in range(len(m.T) - 1) if m.mode[i] == 0 and m.mode[i + 1] == 0]
    frame_cost = sorted(watch)[len(watch) // 2] - hold if watch else 0
    ff = [m.T[i + 1] - m.T[i] for i in range(len(m.T) - 1) if m.mode[i] == 1 and m.mode[i + 1] == 1]
    ff_int = sorted(ff)[len(ff) // 2]
    r_ff = (ff_int - frame_cost) / (every * 65536)
    # loop structure: watch -> replay transitions
    starts = [m.T[i] for i in range(1, len(m.T)) if m.mode[i] == 0 and m.mode[i - 1] == 1]
    period = (starts[-1] - starts[0]) / (len(starts) - 1) if len(starts) > 1 else 0
    watch_len = 64 * (sorted(watch)[len(watch) // 2]) if watch else 0
    a, extrap = m.arrivals(7)
    schedule = json.loads(Path(argv[3]).read_text()) if len(argv) > 3 else []
    working = len(schedule) or 21
    total_pages = RAM_PAGES - 22
    print("Measured on the host emulator (cycle-exact model of the 50 MHz machine)")
    print(f"  log covers {fmt_time(m.T[-1])} of machine time, {len(m.T)} frames")
    print(f"  drawn frame: {frame_cost:,.0f} cycles ({frame_cost / HZ * 1e3:.1f} ms)")
    print(
        f"  replay frame interval: {ff_int:,.0f} cycles ({HZ / ff_int:.1f} frames/s), drawing {frame_cost / ff_int:.1%} of replay time"
    )
    print(f"  replay cost R_ff: {r_ff:.1f} cycles per replayed guest cycle of real work")
    print(f"  show loop: watch {watch_len / HZ:.1f} s + replay {(period - watch_len) / HZ:.1f} s = {period / HZ:.1f} s")
    print(f"  long-run average R_avg (own cycles per guest cycle): {m.r_avg:.1f}")
    print("Nesting timeline (level k visible on the physical screen):")
    for k, (x, e) in enumerate(zip(a, extrap), 1):
        print(f"  level {k}: {fmt_time(x):>12s}{'  (extrapolated)' if e else ''}")
    full_levels = total_pages // working
    print(f"  RAM: {working} own pages per level, {total_pages} guest pages -> {full_levels} resident levels")
    end = m.epoch_end(schedule, total_pages) if schedule else None
    if end:
        print(f"  epoch ends (allocator exhausted, self-reset) after {fmt_time(end)} (extrapolated)")
    out = {
        "frame_cycles": frame_cost,
        "replay_frame_interval": ff_int,
        "r_ff": r_ff,
        "r_avg": m.r_avg,
        "loop_seconds": period / HZ,
        "arrival_cycles": a,
        "extrapolated": extrap,
        "own_pages": working,
        "epoch_end_cycles": end,
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv)
