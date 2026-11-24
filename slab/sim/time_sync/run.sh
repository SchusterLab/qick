#!/usr/bin/env bash
# Simulate qproc_ctrl with and without TIME_SYNC_PERIOD. Usage: run.sh [work_dir]
# Needs Vivado's simulator on PATH (on the lab machine: /opt/xilinx/vivado-shell bash <copy of this script> <dir under /tmp>).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
W="${1:-$(mktemp -d)}"; mkdir -p "$W/src"
if [ -d "$HERE/../../../firmware" ]; then
    cp "$HERE/../../../firmware/ip/qick_processor/src/"{qproc_ctrl.sv,qproc_time_ctrl.sv,_qproc_ips.sv,_qproc_defines.svh} "$W/src/"
    [ "$HERE" = "$W" ] || cp "$HERE/tb.sv" "$W/"
fi
cd "$W"
xvlog -sv -i src tb.sv src/_qproc_ips.sv src/qproc_time_ctrl.sv src/qproc_ctrl.sv "$XILINX_VIVADO/data/verilog/src/glbl.v" > xvlog.log 2>&1 || { grep ERROR xvlog.log | head; exit 1; }
xelab tb glbl -L unisims_ver -L unimacro_ver -L xpm -timescale 1ps/1ps -override_timeunit -override_timeprecision -s t > xelab.log 2>&1 || { grep ERROR xelab.log | head; exit 1; }
xsim t -R 2>&1 | grep -E 'RESULT|PASS|FAIL'
