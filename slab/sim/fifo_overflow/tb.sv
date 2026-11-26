`timescale 1ps/1ps
`include "_qproc_defines.svh"
// Dispatcher-overflow testbench for the tProc v2 (qick_processor), configured
// like the stock ZCU216 design: 20 trigger ports, 16 wave ports, one 8-bit data
// port, FIFO_DEPTH 9 (512-entry queues), t_clk 430.08 MHz, c_clk 215.04 MHz
// (= t_clk/2, as in the stock design; +CCLK200 for an unrelated 200 MHz).
//
// The program (prog.mem/wave.mem/dmem.mem, made by make_prog.py) queues N
// readouts per repetition, far ahead of real time. The testbench counts what
// leaves the tProc (rising edges on the readout trigger port and pin port,
// accepted beats on the generator and readout-config wave ports) and every
// push the dispatcher makes into a queue that is full at that clock (lost).
//
// ARITH is 0 here (stock: 1) only because its DSP macro is an IP core with no
// simulation model in the repository; it plays no part in the dispatcher.
//
// Plusargs: +CFG=<hex>  value written to TPROC_CFG before the start
//                       (0x800 sets bit 11: do not pause the core on a full queue)
//           +RUN_NS=<n> simulated time after the start
//           +RESTART=1  run the program a second time (PROC_START again) and
//                       count separately
//           +CCLK200    200 MHz core clock instead of t_clk/2
// Defines:  NO_GUARD    sources without FIFO_GUARD (count the stall from
//                       some_fifo_full); OLD_R21 the rev-21 sources (implies it)
// After each run it reads AXI register 9 and prints the lost-push flags.
// Names of the dispatcher generate blocks (renamed upstream in 2025).
`ifdef OLD_R21
  `define NO_GUARD
  `define QPROC_CTRL_STATE core_st
  `define TGEN TRIG_FIFO
  `define WGEN WAVE_FIFO
  `define DGEN DATA_FIFO
