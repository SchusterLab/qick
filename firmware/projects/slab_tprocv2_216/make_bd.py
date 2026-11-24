#!/usr/bin/env python3
"""Generate this project's block design from the stock ZCU216 tProc v2 design.

Run from this directory:  python3 make_bd.py
Reads ../qick_tprocv2_216_standard/bd_2023-1.tcl and writes bd_2024-1.tcl.
Re-run it after an upstream sync changes the stock design, and review the diff.

Changes relative to stock:
1. Vivado 2024.1 (the version the lab can run).
2. All ten readout buffers are axis_weighted_buffer (integration weights),
   with as many weights as accumulated samples (2**N_AVG).
3. Weights are loaded through a new DMA and switch (axi_dma_wgt,
   axis_switch_wgt): the existing envelope switch has no free ports.
4. qick_processor has TIME_SYNC_PERIOD = 28, so time zero always falls on the
   same point of the 28-cycle pattern of the tProc, generator and readout clocks.
"""
import re
from pathlib import Path

here = Path(__file__).resolve().parent
src = here.parent / "qick_tprocv2_216_standard" / "bd_2023-1.tcl"
s = src.read_text()
N_BUFFERS = 10
TIME_SYNC_PERIOD = 28

def once(pattern, repl, text, flags=0):
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        raise SystemExit("pattern not found exactly once: %s" % pattern)
    return new

# 1. Vivado version
s = once(r"set scripts_vivado_version 2023\.1", "set scripts_vivado_version 2024.1", s)

# 2. weighted buffers in place of average buffers
assert len(re.findall(r"create_bd_cell -type ip -vlnv QICK:QICK:axis_avg_buffer:1\.2 axis_avg_buffer_\d+ ", s)) == N_BUFFERS
s = s.replace("QICK:QICK:axis_avg_buffer:1.2", "QICK:QICK:axis_weighted_buffer:1.3")
s = s.replace("axis_avg_buffer_", "axis_weighted_buffer_")
def add_wgt(m):
    return m.group(0) + "  set_property CONFIG.N_WGT {%s} $axis_weighted_buffer_%s\n" % (m.group(2), m.group(1))
# every buffer sets N_AVG in a set_property that ends with "$axis_weighted_buffer_<n>"
pat = re.compile(r"  set axis_weighted_buffer_(\d+) \[ create_bd_cell[^\n]*\n(?:(?!\n\n).)*?CONFIG\.N_AVG \{(\d+)\}(?:(?!\n\n).)*?\$axis_weighted_buffer_\1\n", re.S)
s, n = pat.subn(add_wgt, s)
assert n == N_BUFFERS, n

# 3. DMA and switch for loading weights
s = once(r"(  # Create instance: axi_dma_mr, and set properties\n)", r"""  # Create instance: axi_dma_wgt, and set properties
  set axi_dma_wgt [ create_bd_cell -type ip -vlnv xilinx.com:ip:axi_dma:7.1 axi_dma_wgt ]
  set_property -dict [list \\
    CONFIG.c_include_s2mm {0} \\
    CONFIG.c_include_sg {0} \\
    CONFIG.c_sg_length_width {26} \\
  ] $axi_dma_wgt


  # Create instance: axis_switch_wgt, and set properties
  set axis_switch_wgt [ create_bd_cell -type ip -vlnv xilinx.com:ip:axis_switch:1.1 axis_switch_wgt ]
  set_property -dict [list \\
    CONFIG.DECODER_REG {1} \\
    CONFIG.NUM_MI {%d} \\
    CONFIG.NUM_SI {1} \\
    CONFIG.ROUTING_MODE {1} \\
  ] $axis_switch_wgt


\1""" % N_BUFFERS, s)
m = re.search(r"set_property CONFIG\.NUM_SI \{(\d+)\} \$axi_smc\n", s); smc_port = int(m.group(1))
s = s.replace(m.group(0), "set_property CONFIG.NUM_SI {%d} $axi_smc\n" % (smc_port + 1))
m = re.search(r"set_property CONFIG\.NUM_MI \{(\d+)\} \$ps8_0_axi_periph\n", s); mi = int(m.group(1))
s = s.replace(m.group(0), "set_property CONFIG.NUM_MI {%d} $ps8_0_axi_periph\n" % (mi + 2))

intf = ["  connect_bd_intf_net -intf_net axi_dma_wgt_M_AXIS_MM2S [get_bd_intf_pins axi_dma_wgt/M_AXIS_MM2S] [get_bd_intf_pins axis_switch_wgt/S00_AXIS]",
        "  connect_bd_intf_net -intf_net axi_dma_wgt_M_AXI_MM2S [get_bd_intf_pins axi_dma_wgt/M_AXI_MM2S] [get_bd_intf_pins axi_smc/S%02d_AXI]" % smc_port,
        "  connect_bd_intf_net -intf_net ps8_0_axi_periph_M%02d_AXI [get_bd_intf_pins axi_dma_wgt/S_AXI_LITE] [get_bd_intf_pins ps8_0_axi_periph/M%02d_AXI]" % (mi, mi),
        "  connect_bd_intf_net -intf_net ps8_0_axi_periph_M%02d_AXI [get_bd_intf_pins axis_switch_wgt/S_AXI_CTRL] [get_bd_intf_pins ps8_0_axi_periph/M%02d_AXI]" % (mi + 1, mi + 1)]
