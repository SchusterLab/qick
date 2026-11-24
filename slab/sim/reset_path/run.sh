#!/usr/bin/env bash
# Simulate the phase-reset path from the tProc clock domain into the generator
# and dynamic-readout control blocks, at the stock ZCU216 clock rates.
# Usage: slab/sim/reset_path/run.sh [work_dir]
# Needs Vivado's simulator (xvlog, xvhdl, xelab, xsim) on PATH. On the lab machine,
# whose Vivado container only sees /home/slab, /opt/xilinx and /tmp, run it as:
#   /opt/xilinx/vivado-shell bash /tmp/<work_dir>/run.sh /tmp/<work_dir>
# after copying this directory and the sources listed below into the work directory
# (the script does the copy itself when it can see the repository).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="${1:-$(mktemp -d)}"
mkdir -p "$W/src"
if [ -d "$HERE/../../../firmware" ]; then
    F="$HERE/../../../firmware"
    cp "$F/ip/axis_cdcsync_v1/src/cdcsync.sv" "$F/hdl/fifo_dc_axi_xpm.sv" "$F/hdl/fifo_xpm.sv" \
       "$F/ip/axis_signal_gen_v6/src/ctrl_sg_v6.sv" "$F/ip/axis_dyn_readout_v1/src/ctrl_dyn_ro_v1.sv" \
       "$F/ip/axis_tmux_v1/src/tmux.sv" "$F/ip/qick_sg_translator/src/sg_translator.v" "$F/ip/axis_phrst_sync_v1/src/axis_phrst_sync_v1.sv" \
       "$F/ip/axis_dyn_readout_v1/src/fifo/bram_simple_dp.vhd" "$F/ip/axis_dyn_readout_v1/src/fifo/fifo.vhd" "$W/src/"
    [ "$HERE" = "$W" ] || cp "$HERE/tb.sv" "$HERE/summarise.py" "$W/"
fi
cd "$W"
xvlog -sv tb.sv src/cdcsync.sv src/fifo_dc_axi_xpm.sv src/fifo_xpm.sv src/ctrl_sg_v6.sv src/ctrl_dyn_ro_v1.sv src/tmux.sv src/axis_phrst_sync_v1.sv > xvlog.log 2>&1
xvlog src/sg_translator.v "$XILINX_VIVADO/data/verilog/src/glbl.v" >> xvlog.log 2>&1
xvhdl src/bram_simple_dp.vhd src/fifo.vhd > xvhdl.log 2>&1
# Static phases of the generator and readout clocks, in simulation time units.
for cfg in "7 13" "300 900" "555 41" "1000 2000"; do
    set -- $cfg
    for sync in 0 1; do
        xelab tb glbl -L xpm -timescale 1ps/1ps -override_timeunit -override_timeprecision \
            -generic_top GOFF=$1 -generic_top ROFF=$2 -generic_top USE_SYNC=$sync -s sim_$1_$2_s$sync > xelab.log 2>&1
        xsim sim_$1_$2_s$sync -R > sim_$1_$2_sync$sync.txt 2>&1
        echo "clock phases $cfg, sync block $sync: $(grep -c '^RES' sim_$1_$2_sync$sync.txt) resets simulated"
    done
done
echo "Results in $W; summarise with: python summarise.py (run in that directory, needs numpy)"
