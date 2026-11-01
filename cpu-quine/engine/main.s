; CPU-Quine replay engine -- the machine's only program.
;
; It replays the machine's own execution from reset (the "guest") and draws,
; for the replayed cycle, the state of every transistor in the CPU.  The guest
; runs this same program, so the guest's screen shows the guest's guest, and
; so on down.
;
; * ROM is shared: guest fetches and ROM loads read the one physical ROM.
; * RAM nests: guest RAM lives in 256-word pages allocated from this level's
;   RAM on the first non-zero write (page table PTAB, see gwrite).
; * Fast-forward ("replay") steps the guest an instruction at a time with the
;   generated handlers in gen_stepper.s; drawing expands the guest's flip-flop
;   state to every net and transistor (gen_cells.s).
; * Watch mode steps the guest one clock cycle at a time: the expansion that
;   draws a cycle also yields every flip-flop's D input, which is latched.
;
; Build-time parameters (see quine/build.py):
;   WATCH_CYCLES  guest cycles shown one by one per show loop
;   HOLD_OUTER, HOLD_INNER  pacing loop counts for each watched cycle
;   FF_FRAMES     frames drawn per fast-forward phase
;   FRAME_EVERY   draw a frame every FRAME_EVERY * 65536 guest cycles
;   PAGE_LIMIT    first physical page the allocator may not use (128 = all RAM;
;                 smaller values end epochs early, for testing the self-reset)

.include "gen_layout.s"

; ---------------------------------------------------------------------------
; Macros
; ---------------------------------------------------------------------------

; A = guest RAM address -> A = the word at it (8 instructions; clobbers X, ga).
; Unmapped pages have PTAB = $8000, which moves the read above $7FFF where
; RAM reads return zero; guest addresses above $7FFF use PTAB[128..255] = 0.
.macro GREAD
    STA ga
    SWAB
    AND #$FF
    TAX
    LDA ga
    ADD PTAB,X
    TAX
    LDA 0,X
.endm

; Start emulating the guest instruction at gPC.  Y = gPC in every handler.
.macro DISPATCH
    LDY gPC
    LDA rom 0,Y
    STA gIR
    SWAB
    AND #$FF
    TAX
    JMPX DISPATCH_TABLE
.endm

; End of a handler: count the instruction's n cycles, then dispatch.
; ffsync_* marks the points where the compact guest state is complete.
.macro NEXT n
    LDA gCYC0
    ADD #\n
    STA gCYC0
    BCC ffsync_\@
    JMP ff_carry
    .mark ffsync_\@
    DISPATCH
.endm

; ---------------------------------------------------------------------------
; Reset and the show script
; ---------------------------------------------------------------------------

        .org 0
reset:  JMP boot

boot:   LDX #0                  ; guest page table: all pages unmapped
.pt:    LDA #$8000
        STA PTAB,X
        LDA #0
        STA PTAB+128,X          ; guest I/O space reads as zero
        INX
        CPX #128
        BNE .pt
        JSR guest_init
        LDA #FIRST_GUEST_PAGE
        STA nextp
        LDA #0
        STA mode
        STA fcount
        STA frames_left
        STA wcount
        JSR text_static

show:   JSR watch               ; 1. watch a few cycles closely
        LDA #FF_FRAMES          ; 2. replay at full speed, drawing frames
        STA frames_left
        LDA #FRAME_EVERY
        STA fcount
        DISPATCH

; A carry out of the low cycle word (every 65536 guest cycles of real work).
ff_carry:
        LDA gCYC1
        ADD #1
        STA gCYC1
        BCC .c1
        LDA gCYC2
        ADD #1
        STA gCYC2
.c1:    LDA fcount
        SUB #1
        STA fcount
        BEQ .frame
        .mark ffsync_carry
        DISPATCH
.frame: LDA #FRAME_EVERY
        STA fcount
        JSR materialize
        JSR frame
        LDA frames_left
        SUB #1
        STA frames_left
        BEQ .end
        .mark ffsync_frame
        DISPATCH
.end:   JMP show

; Watch mode: draw WATCH_CYCLES consecutive guest cycles, each held on screen,
; stepping the guest one clock cycle at a time through the expansion.
watch:  LDA #0
        STA mode
        LDY gPC                 ; idling in a delay loop?  skip to its end
        LDA rom 0,Y
        CMP #DBNE_SELF
        BNE .go
        STA gIR
        JSR idle_skip
.go:    JSR materialize
        LDA #WATCH_CYCLES
        STA wcount
.lp:    JSR frame
        JSR hold
        JSR latch
.latched:
        LDA wcount
        SUB #1
        STA wcount
        BNE .lp
.fin:   LDA gMU                 ; finish the instruction in progress
        BEQ .done
        JSR expand
        JSR latch
.latched2:
        JMP .fin
.done:  JSR dematerialize
        LDA #1
        STA mode
        RTS

frame:  JSR expand              ; nets + combinational cells of the die map
        JSR draw_dffs           ; flip-flops
        JSR draw_inset          ; the guest's own screen
        JSR text_dynamic        ; status panel
        JSR emit_hash           ; frame hash to the debug port
        RTS

; Pacing: a counted delay loop, itself part of the replayed history.
hold:   LDY #HOLD_OUTER
.o:     LDX #HOLD_INNER
.i:     DBNE .i
        DEY
        BNE .o
        RTS

; ---------------------------------------------------------------------------
; Guest machine state
; ---------------------------------------------------------------------------