`else
  `define QPROC_CTRL_STATE QPROC_CTRL.core_st
  `define TGEN TRIG_PORT
  `define WGEN WAVE_PORT
  `define DGEN DATA_PORT
`endif
module tb;
  localparam real T_HALF = 1162.6;   // 430.08 MHz
  real C_HALF = 2325.2;              // 215.04 MHz: the stock core clock is t_clk/2 from a clock wizard
  initial if ($test$plusargs("CCLK200")) C_HALF = 2500.0;   // unrelated 200 MHz instead
  localparam real P_HALF = 5000.0;   // 100 MHz
  localparam int NTRIG = 20, NW = 16;
  localparam int RO_TRIG = 10, PIN_TRIG = 0, GEN_W = 0, ROCFG_W = 4;

  reg tclk = 0, cclk = 0, pclk = 0;
  always #(T_HALF) tclk = ~tclk;
  always #(C_HALF) cclk = ~cclk;
  always #(P_HALF) pclk = ~pclk;
  reg rstn = 0;

  TYPE_IF_AXI_REG axi();
  wire        trig   [NTRIG];
  wire        dvalid [1];
  wire [7:0]  ddata  [1];
  wire [167:0] wdata [NW];
  wire        wvalid [NW];
  wire        wready [NW];
  genvar g;
  generate for (g = 0; g < NW; g++) begin : RDY
    assign wready[g] = 1'b1;
  end endgenerate
  wire        in_valid [1];
  wire [63:0] in_data  [1];
  assign in_valid[0] = 1'b0;
  assign in_data[0]  = 64'd0;
  wire [31:0] qdt [2];
  assign qdt[0] = 0; assign qdt[1] = 0;
  wire [47:0] time_abs;

  qick_processor #(
    .DEBUG(0), .DUAL_CORE(0), .LFSR(1), .DIVIDER(1), .ARITH(0), .TIME_READ(1),
    .FIFO_DEPTH(9), .PMEM_AW(12), .DMEM_AW(14), .WMEM_AW(10), .REG_AW(4),
    .IN_PORT_QTY(1), .OUT_TRIG_QTY(NTRIG), .OUT_DPORT_QTY(1), .OUT_DPORT_DW(8),
    .OUT_WPORT_QTY(NW)
  ) DUT (
    .t_clk_i(tclk), .t_rst_ni(rstn), .c_clk_i(cclk), .c_rst_ni(rstn),
    .ps_clk_i(pclk), .ps_rst_ni(rstn),
    .ext_flag_i(1'b0), .proc_start_i(1'b0), .proc_stop_i(1'b0),
    .core_start_i(1'b0), .core_stop_i(1'b0), .time_rst_i(1'b0), .time_init_i(1'b0),
    .time_updt_i(1'b0), .time_updt_dt_i(32'd0), .time_abs_o(time_abs),
    .periph_a_dt_o(), .periph_b_dt_o(), .periph_c_dt_o(), .periph_d_dt_o(), .periph_op_o(),
    .qnet_en_o(), .qnet_rdy_i(1'b1), .qnet_dt_i(qdt), .qnet_vld_i(1'b0), .qnet_flag_i(1'b0),
    .qcom_en_o(), .qcom_rdy_i(1'b1), .qcom_dt_i(qdt), .qcom_vld_i(1'b0), .qcom_flag_i(1'b0),
    .qp1_en_o(), .qp1_rdy_i(1'b1), .qp1_dt_i(qdt), .qp1_vld_i(1'b0), .qp1_flag_i(1'b0),
    .qp2_en_o(), .qp2_rdy_i(1'b1), .qp2_dt_i(qdt), .qp2_vld_i(1'b0),
    .s_dma_axis_tdata_i(256'd0), .s_dma_axis_tlast_i(1'b0), .s_dma_axis_tvalid_i(1'b0),
    .s_dma_axis_tready_o(), .m_dma_axis_tdata_o(), .m_dma_axis_tlast_o(),
    .m_dma_axis_tvalid_o(), .m_dma_axis_tready_i(1'b1),
    .IF_s_axireg(axi),
    .port_tvalid_i(in_valid), .port_tdata_i(in_data),
    .port_trig_o(trig), .port_tvalid_o(dvalid), .port_tdata_o(ddata),
    .m_axis_tdata(wdata), .m_axis_tvalid(wvalid), .m_axis_tready(wready),
    .dport_di(4'd0), .ps_debug_do(), .t_debug_do(), .t_fifo_do(), .c_time_usr_do(),
    .c_debug_do(), .c_time_ref_do(), .c_proc_do(), .c_port_do(), .c_core_do()
  );

  // ---------------- AXI-Lite master ----------------
  initial begin
    axi.axi_awvalid = 0; axi.axi_wvalid = 0; axi.axi_bready = 1;
    axi.axi_arvalid = 0; axi.axi_rready = 1; axi.axi_awprot = 0; axi.axi_arprot = 0;
    axi.axi_wstrb = 4'hf; axi.axi_awaddr = 0; axi.axi_araddr = 0; axi.axi_wdata = 0;
  end
  task automatic axi_write(input [5:0] addr, input [31:0] data);
    @(posedge pclk); #1;
    axi.axi_awaddr = addr; axi.axi_wdata = data; axi.axi_awvalid = 1; axi.axi_wvalid = 1;
    do @(posedge pclk); while (!(axi.axi_awready && axi.axi_wready));
    #1; axi.axi_awvalid = 0; axi.axi_wvalid = 0;
    do @(posedge pclk); while (!axi.axi_bvalid);
  endtask
  task automatic axi_read(input [5:0] addr, output [31:0] data);
    @(posedge pclk); #1;
    axi.axi_araddr = addr; axi.axi_arvalid = 1;
    do @(posedge pclk); while (!axi.axi_arready);
    #1; axi.axi_arvalid = 0;
    while (!axi.axi_rvalid) @(posedge pclk);
    data = axi.axi_rdata;
  endtask

  // ---------------- counters ----------------
  integer run = 0;                       // 0: first run, 1: second run
  integer ro_edges[2], pin_edges[2], gen_beats[2], rocfg_beats[2];
  integer lost_t[2], lost_w[2], lost_d[2], pushes_t[2], pushes_w[2];
  integer stall_cycles[2], full_cycles[2], ro_high_max[2], ro_high;
  initial for (int r = 0; r < 2; r++) begin
    full_cycles[r] = 0; ro_high_max[r] = 0;
    ro_edges[r] = 0; pin_edges[r] = 0; gen_beats[r] = 0; rocfg_beats[r] = 0;
    lost_t[r] = 0; lost_w[r] = 0; lost_d[r] = 0; pushes_t[r] = 0; pushes_w[r] = 0; stall_cycles[r] = 0;
  end
  // The program plays one waveform per wave port: every beat on a port must carry
  // the same word as the first one (a different word = wrong waveform dispatched).
  reg [167:0] gen_word, rocfg_word;
  reg gen_seen = 0, rocfg_seen = 0;
  integer gen_bad[2], rocfg_bad[2];
  initial for (int r = 0; r < 2; r++) begin gen_bad[r] = 0; rocfg_bad[r] = 0; end
  reg ro_q = 0, pin_q = 0;
  integer fd;
  always @(posedge tclk) begin
    ro_q <= trig[RO_TRIG]; pin_q <= trig[PIN_TRIG];
    if (trig[RO_TRIG] && !ro_q) begin ro_edges[run]++; $fdisplay(fd, "RO %0d %0d", run, time_abs); end
    // longest readout-trigger pulse, in t_clk cycles (the program asks for 10)
    if (trig[RO_TRIG]) begin ro_high = ro_q ? ro_high + 1 : 1; if (ro_high > ro_high_max[run]) ro_high_max[run] = ro_high; end
    if (trig[PIN_TRIG] && !pin_q) pin_edges[run]++;
    if (wvalid[GEN_W] && wready[GEN_W]) begin
      gen_beats[run]++;
      if (!gen_seen) begin gen_seen = 1; gen_word = wdata[GEN_W]; end
      else if (wdata[GEN_W] !== gen_word) gen_bad[run]++;
    end
    if (wvalid[ROCFG_W] && wready[ROCFG_W]) begin
      rocfg_beats[run]++;
      if (!rocfg_seen) begin rocfg_seen = 1; rocfg_word = wdata[ROCFG_W]; end
      else if (wdata[ROCFG_W] !== rocfg_word) rocfg_bad[run]++;
    end
  end
  // pushes into full queues (the XPM FIFO ignores them)
  wire [NTRIG-1:0] t_push, t_full;
  wire [NW-1:0]    w_push, w_full;
  generate
    for (g = 0; g < NTRIG; g++) begin : TM
      assign t_push[g] = DUT.DISPATCHER.`TGEN[g].trig_fifo_inst.push_i;
      assign t_full[g] = DUT.DISPATCHER.`TGEN[g].trig_fifo_inst.async_full_o;
    end
    for (g = 0; g < NW; g++) begin : WM
      assign w_push[g] = DUT.DISPATCHER.`WGEN[g].wave_fifo_inst.push_i;
      assign w_full[g] = DUT.DISPATCHER.`WGEN[g].wave_fifo_inst.async_full_o;
    end
  endgenerate
  wire d_push = DUT.DISPATCHER.`DGEN[0].data_fifo_inst.push_i;
  wire d_full = DUT.DISPATCHER.`DGEN[0].data_fifo_inst.async_full_o;
  integer first_loss_reported = 0;
  always @(posedge cclk) if (rstn) begin
    pushes_t[run] += $countones(t_push);
    pushes_w[run] += $countones(w_push);
    lost_t[run] += $countones(t_push & t_full);
    lost_w[run] += $countones(w_push & w_full);
    lost_d[run] += (d_push & d_full);
    if (DUT.`QPROC_CTRL_STATE == 5) begin              // core running (C_RUN)
      if (DUT.some_fifo_full) full_cycles[run]++;      // a queue truly full
`ifndef NO_GUARD
      if (DUT.fifo_stall)     stall_cycles[run]++;     // core paused by the queues
`else
      if (DUT.some_fifo_full) stall_cycles[run]++;
`endif
    end
    if (!first_loss_reported && ((t_push & t_full) || (w_push & w_full))) begin
      first_loss_reported = 1;
      $display("FIRST LOSS at %0t: t_push=%b t_full=%b w_push=%b w_full=%b core_en=%b some_fifo_full=%b",
               $time, t_push, t_full, w_push, w_full, DUT.core_en, DUT.some_fifo_full);
    end
  end

  // ---------------- stimulus ----------------
  integer cfg, run_ns, restart;
  reg [31:0] rd;
  initial begin
    fd = $fopen("edges.txt", "w");
    if (!$value$plusargs("CFG=%h", cfg)) cfg = 0;
    if (!$value$plusargs("RUN_NS=%d", run_ns)) run_ns = 400000;
    if (!$value$plusargs("RESTART=%d", restart)) restart = 0;
    $readmemh("prog.mem", DUT.CORE_0.CORE_MEM.P_MEM.RAM);
    $readmemh("wave.mem", DUT.CORE_0.CORE_MEM.W_MEM.RAM);
    $readmemh("dmem.mem", DUT.CORE_0.CORE_MEM.D_MEM.RAM);
    #200000; rstn = 1;
    #2000000;                                   // XPM FIFOs leave reset
    axi_write(6'd4, cfg);                       // TPROC_CFG
    #100000;
    axi_write(6'd0, 32'd4);                     // PROC_START
    #(run_ns * 1000.0);
    report(0);
    axi_read(6'd36, rd);
    $display("RESULT run=0 REG9=%08h lost-push flags {wave,data,trig}=%03b", rd, rd[10:8]);
    if (restart) begin
      axi_write(6'd0, 32'd8);                   // PROC_STOP
      #200000;
      run = 1;
      axi_write(6'd0, 32'd4);                   // PROC_START
      // the flags of the first run must be cleared by the start
      axi_read(6'd36, rd);
      $display("RESULT run=1 after start REG9=%08h", rd);
      #(run_ns * 1000.0);
      report(1);
      axi_read(6'd36, rd);
      $display("RESULT run=1 REG9=%08h lost-push flags {wave,data,trig}=%03b", rd, rd[10:8]);
    end
    $fclose(fd);
    $finish;
  end

  task report(input integer r);
    $display("RESULT run=%0d cfg=%0h ro_trig_edges=%0d pin_edges=%0d gen_pulses=%0d rocfg_beats=%0d trig_pushes=%0d wave_pushes=%0d lost_trig_pushes=%0d lost_wave_pushes=%0d lost_data_pushes=%0d full_cycles=%0d stall_cycles=%0d wrong_gen_words=%0d wrong_rocfg_words=%0d ro_trig_max_width=%0d trig_level_ro=%0d",
             r, cfg, ro_edges[r], pin_edges[r], gen_beats[r], rocfg_beats[r], pushes_t[r], pushes_w[r],
             lost_t[r], lost_w[r], lost_d[r], full_cycles[r], stall_cycles[r], gen_bad[r], rocfg_bad[r], ro_high_max[r], trig[RO_TRIG]);
  endtask
endmodule
