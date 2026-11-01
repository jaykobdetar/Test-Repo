"""Assign every net a bit in the net-state bitset.

The layout makes the hot paths of the engine cheap: each register's Q nets
fill one word in bit order (copy-in is one load/store per register), each
register's D nets fill another word where possible (latching is one
load/store), and so do RDATA, the address bus and the write-data bus.
Everything else is packed after those words.
"""

from __future__ import annotations

from . import arch, netlist


def assign(nl: netlist.Netlist) -> dict:
    regs = [r for r, _ in arch.REGISTERS]
    index: dict = {}
    words: dict[str, int] = {}
    nextw = 0

    def word(name):
        nonlocal nextw
        words[name] = nextw
        nextw += 1
        return words[name]

    def place(net, pos):
        if net in index:
            return False
        index[net] = pos
        return True

    # Q words
    bits = {r: {} for r in regs}
    for q, (r, b) in nl.ff_reg.items():
        bits[r][b] = q
    for r in regs:
        w = word("Q_" + r)
        for b, q in bits[r].items():
            assert place(q, 16 * w + b)
    w = word("RDATA")
    for i, n in enumerate(nl.inputs["rdata"]):
        assert place(n, 16 * w + i)
    # D words: only gate-output nets that are not yet placed and not shared
    dnet = {}
    for d in nl.dffs:
        dnet.setdefault(d.ins["D"], []).append(nl.ff_reg[d.out])
    gate_out = {g.out for g in nl.gates}
    d_aligned = {r: set() for r in regs}
    for r in regs:
        w = word("D_" + r)
        for d in nl.dffs:
            rr, b = nl.ff_reg[d.out]
            n = d.ins["D"]
            if rr == r and n in gate_out and len(dnet[n]) == 1 and place(n, 16 * w + b):
                d_aligned[r].add(b)
    bus_aligned = {}
    for port in ("addr", "wdata"):
        w = word("BUS_" + port.upper())
        ok = set()
        for i, n in enumerate(nl.outputs[port]):
            if n in gate_out and place(n, 16 * w + i):
                ok.add(i)
        bus_aligned[port] = ok
    # misc word: constants, we, space
    w = word("MISC")
    place(netlist.CONST0, 16 * w + 0)
    place(netlist.CONST1, 16 * w + 1)
    place(nl.clk, 16 * w + 2)
    place(nl.rst_n, 16 * w + 3)
    place(nl.outputs["space"][0], 16 * w + 4)
    place(nl.outputs["we"][0], 16 * w + 5)
    pos = 16 * nextw
    for g in nl.gates:
        if place(g.out, pos):
            pos += 1
    return {
        "index": index,
        "words": words,
        "nbits": pos,
        "nwords": (pos + 15) // 16,
        "d_aligned": d_aligned,
        "bus_aligned": bus_aligned,
    }
