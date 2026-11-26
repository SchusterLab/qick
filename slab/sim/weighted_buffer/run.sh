#!/usr/bin/env bash
# Simulation of axis_weighted_buffer v1.3 against the numpy model in model.py.
# Usage: run.sh WORK_DIR [CASE ...]            (cases: `model.py list`; default all)
#        NETLIST=1 run.sh WORK_DIR [CASE ...]  same cases on the post-synthesis netlist
#                                              (N_WGT=13 cases only)
# On the lab machine the Vivado container sees only /home/slab, /opt/xilinx and /tmp:
#   slab/sim/weighted_buffer/run.sh /tmp/wbsim
# Copies the IP sources to WORK_DIR, generates the simulation model of the DSP
# macro from its .xci (part xczu49dr as in the lab ZCU216 build; Vivado 2024.1
# upgrades the core from rev 3 to rev 6), compiles with xsim, writes each case's
# stimulus with model.py, simulates it, and checks the result with model.py.
#
# One edit is made to the simulated copy of avg_buffer.v (not to the repository):
# the 13/14-bit weight start address is zero-extended explicitly where it drives
# the 32-bit VHDL port DW_ADDR_REG of matched_filter. Unpatched, xsim's
# mixed-language port association loses the address (a weight load at address
# 128 lands at 0); Vivado synthesis connects the low bits (warning Synth 8-689),
# and the post-synthesis netlist loads at the right address (NETLIST=1, case reload).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
W="$(mkdir -p "$1" && cd "$1" && pwd)"; shift
VSH="${VIVADO_SHELL:-/opt/xilinx/vivado-shell}"
PY="${PYTHON:-$REPO/.venv/bin/python}"   # needs numpy
NETLIST="${NETLIST:-0}"
S="$REPO/firmware/ip/axis_weighted_buffer/src"
mkdir -p "$W/src"
cp "$S"/*.v "$S"/*.sv "$S"/*.vhd "$S"/fifo/*.vhd "$REPO/firmware/hdl/fifo_dc_axi_xpm.sv" "$W/src/"
cp "$HERE/tb.sv" "$HERE/model.py" "$W/"

# 1. DSP macro, generated from the IP's own configuration
if [ ! -f "$W/ip/dsp_macro_0.vhd" ]; then
    mkdir -p "$W/ip/gen"
    cp "$S/dsp_macro_0/dsp_macro_0.xci" "$W/ip/gen/"
    cat > "$W/ip/gen.tcl" <<'EOS'
create_project -force -part xczu49dr-ffvf1760-2-e dspgen dspgen
import_ip gen/dsp_macro_0.xci
set ip [get_ips dsp_macro_0]
upgrade_ip $ip
foreach p {pipeline_options areg_3 areg_4 breg_3 breg_4 creg_3 creg_4 creg_5 mreg_5 preg_6 C_REG_CONFIG C_OPMODES C_LATENCY} {
    puts "DSPCFG $p = [get_property CONFIG.$p $ip]"
}
generate_target simulation $ip
set f [glob dspgen/dspgen.gen/sources_1/ip/dsp_macro_0/sim/dsp_macro_0.vhd]
file copy -force $f dsp_macro_0.vhd
EOS
    "$VSH" bash -c "cd '$W/ip' && vivado -mode batch -nojournal -nolog -source gen.tcl > gen.log 2>&1" \
        || { grep -E "ERROR" "$W/ip/gen.log"; exit 1; }
    grep DSPCFG "$W/ip/gen.log"
fi

if [ "$NETLIST" = 1 ]; then
  # 2a. out-of-context synthesis (N_WGT=13) and a functional netlist
  if [ ! -f "$W/synth/netlist13.v" ]; then
    mkdir -p "$W/synth"
    cat > "$W/synth/synth.tcl" <<'EOS'
create_project -in_memory -part xczu49dr-ffvf1760-2-e
set_property target_language Verilog [current_project]
read_ip ../ip/dspgen/dspgen.srcs/sources_1/ip/dsp_macro_0/dsp_macro_0.xci
set_property GENERATE_SYNTH_CHECKPOINT false [get_files dsp_macro_0.xci]
generate_target all [get_ips dsp_macro_0]
read_vhdl -vhdl2008 [list ../src/synchronizer_n.vhd ../src/bram_dp.vhd ../src/bram_simple_dp.vhd ../src/fifo.vhd ../src/fifo_axi.vhd ../src/rd2axi.vhd ../src/data_writer.vhd ../src/data_reader.vhd ../src/axi_slv.vhd ../src/matched_filter.vhd]
read_verilog -sv [list ../src/fifo_dc_axi_xpm.sv ../src/avg.sv ../src/buffer.sv]
read_verilog [list ../src/avg_top.v ../src/buffer_top.v ../src/avg_buffer.v ../src/axis_weighted_buffer.v]
synth_design -top axis_weighted_buffer -mode out_of_context -generic N_AVG=13 -generic N_BUF=12 -generic N_WGT=13 -generic B=16
write_verilog -force -mode funcsim netlist13.v
EOS
    "$VSH" bash -c "cd '$W/synth' && vivado -mode batch -nojournal -nolog -source synth.tcl > synth.log 2>&1" \
        || { grep -E "^ERROR" "$W/synth/synth.log"; exit 1; }
    grep "Synth 8-689" "$W/synth/synth.log" | sed 's/ \[.*//' | sort -u
  fi
  cat > "$W/compile.sh" <<'EOS'
