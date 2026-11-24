`timescale 1ps/1ps
// Time unit: 1 "ps" here is one tick = (1/15.36 MHz)/43680 = 1.4905 ps of real time.
// t_clk 430.08 MHz = 1560 ticks, generator 599.04 MHz = 1120 ticks, readout 307.2 MHz = 2184 ticks.
module tb;
  parameter integer GOFF = 7;    // static phase of generator clock, ticks
  parameter integer ROFF = 13;   // static phase of readout clock, ticks
  parameter integer NWR  = 84;
  parameter integer SPACING = 225; // t_clk cycles between writes; 225 = 1 mod 28
  reg tclk=0, gclk=0, rclk=0;
  initial forever #780 tclk = ~tclk;
  initial begin #(GOFF); forever #560 gclk = ~gclk; end
  initial begin #(ROFF); forever #1092 rclk = ~rclk; end
  reg trstn=0, grstn=0, rrstn=0;
  initial begin #(1560*40) trstn=1; grstn=1; rrstn=1; end

  reg          wv=0;
  reg  [167:0] wd=0;

  // ---------------- generator path ----------------
  wire [167:0] g_cdc_d; wire g_cdc_v;
  cdcsync #(.N(1), .B(168)) g_cdc (
    .s_axis_aresetn(trstn), .s_axis_aclk(tclk), .s0_axis_tvalid(wv), .s0_axis_tdata(wd),
    .m_axis_aresetn(grstn), .m_axis_aclk(gclk), .m0_axis_tready(1'b1), .m0_axis_tvalid(g_cdc_v), .m0_axis_tdata(g_cdc_d));
  wire [159:0] g_tr_d; wire g_tr_v;
  sg_translator #(.OUT_TYPE(0)) g_tr (.aresetn(grstn), .aclk(gclk), .s_axis_tdata(g_cdc_d), .s_axis_tvalid(g_cdc_v),
    .m_gen_v6_axis_tdata(g_tr_d), .m_gen_v6_axis_tvalid(g_tr_v), .m_gen_v6_axis_tready(1'b1));
  // axis_register_slice_4 (modelled as one register stage)
  reg [159:0] g_rs_d=0; reg g_rs_v=0;
  always @(posedge gclk) begin g_rs_d <= g_tr_d; g_rs_v <= g_tr_v; end
  wire g_rd, g_empty; wire [159:0] g_dout;
  fifo_xpm #(.B(160), .N(16)) g_fifo (.rstn(grstn), .clk(gclk), .wr_en(g_rs_v), .din(g_rs_d), .rd_en(g_rd), .dout(g_dout), .full(), .empty(g_empty));
  ctrl_sg_v6 #(.N(16), .N_DDS(16)) g_ctrl (.rstn(grstn), .clk(gclk), .fifo_rd_en_o(g_rd), .fifo_empty_i(g_empty), .fifo_dout_i(g_dout));

  // ---------------- readout path ----------------
  wire [167:0] r_mx_d; wire r_mx_v;
  tmux #(.N(8), .B(168)) r_mux (.aresetn(trstn), .aclk(tclk), .s_axis_tvalid(wv), .s_axis_tdata(wd | (168'd1 << 160)),
    .m1_axis_tvalid(r_mx_v), .m1_axis_tdata(r_mx_d));
  wire [167:0] r_cdc_d; wire r_cdc_v;
  cdcsync #(.N(2), .B(168)) r_cdc (
    .s_axis_aresetn(trstn), .s_axis_aclk(tclk), .s0_axis_tvalid(r_mx_v), .s0_axis_tdata(r_mx_d), .s1_axis_tvalid(1'b0), .s1_axis_tdata(168'd0),
    .m_axis_aresetn(rrstn), .m_axis_aclk(rclk), .m0_axis_tready(1'b1), .m1_axis_tready(1'b1), .m0_axis_tvalid(r_cdc_v), .m0_axis_tdata(r_cdc_d));
  wire [87:0] r_tr_d; wire r_tr_v;
  sg_translator #(.OUT_TYPE(3)) r_tr (.aresetn(rrstn), .aclk(rclk), .s_axis_tdata(r_cdc_d), .s_axis_tvalid(r_cdc_v),
    .m_readout_axis_tdata(r_tr_d), .m_readout_axis_tvalid(r_tr_v), .m_readout_axis_tready(1'b1));
  wire r_rd, r_empty; wire [87:0] r_dout;
  fifo #(.B(88), .N(8)) r_fifo (.rstn(rrstn), .clk(rclk), .wr_en(r_tr_v), .din(r_tr_d), .rd_en(r_rd), .dout(r_dout), .full(), .empty(r_empty));
  ctrl_dyn_ro_v1 #(.N(8)) r_ctrl (.aresetn(rrstn), .aclk(rclk), .fifo_rd_en(r_rd), .fifo_empty(r_empty), .fifo_dout(r_dout));

  // ---------------- measurement ----------------
  time tw, tg, tr; integer k=0; integer gn=0, rn=0;
  always @(posedge gclk) if (g_ctrl.sync_reg && g_ctrl.phrst_int) begin tg = $time; gn = gn+1; end
  always @(posedge rclk) if (r_ctrl.sync_reg && r_ctrl.phrst_int) begin tr = $time; rn = rn+1; end
  integer i;
  initial begin
    repeat (1000) @(posedge tclk);
    for (i=0; i<NWR; i=i+1) begin
      @(posedge tclk); #1;
      wd = 0; wd[156] = 1'b1;            // phrst
      wd[151:120] = 32'd20;              // nsamp
      wd[31:0] = 32'h12345678;           // freq
      wv = 1; tw = $time - 1;
      @(posedge tclk); #1; wv = 0;
      repeat (SPACING-2) @(posedge tclk);
      $display("RES %0d slot %0d  gen %0d  ro %0d  n %0d %0d", i, ((tw/1560) % 28), tg-tw, tr-tw, gn, rn);
    end
    $finish;
  end
endmodule
