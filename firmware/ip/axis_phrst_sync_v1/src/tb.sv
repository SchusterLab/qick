`timescale 1ns/1ps
// Self-checking testbench for axis_phrst_sync_v1.
// Checks: reset words leave only when the period counter reads zero; other words
// pass with OUT_REG cycles of latency when nothing is held; order is preserved.
module tb;
  localparam B = 168, PERIOD = 28, PB = 156;
  reg clk = 0; always #1 clk = ~clk;
  reg rstn = 0;
  reg sv = 0; reg [B-1:0] sd = 0;
  integer errors = 0;
  genvar g;
  generate for (g = 0; g < 2; g = g + 1) begin : G
    wire mv; wire [B-1:0] md; wire sync;
    axis_phrst_sync_v1 #(.B(B), .PERIOD(PERIOD), .PHRST_BIT(PB), .OUT_REG(g)) dut
      (.aclk(clk), .aresetn(rstn), .s_axis_tvalid(sv), .s_axis_tdata(sd), .m_axis_tready(1'b1),
       .m_axis_tvalid(mv), .m_axis_tdata(md), .sync_o(sync));
    // The output slot of a released reset word: counter value when it left the queue stage.
    reg [7:0] cnt_d; always @(posedge clk) cnt_d <= dut.cnt;
    wire [7:0] out_slot = g ? cnt_d : dut.cnt;
    integer n_out = 0; reg [31:0] last = 0;
    always @(posedge clk) if (rstn && mv) begin
      n_out <= n_out + 1;
      if (md[PB] && out_slot != 0) begin errors = errors + 1; $display("FAIL reg=%0d: reset word released at slot %0d", g, out_slot); end
      if (md[31:0] != last + 1)    begin errors = errors + 1; $display("FAIL reg=%0d: order, got %0d after %0d", g, md[31:0], last); end
      last <= md[31:0];
    end
  end endgenerate

  integer seq = 0;
  task send(input rst);
    begin
      seq = seq + 1;
      sd = 0; sd[31:0] = seq; sd[PB] = rst; sv = 1;
      @(posedge clk); #0.1; sv = 0;
    end
  endtask
  // Latency check for a word that is not held.
  integer t_in, lat0, lat1;
  integer i, k;
  initial begin
    repeat (5) @(posedge clk); #0.1; rstn = 1;
    repeat (40) @(posedge clk); #0.1;
    // 1. plain words, spaced out: latency must be OUT_REG cycles
    for (i = 0; i < 5; i = i + 1) begin
      fork
        send(0);
        begin #0.2; if (!G[0].mv) begin errors = errors + 1; $display("FAIL: comb pass-through not immediate"); end end
      join
      #0.2; if (!G[1].mv) begin errors = errors + 1; $display("FAIL: registered output not one cycle later"); end
      repeat (7) @(posedge clk); #0.1;
    end
    // 2. reset words on every slot, each followed closely by plain words
    for (k = 0; k < 60; k = k + 1) begin
      send(1);
      repeat (k % 3) @(posedge clk); #0.1;
      send(0);
      send(0);
      repeat (61) @(posedge clk); #0.1;   // 64 cycles per iteration: slot advances by 8 mod 28... plus k%3
    end
    // 3. two reset words back to back
    send(1); send(1); send(0);
    repeat (100) @(posedge clk);
    if (G[0].n_out != seq || G[1].n_out != seq) begin errors = errors + 1; $display("FAIL: sent %0d words, got %0d and %0d", seq, G[0].n_out, G[1].n_out); end
    if (errors == 0) $display("PASS: %0d words through both variants", seq); else $display("FAILED with %0d errors", errors);
    $finish;
  end
endmodule