guest_init:                     ; the reset state of the guest machine
        LDA #0
        STA gA
        STA gX
        STA gY
        STA gSP
        STA gPC
        STA gIR
        STA gMU
        STA gF
        STA gC
        STA gCYC0
        STA gCYC1
        STA gCYC2
        STA greset
        LDA #1
        STA gRES                ; Z = 0, N = 0
        LDA rom 0               ; RDATA during reset: ROM[PC=0]
        STA gRD
        RTS

; The guest wrote its own reset address: it restarts with all-zero RAM.
; (Pages it held stay allocated but unmapped; never reached in practice.)
guest_reset:
        JSR guest_init
        LDX #0
.l:     LDA #$8000
        STA PTAB,X
        INX
        CPX #128
        BNE .l
        RTS
guest_reset_ff:
        JSR guest_reset
        DISPATCH

; Compact state (lazy flags) -> full flip-flop state at an instruction
; boundary: Z,N from gRES, C from gC, MU = 0, RDATA = ROM[PC].
materialize:
        LDA gRES
        BEQ .z
        BMI .n
        LDA #0
        JMP .c
.z:     LDA #1
        JMP .c
.n:     LDA #2
.c:     STA t0
        LDA gC
        SHL
        SHL
        OR t0
        STA gF
        LDA #0
        STA gMU
        LDX gPC
        LDA rom 0,X
        STA gRD
        RTS

; Full state at an instruction boundary -> compact state.
dematerialize:
        LDA gF
        AND #1
        BEQ .nz
        LDA #0
        JMP .s
.nz:    LDA gF
        AND #2
        BEQ .p
        LDA #$8000
        JMP .s
.p:     LDA #1
.s:     STA gRES
        LDA gF
        SHR
        SHR
        AND #1
        STA gC
        RTS

; The guest sits on "DBNE self": perform all its iterations in one step.
; X = n (0 means 65536) iterations of 4 cycles; X ends at 0.  These cycles
; do not count towards the frame clock, which follows real work.
idle_skip:
        LDA gX
        BEQ .full
        SHL
        SHL
        ADD gCYC0
        STA gCYC0
        LDA #0
        ROL
        STA t0
        LDA gX
        SWAB
        AND #$FF
        SHR
        SHR
        SHR
        SHR
        SHR
        SHR
        ADD t0
        JMP .hi
.full:  LDA #4
.hi:    ADD gCYC1
        STA gCYC1
        BCC .x
        LDA gCYC2
        ADD #1
        STA gCYC2
.x:     LDA #0
        STA gX
        LDA gPC
        ADD #1
        AND #$7FFF
        STA gPC
        RTS

; ---------------------------------------------------------------------------
; Guest RAM writes (reads are the GREAD macro)
; ---------------------------------------------------------------------------

; A = value, ga = guest address.  Keeps Y.  A non-zero write to an unmapped
; page allocates the next free physical page; RAM is cleared at reset, so a
; fresh page already reads as zero.  Out of pages: self-reset (new epoch).
gwrite: STA gv
        LDA ga
        BMI .io
        SWAB
        AND #$FF
        TAX
        LDA PTAB,X
        CMP #$8000
        BEQ .unmapped
.store: ADD ga
        TAX
        LDA gv
        STA 0,X
        RTS
.unmapped:
        LDA gv
        BEQ .ret                ; zero to an unmapped page: already zero
        LDA nextp
        CMP #PAGE_LIMIT
        BCS oom
        ADD #1
        STA nextp
        SUB #1
        SWAB                    ; physical page base
        STA gtmp
        TXA
        SWAB                    ; guest page base
        STA gtmp2
        LDA gtmp
        SUB gtmp2
        STA PTAB,X              ; delta = (p - g) << 8
        JMP .store
.io:    CMP #IO_RESET           ; guest I/O: only its reset address matters
        BNE .ret
        LDA #1
        STA greset
.ret:   RTS

oom:    STA IO_RESET            ; the allocator is exhausted: start a new epoch
.h:     BRA .h

; ---------------------------------------------------------------------------
; Watch-mode clock edge: the memory operation of the drawn cycle, then every
; flip-flop takes its D input.  Must follow expand.
; ---------------------------------------------------------------------------

latch:  LDA W_BUS_ADDR
        STA t2
        LDA W_MISC
        AND #MISC_SPACE
        BEQ .rom
        LDA t2
        GREAD                   ; read-first: the read sees the old word
        STA nrd
        LDA W_MISC
        AND #MISC_WE
        BEQ .edge
        LDA t2
        STA ga
        LDA W_BUS_WDATA
        JSR gwrite
        LDA greset
        BEQ .edge
        JSR guest_reset
        JSR materialize
        RTS
.rom:   LDX t2
        LDA rom 0,X
        STA nrd
.edge:  JSR latch_regs
        LDA nrd
        STA gRD
        LDA gCYC0
        ADD #1
        STA gCYC0
        BCC .r
        LDA gCYC1
        ADD #1
        STA gCYC1
        BCC .r
        LDA gCYC2
        ADD #1
        STA gCYC2
.r:     RTS

; ---------------------------------------------------------------------------
; Generated code and tables
; ---------------------------------------------------------------------------

.include "gen_stepper.s"
.include "gen_cells.s"
.include "gen_text.s"
.include "gen_inset.s"

ROM_END:
.assert ROM_END <= $8000, "ROM overflow"