for i in range(N_BUFFERS):
    intf.append("  connect_bd_intf_net -intf_net axis_switch_wgt_M%02d_AXIS [get_bd_intf_pins axis_switch_wgt/M%02d_AXIS] [get_bd_intf_pins axis_weighted_buffer_%d/s1_axis_weights]" % (i, i, i))
# the root design's connections are the last "Create port connections" block (the first is inside the ddr4 hierarchy)
marker = "  # Create port connections\n"
k = s.rindex(marker)
s = s[:k] + "\n".join(intf) + "\n\n" + s[k:]

def extend_net(text, net, pins):
    pat = re.compile(r"(  connect_bd_net -net %s [^\n]*)\n" % re.escape(net))
    add = " ".join("[get_bd_pins %s]" % p for p in pins)
    # the root design's net is the last one with this name (hierarchies define their own)
    m = list(pat.finditer(text))[-1]
    return text[:m.end(1)] + " " + add + text[m.end(1):]
s = extend_net(s, "zynq_ultra_ps_e_0_pl_clk0", ["axi_dma_wgt/s_axi_lite_aclk", "axi_dma_wgt/m_axi_mm2s_aclk", "axis_switch_wgt/aclk", "axis_switch_wgt/s_axi_ctrl_aclk",
                                               "ps8_0_axi_periph/M%02d_ACLK" % mi, "ps8_0_axi_periph/M%02d_ACLK" % (mi + 1)])
s = extend_net(s, "rst_ps8_0_99M_peripheral_aresetn", ["axi_dma_wgt/axi_resetn", "axis_switch_wgt/aresetn", "axis_switch_wgt/s_axi_ctrl_aresetn",
                                                      "ps8_0_axi_periph/M%02d_ARESETN" % mi, "ps8_0_axi_periph/M%02d_ARESETN" % (mi + 1)])

# addresses: copy every line that places axi_dma_gen's memory-side address space, and add two register blocks
dma_lines = [l for l in s.split("\n") if "axi_dma_gen/Data_MM2S" in l]
assert len(dma_lines) == 4, len(dma_lines)
used = [int(x, 16) for x in re.findall(r"assign_bd_address -offset (0x0004[0-9A-Fa-f]+) -range 0x[0-9A-Fa-f]+ -target_address_space \[get_bd_addr_spaces zynq_ultra_ps_e_0/Data\]", s)]
rng = {int(a, 16): int(b, 16) for a, b in re.findall(r"assign_bd_address -offset (0x0004[0-9A-Fa-f]+) -range (0x[0-9A-Fa-f]+) -target_address_space \[get_bd_addr_spaces zynq_ultra_ps_e_0/Data\]", s)}
top = max(a + r for a, r in rng.items())
top = (top + 0xFFFF) & ~0xFFFF
new_addr = ["  assign_bd_address -offset 0x%012X -range 0x00010000 -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] [get_bd_addr_segs axi_dma_wgt/S_AXI_LITE/Reg] -force" % top,
            "  assign_bd_address -offset 0x%012X -range 0x00010000 -target_address_space [get_bd_addr_spaces zynq_ultra_ps_e_0/Data] [get_bd_addr_segs axis_switch_wgt/S_AXI_CTRL/Reg] -force" % (top + 0x10000)]
assign = [l.replace("axi_dma_gen", "axi_dma_wgt") for l in dma_lines if "assign_bd_address" in l] + new_addr
exclude = [l.replace("axi_dma_gen", "axi_dma_wgt") for l in dma_lines if "exclude_bd_addr_seg" in l]
first_ex = next(l for l in s.split("\n") if l.strip().startswith("exclude_bd_addr_seg"))
s = s.replace(first_ex, "\n".join(assign) + "\n" + first_ex, 1)
last_ex = [l for l in s.split("\n") if l.strip().startswith("exclude_bd_addr_seg")][-1]
s = s.replace(last_ex, last_ex + "\n" + "\n".join(exclude), 1)

# 4. tProc time reset aligned to the common period of the clocks
s = once(r"(    CONFIG\.OUT_TRIG_QTY \{\d+\} \\\n)", r"\1    CONFIG.TIME_SYNC_PERIOD {%d} \\\n" % TIME_SYNC_PERIOD, s)

(here / "bd_2024-1.tcl").write_text(s)
print("wrote bd_2024-1.tcl: %d weighted buffers, DMA on axi_smc/S%02d_AXI, registers at 0x%X and 0x%X" % (N_BUFFERS, smc_port, top, top + 0x10000))
