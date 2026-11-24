`timescale 1ps/1ps
// Checks the TIME_SYNC_PERIOD option of qproc_ctrl: with it set to 28, every
// time reset takes effect on the same slot of a free-running 28-cycle count of
// t_clk, however the start request is timed. With it off, the slot is arbitrary.
module tb;
  reg tclk = 0, cclk = 0;
  initial forever #780  tclk = ~tclk;     // 430.08 MHz in the units of ../reset_path
  initial forever #1571 cclk = ~cclk;     // core clock, deliberately not related to t_clk
  reg trstn = 0, crstn = 0;
  initial begin #50000 trstn = 1; crstn = 1; end
  reg [15:0] ctrl = 0;
  integer tbcnt = 0;
  always @(posedge tclk) if (!trstn) tbcnt <= 0; else tbcnt <= (tbcnt == 27) ? 0 : tbcnt + 1;

  genvar g;
  generate for (g = 0; g < 2; g = g + 1) begin : G
    wire time_rst, time_en; wire [47:0] time_abs;
    qproc_ctrl #(.TIME_READ(0), .TIME_SYNC_PERIOD(g ? 28 : 0)) dut (
      .t_clk_i(tclk), .t_rst_ni(trstn), .c_clk_i(cclk), .c_rst_ni(crstn),
      .proc_start_i(1'b0), .proc_stop_i(1'b0), .core_start_i(1'b0), .core_stop_i(1'b0),
      .time_rst_i(1'b0), .time_updt_i(1'b0), .time_updt_dt_i(32'd0),
      .int_time_en(1'b0), .int_time_cmd(4'd0), .int_time_dt(32'd0),
      .PS_TPROC_CTRL(ctrl), .PS_TPROC_CFG(3'd0), .xreg_TPROC_W_DT(32'd0),
      .all_fifo_full_i(1'b0), .some_fifo_full_i(1'b0),
      .time_rst_o(time_rst), .time_en_o(time_en), .time_abs_o(time_abs));
    reg [27:0] seen = 0; integer n = 0;
    // slot on which time is 1 (first count after the reset) and running
    reg [47:0] prev = 0;
    always @(posedge tclk) begin
      prev <= time_abs;
      if (time_en && time_abs == 1 && prev == 0) begin seen[tbcnt] <= 1'b1; n <= n + 1; end
    end
  end endgenerate

  integer i, gap, nslots0, nslots1, k;
  initial begin
    #200000;
    for (i = 0; i < 200; i = i + 1) begin
      gap = 400000 + ($urandom % 100000);
      #(gap);
      @(posedge cclk); #1; ctrl[2] = 1;       // START
      repeat (4) @(posedge cclk); #1; ctrl[2] = 0;
    end
    #400000;
    nslots0 = 0; nslots1 = 0;
    for (k = 0; k < 28; k = k + 1) begin nslots0 = nslots0 + G[0].seen[k]; nslots1 = nslots1 + G[1].seen[k]; end
    $display("RESULT option off: %0d starts, time zero on %0d different slots", G[0].n, nslots0);
    $display("RESULT option on : %0d starts, time zero on %0d different slots", G[1].n, nslots1);
    if (G[1].n == 200 && nslots1 == 1 && G[0].n == 200 && nslots0 > 1) $display("PASS"); else $display("FAIL");
    $finish;
  end
endmodule
