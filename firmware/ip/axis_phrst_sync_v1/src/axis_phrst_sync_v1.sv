// axis_phrst_sync_v1
//
// Makes phase resets land on a repeatable clock-domain-crossing offset.
//
// Sits in the tProc timing-clock domain between a tProc waveform port and
// whatever carries the word to a generator or readout (axis_cdcsync_v1,
// axis_tmux_v1). Words whose phase-reset bit is clear pass straight through.
// A word whose phase-reset bit is set is held until a free-running counter of
// PERIOD cycles reads zero, then released.
//
// PERIOD is the number of aclk cycles after which the relative phases of
// aclk and the destination clocks repeat. For the stock ZCU216 clocks
// (430.08, 599.04 and 307.2 MHz, all multiples of 15.36 MHz) it is 28.
// Because every reset then crosses on the same point of that pattern, the
// destination sample counters are always zeroed at the same offset.
//
// Order is preserved: words that arrive while a reset word is held wait
// behind it and are released one per cycle afterwards.
//
// All instances on the same clock and reset count in step, so resets sent on
// the same cycle to a generator and a readout are released on the same cycle.
//
// Latency for words that are not held: OUT_REG cycles (0 or 1).
module axis_phrst_sync_v1
   #(
      parameter B          = 168 , // word width
      parameter PERIOD     = 28  , // aclk cycles in one common period of the clocks
      parameter PHRST_BIT  = 156 , // position of the phase-reset bit (conf[4] of the tProc word)
      parameter DEPTH_LOG2 = 4   , // queue depth = 2**DEPTH_LOG2 words
      parameter OUT_REG    = 1     // 1: registered output, 0: combinational pass-through
   )
   (
      input  wire         aclk          ,
      input  wire         aresetn       ,
      output wire         s_axis_tready ,
      input  wire         s_axis_tvalid ,
      input  wire [B-1:0] s_axis_tdata  ,
      input  wire         m_axis_tready , // not used: the downstream blocks never stall
      output wire         m_axis_tvalid ,
      output wire [B-1:0] m_axis_tdata  ,
      output wire         sync_o          // high for one cycle at the start of each period
   );

localparam CW = (PERIOD > 1) ? $clog2(PERIOD) : 1;

// Free-running period counter.
reg [CW-1:0] cnt;
wire         slot0 = (cnt == 0);
always @(posedge aclk) begin
   if (~aresetn)             cnt <= 0;
   else if (cnt == PERIOD-1) cnt <= 0;
   else                      cnt <= cnt + 1;
end
assign sync_o = slot0;

// Queue for held words.
reg  [B-1:0]          mem [0:(1<<DEPTH_LOG2)-1];
reg  [DEPTH_LOG2:0]   wp, rp;
wire                  empty = (wp == rp);
wire                  full  = (wp[DEPTH_LOG2] != rp[DEPTH_LOG2]) && (wp[DEPTH_LOG2-1:0] == rp[DEPTH_LOG2-1:0]);
wire [B-1:0]          head  = mem[rp[DEPTH_LOG2-1:0]];

wire in_rst  = s_axis_tdata[PHRST_BIT];
wire bypass  = s_axis_tvalid & empty & (~in_rst | slot0);
wire push    = s_axis_tvalid & ~bypass & ~full;
wire pop     = ~empty & (~head[PHRST_BIT] | slot0);

always @(posedge aclk) begin
   if (~aresetn) begin
      wp <= 0;
      rp <= 0;
   end
   else begin
      if (push) begin
         mem[wp[DEPTH_LOG2-1:0]] <= s_axis_tdata;
         wp <= wp + 1;
      end
      if (pop) rp <= rp + 1;
   end
end

wire         o_valid = bypass | pop;
wire [B-1:0] o_data  = pop ? head : s_axis_tdata;

generate
   if (OUT_REG) begin : GEN_reg
      reg         valid_r;
      reg [B-1:0] data_r;
      always @(posedge aclk) begin
         if (~aresetn) begin
            valid_r <= 0;
            data_r  <= 0;
         end
         else begin
            valid_r <= o_valid;
            data_r  <= o_data;
         end
      end
      assign m_axis_tvalid = valid_r;
      assign m_axis_tdata  = data_r;
   end
   else begin : GEN_comb
      assign m_axis_tvalid = o_valid;
      assign m_axis_tdata  = o_data;
   end
endgenerate

assign s_axis_tready = ~full;

endmodule
