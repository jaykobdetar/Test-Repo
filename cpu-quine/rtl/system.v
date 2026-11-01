// CPU-Quine system top level for an FPGA (design phase 3).
//
//   core      the generated CPU core (build/core.v), the only modelled part
//   memory    32K-word ROM and 32K-word RAM, synchronous, read-first;
//             RDATA is the word addressed in the previous cycle
//   reset     power-up and a write to $FFFF both clear the RAM for 32,768
//             cycles with the core held in reset; reset is released at a
//             falling clock edge, so cycle 0 is the next rising edge
//   observer  folds the core's flip-flops into a running CRC-32 every core
//             cycle; every 2^20 cycles it queues the CRC for the UART
//   debug     writes to $FFFE are queued for the UART
//   uart      8N1: 0xA5 + CRC (4 bytes, LSB first), 0x5A + debug word (2 bytes)
//   video     640x480 at 60 Hz from a 25 MHz pixel clock, every framebuffer
//             pixel doubled; reads the RAM's second port only
//
// Nothing outside the core feeds back into it except RDATA and reset, both of
// which the model knows (see csrc/qmachine.h for the shared memory semantics).
`default_nettype none
module system #(
    parameter ROM_HEX = "rom.hex",
    parameter UART_DIV = 434            // 50 MHz / 115200
) (
    input  wire clk,                    // core clock, 50 MHz
    input  wire pix_clk,                // pixel clock, 25 MHz
    input  wire por_n,                  // power-on reset, active low, asynchronous
    output reg  vga_hsync,
    output reg  vga_vsync,
    output reg  vga_pixel,              // drive all colour channels from this
    output wire uart_tx
);
    // ------------------------------------------------------------------ core
    wire [15:0] addr, wdata, rdata;
    wire space, we;
    wire [91:0] state;
    wire core_rst_n;
    core u_core (
        .clk(clk), .rst_n(core_rst_n), .rdata(rdata),
        .addr(addr), .space(space), .we(we), .wdata(wdata), .state(state)
    );

    // ----------------------------------------------------------------- reset
    reg running;                        // core out of reset (posedge domain)
    reg [14:0] clr;                     // RAM-clear address
    reg rst_n_q;                        // release at a falling edge
    wire reset_req = running & we & space & (addr == 16'hFFFF);
    always @(posedge clk or negedge por_n) begin
        if (!por_n) begin
            running <= 1'b0;
            clr <= 15'd0;
        end else if (running) begin
            if (reset_req) begin
                running <= 1'b0;
                clr <= 15'd0;
            end
        end else begin
            clr <= clr + 15'd1;
            if (clr == 15'h7FFF) running <= 1'b1;
        end
    end
    always @(negedge clk or negedge por_n)
        if (!por_n) rst_n_q <= 1'b0;
        else rst_n_q <= running;
    // asserts as soon as running drops (just after the write's edge), releases
    // half a cycle after running rises
    assign core_rst_n = rst_n_q & running;

    // ---------------------------------------------------------------- memory
    reg [15:0] rom [0:32767];
`ifdef SIMULATION
    reg [8*512-1:0] rom_path;
    initial begin
        if (!$value$plusargs("rom=%s", rom_path)) rom_path = ROM_HEX;
        $readmemh(rom_path, rom);
    end
