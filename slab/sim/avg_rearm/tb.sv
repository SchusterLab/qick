`timescale 1ps/1ps
// The accumulated buffer's capture FSM (axis_avg_buffer/src/avg.sv) when it is
// re-armed for a new acquisition while the readout trigger from the tProc is
// still high (left high by a stopped program, e.g. one whose trigger-clear was
// dropped by a full dispatcher queue).
//   order "old": the library arms the buffer (config_avg + enable) and only then
//                resets the tProc, which clears the trigger output;
//   order "new": the tProc is reset first, then the buffer is armed.
// Each case then fires 5 real triggers and lists the buffer addresses written.
module tb;
  reg clk = 0; always #1627 clk = ~clk;     // ~307 MHz readout clock
  reg rstn = 0;
  reg trig = 0, start = 0;
  reg [9:0] addr_reg = 10'd77;               // left over from the previous acquisition
  wire we; wire [9:0] maddr; wire [63:0] mdi;
  avg #(.N(10), .B(16)) dut (.rstn(rstn), .clk(clk), .trigger_i(trig), .din_valid_i(1'b1),
    .din_i(32'h00010001), .mem_we_o(we), .mem_addr_o(maddr), .mem_di_o(mdi),
    .START_REG(start), .ADDR_REG(addr_reg), .LEN_REG(32'd100), .PHOTON_MODE_REG(1'b0),
    .H_THRSH_REG(16'd0), .L_THRSH_REG(16'd0));
  integer nw = 0;
  reg [8*64-1:0] list;
  always @(posedge clk) if (we) begin
    nw = nw + 1;
    $display("  write %0d at address %0d", nw, maddr);
  end
  task automatic axi_gap; #300000; endtask   // one AXI-Lite write from the PS, ~0.3 us
  task automatic fire(input integer n);
    repeat (n) begin #2000000; trig = 1; #23000; trig = 0; end
  endtask
  task automatic arm;  // QickSoc.config_avg() then enable_buf()
    start = 0; axi_gap; addr_reg = 0; axi_gap; /* length */ axi_gap; start = 1; axi_gap;
  endtask
  initial begin
    #20000 rstn = 1;
    // previous acquisition: armed, captured something, trigger left high at its end
    start = 1; #100000; trig = 1; #2000000;
    $display("ORDER old: arm the buffer, then reset the tProc (trigger cleared)");
    nw = 0; arm; #1000000; trig = 0; fire(5); #1000000;
    $display("RESULT old: %0d writes for 5 triggers", nw);
    // same starting state again
    start = 1; addr_reg = 77; #100000; trig = 1; #2000000;
    $display("ORDER new: reset the tProc (trigger cleared), then arm the buffer");
    nw = 0; trig = 0; #1000000; arm; fire(5); #1000000;
    $display("RESULT new: %0d writes for 5 triggers", nw);
    $finish;
  end
endmodule
