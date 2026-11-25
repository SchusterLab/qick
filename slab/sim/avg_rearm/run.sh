#!/usr/bin/env bash
# Re-arming the accumulated buffer while the readout trigger is high. Usage: run.sh WORK_DIR
# On the lab machine (Vivado container sees /home/slab, /opt/xilinx, /tmp only):
#   slab/sim/avg_rearm/run.sh /home/slab/tmp-qproc-sim/avg
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
W="$1"; mkdir -p "$W"
cp "$REPO/firmware/ip/axis_avg_buffer/src/avg.sv" "$HERE/tb.sv" "$W/"
cat > "$W/sim.sh" <<'EOS'
cd "$(dirname "$0")"
xvlog -sv tb.sv avg.sv > xvlog.log 2>&1 || { grep ERROR xvlog.log; exit 1; }
xelab tb -timescale 1ps/1ps -override_timeunit -override_timeprecision -s t > xelab.log 2>&1 || { grep ERROR xelab.log; exit 1; }
xsim t -R | grep -E "ORDER|write|RESULT"
EOS
"${VIVADO_SHELL:-/opt/xilinx/vivado-shell}" bash "$W/sim.sh"