`else
    initial $readmemh(ROM_HEX, rom);
`endif
    reg [15:0] rom_q;
    always @(posedge clk) rom_q <= rom[addr[14:0]];

    reg [15:0] ram [0:32767];
    wire        a_we   = running ? (we & space & ~addr[15]) : 1'b1;
    wire [14:0] a_addr = running ? addr[14:0] : clr;
    wire [15:0] a_din  = running ? wdata : 16'd0;
    reg  [15:0] ram_q;
    always @(posedge clk) begin
        ram_q <= ram[a_addr];           // read-first
        if (a_we) ram[a_addr] <= a_din;
    end

    reg sel_ram, sel_io;
    always @(posedge clk) begin
        sel_ram <= space;
        sel_io <= addr[15];
    end
    assign rdata = !sel_ram ? rom_q : (sel_io ? 16'd0 : ram_q);

    // -------------------------------------------------------------- observer
    // CRC-32 (reflected, polynomial 0xEDB88320) over a 96-bit packing of the
    // flip-flops, least significant bit first (build/crc96.v, generated):
    //   {3'b0, F, MU, SP, 1'b0, PC, IR, Y, X, A}
    // state = {F[91:89], MU[88:87], IR[86:71], PC[70:56], SP[55:48], Y, X, A}
    wire [95:0] fold = {3'b000, state[91:89], state[88:87], state[55:48], 1'b0,
                        state[70:56], state[86:71], state[47:0]};
    // one register stage in front of the CRC keeps it off the core's timing
    reg [95:0] fold_q;
    reg        fold_v;
    always @(posedge clk or negedge por_n) begin
        if (!por_n) begin
            fold_q <= 96'd0;
            fold_v <= 1'b0;
        end else begin
            fold_q <= fold;
            fold_v <= core_rst_n;       // this edge advances the core
        end
    end
    reg [31:0] crc;
    wire [31:0] crc_next;
    crc96 u_crc (.c(crc), .v(fold_q), .y(crc_next));  // build/crc96.v
    reg [19:0] crc_count;
    reg        crc_pending;
    reg [31:0] crc_hold;
    wire       crc_take;
    always @(posedge clk or negedge por_n) begin
        if (!por_n) begin
            crc <= 32'hFFFFFFFF;
            crc_count <= 20'd0;
            crc_pending <= 1'b0;
            crc_hold <= 32'd0;
        end else begin
            if (fold_v) begin
                crc <= crc_next;
                crc_count <= crc_count + 20'd1;
                if (crc_count == 20'hFFFFF) begin
                    crc_hold <= crc_next;
                    crc_pending <= 1'b1;
                end
            end
            if (crc_take) crc_pending <= 1'b0;
        end
    end

    // ------------------------------------------------------------ debug FIFO
    reg [15:0] dbg_fifo [0:15];
    reg [3:0]  dbg_wr, dbg_rd;
    wire       dbg_push = running & we & space & (addr == 16'hFFFE);
    wire       dbg_empty = dbg_wr == dbg_rd;
    wire       dbg_take;
    always @(posedge clk or negedge por_n) begin
        if (!por_n) begin
            dbg_wr <= 4'd0;
            dbg_rd <= 4'd0;
        end else begin
            if (dbg_push) begin
                dbg_fifo[dbg_wr] <= wdata;
                dbg_wr <= dbg_wr + 4'd1;
            end
            if (dbg_take) dbg_rd <= dbg_rd + 4'd1;
        end
    end

    // ------------------------------------------------------------------ UART
    reg [39:0] msg;                     // bytes to send, low byte first
    reg [2:0]  msg_len;
    reg [9:0]  shift;                   // start bit, 8 data bits, stop bit
    reg [3:0]  bits;
    reg [15:0] baud;
    reg        tx;
    assign uart_tx = tx;
    wire idle = (bits == 4'd0) && (msg_len == 3'd0);
    assign crc_take = idle && crc_pending;
    assign dbg_take = idle && !crc_pending && !dbg_empty;
    always @(posedge clk or negedge por_n) begin
        if (!por_n) begin
            msg_len <= 3'd0;
            bits <= 4'd0;
            baud <= 16'd0;
            tx <= 1'b1;
        end else if (crc_take) begin
            msg <= {crc_hold, 8'hA5};
            msg_len <= 3'd5;
        end else if (dbg_take) begin
            msg <= {16'd0, dbg_fifo[dbg_rd], 8'h5A};
            msg_len <= 3'd3;
        end else if (bits == 4'd0) begin
            if (msg_len != 3'd0) begin
                shift <= {1'b1, msg[7:0], 1'b0};
                msg <= msg >> 8;
                msg_len <= msg_len - 3'd1;
                bits <= 4'd10;
                baud <= 16'd0;
            end
        end else if (baud == UART_DIV - 1) begin
            baud <= 16'd0;
            tx <= shift[0];
            shift <= shift >> 1;
            bits <= bits - 4'd1;
        end else begin
            if (baud == 16'd0) tx <= shift[0];
            baud <= baud + 16'd1;
        end
    end

    // ----------------------------------------------------------------- video
    // 800 x 525 at 25 MHz: 640x480 visible; the 320x240 framebuffer at $0300,
    // 20 words per row, bit 15 leftmost, every pixel shown as 2x2.
    reg [9:0] hx = 10'd0, vy = 10'd0;
    always @(posedge pix_clk) begin
        if (hx == 10'd799) begin
            hx <= 10'd0;
            vy <= (vy == 10'd524) ? 10'd0 : vy + 10'd1;
        end else hx <= hx + 10'd1;
    end
    reg [15:0] vaddr, vword;
    reg [9:0]  hx1, hx2;
    reg        vis1, hs1, vs1, vis2, hs2, vs2;
    always @(posedge pix_clk) begin
        // stage 1: the address of the word holding this pixel
        vaddr <= 16'h0300 + {6'd0, vy[9:1]} * 16'd20 + {11'd0, hx[9:5]};
        hx1 <= hx;
        vis1 <= (hx < 10'd640) && (vy < 10'd480);
        hs1 <= !((hx >= 10'd656) && (hx < 10'd752));
        vs1 <= !((vy >= 10'd490) && (vy < 10'd492));
        // stage 2: read the RAM's second port
        vword <= ram[vaddr[14:0]];
        hx2 <= hx1;
        vis2 <= vis1;
        hs2 <= hs1;
        vs2 <= vs1;
        // stage 3: the pixel
        vga_pixel <= vis2 & vword[4'd15 - hx2[4:1]];
        vga_hsync <= hs2;
        vga_vsync <= vs2;
    end
endmodule
`default_nettype wire