set -e
cd "$(dirname "$0")"
xvlog -sv synth/netlist13.v tb.sv "$XILINX_VIVADO/data/verilog/src/glbl.v" > xvlog.log 2>&1 || { grep ERROR xvlog.log | head; exit 1; }
xelab --relax tb glbl -L unisims_ver -L xpm -timescale 1ps/1ps -override_timeunit -override_timeprecision -s snap13 \
    > xelab13.log 2>&1 || { grep ERROR xelab13.log | head; exit 1; }
EOS
else
  # 2b. RTL, with the address zero-extension described above
  sed -i 's/\.DW_ADDR_REG(WGT_DW_ADDR_REG),/.DW_ADDR_REG({{(32-N_WGT){1'"'"'b0}}, WGT_DW_ADDR_REG}),   \/\/ sim only, see run.sh/' "$W/src/avg_buffer.v"
  grep -q "sim only, see run.sh" "$W/src/avg_buffer.v" || { echo "avg_buffer.v changed: address patch not applied"; exit 1; }
  cat > "$W/compile.sh" <<'EOS'
set -e
cd "$(dirname "$0")"
LIBS="-L dsp_macro_v1_0_6 -L xbip_dsp48_wrapper_v3_0_6 -L xbip_pipe_v3_0_9 -L xbip_utils_v3_0_13 -L xpm -L unisims_ver -L unisim"
xvhdl --2008 src/synchronizer_n.vhd src/bram_dp.vhd src/bram_simple_dp.vhd src/fifo.vhd src/fifo_axi.vhd \
    src/rd2axi.vhd src/data_writer.vhd src/data_reader.vhd src/axi_slv.vhd ip/dsp_macro_0.vhd src/matched_filter.vhd \
    > xvhdl.log 2>&1 || { grep ERROR xvhdl.log | head; exit 1; }
xvlog -sv src/fifo_dc_axi_xpm.sv src/avg.sv src/buffer.sv src/avg_top.v src/buffer_top.v src/avg_buffer.v \
    src/axis_weighted_buffer.v tb.sv "$XILINX_VIVADO/data/verilog/src/glbl.v" \
    > xvlog.log 2>&1 || { grep ERROR xvlog.log | head; exit 1; }
for nw in 13 14; do
  xelab --relax tb glbl $LIBS -generic_top "N_WGT=$nw" -timescale 1ps/1ps -override_timeunit -override_timeprecision -s snap$nw \
      > xelab$nw.log 2>&1 || { grep ERROR xelab$nw.log | head; exit 1; }
done
EOS
fi
"$VSH" bash "$W/compile.sh"

# 3. cases
CASES=("$@")
[ ${#CASES[@]} -gt 0 ] || mapfile -t CASES < <("$PY" "$HERE/model.py" list)
fail=0
for c in "${CASES[@]}"; do
    d="$W/cases/$c"; rm -rf "$d"; mkdir -p "$d"
    "$PY" "$HERE/model.py" gen "$c" "$d" > "$d/gen.log"
    gap=$(cat "$d/gap"); nw=$(cat "$d/nwgt")
    if [ "$NETLIST" = 1 ] && [ "$nw" != 13 ]; then echo "== $c: skipped (netlist is N_WGT=13)"; continue; fi
    "$VSH" bash -c "cd '$d' && xsim snap$nw -R --xsimdir '$W/xsim.dir' -testplusarg SCRIPT=script.txt -testplusarg GAP=$gap > sim.log 2>&1" || true
    grep -E "^ERROR|Fatal" "$d/sim.log" && fail=1
    "$PY" "$HERE/model.py" check "$c" "$d" || fail=1
done
exit $fail
