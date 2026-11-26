#!/usr/bin/env bash
# Dispatcher-overflow simulation of the tProc v2. Usage: run.sh WORK_DIR [CASE ...]
# A case is NAME:N:REPS:SPACING_US:WAIT_EVERY:CFG_HEX:RESTART, e.g. p300:300:3:0.3:0:0:0
# Needs the repository's .venv (to build programs) and Vivado's simulator. On the lab
# machine, whose Vivado container only sees /home/slab, /opt/xilinx and /tmp:
#   slab/sim/fifo_overflow/run.sh /home/slab/tmp-qproc-sim
# copies sources and builds programs here, then runs xsim through /opt/xilinx/vivado-shell.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
W="$1"; shift
CASES=("$@")
[ ${#CASES[@]} -gt 0 ] || CASES=(n300:300:2:2:0:0:0 n300_nostall_restart:300:2:2:0:800:1 n500:500:1:1:0:0:0 w500:500:1:1:-2:0:0 loop2000:2000:1:1:-1:0:0)
VSH="${VIVADO_SHELL:-/opt/xilinx/vivado-shell}"
mkdir -p "$W/src"
S="${QPROC_SRC:-$REPO/firmware/ip/qick_processor/src}"   # QPROC_SRC: simulate another copy of the sources
cp "$S"/*.sv "$S"/*.svh \
   "$S"/*.v "$S"/axi_slv_qproc.vhd "$W/src/"
rm -f "$W/src/axis_qick_processor.sv"
cp "$HERE/tb.sv" "$W/"
cat > "$W/compile.sh" <<'EOS'
set -e
cd "$(dirname "$0")"
xvlog -sv --relax $XVLOG_DEFS -i src src/_qproc_ips.sv src/qproc_*.sv src/qcore_*.sv src/qick_processor.sv tb.sv \
   src/qcore_mem.v src/qproc_mem_ctrl.v src/mem_rw.v src/data_mem_ctrl.v src/axis_read.v src/axis_write.v \
   "$XILINX_VIVADO/data/verilog/src/glbl.v" > xvlog.log 2>&1 || { grep -E "ERROR" xvlog.log | head; exit 1; }
xvhdl src/axi_slv_qproc.vhd > xvhdl.log 2>&1 || { grep ERROR xvhdl.log | head; exit 1; }
xelab --relax tb glbl -L xpm -L unisims_ver -L unimacro_ver -timescale 1ps/1ps -override_timeunit -override_timeprecision -s snap > xelab.log 2>&1 || { grep ERROR xelab.log | head; exit 1; }
EOS
"$VSH" env XVLOG_DEFS="${XVLOG_DEFS:-}" bash "$W/compile.sh"
for c in "${CASES[@]}"; do
    IFS=: read -r name n reps sp we cfg rs <<< "$c"
    d="$W/$name"; mkdir -p "$d"
    if ! "$REPO/.venv/bin/python" "$HERE/make_prog.py" "$d" "$n" "$reps" "$sp" "$we" > "$d/make_prog.log" 2>&1; then
        echo "== $name: program generation failed:"; tail -1 "$d/make_prog.log"; continue
    fi
    # simulated time: the whole schedule plus margin
    run_ns=$(python3 -c "print(int(($n*$sp+3)*$reps*1000*1.05+20000))")
    cat > "$d/sim.sh" <<EOS
cd "$d" && xsim snap -R --xsimdir "$W/xsim.dir" -testplusarg CFG=$cfg -testplusarg RUN_NS=$run_ns -testplusarg RESTART=$rs ${SIM_ARGS:-} > sim.log 2>&1
EOS
    "$VSH" bash "$d/sim.sh" || true
    echo "== $name (N=$n reps=$reps wait_every=$we TPROC_CFG=0x$cfg restart=$rs)"
    grep -E "RESULT|FIRST LOSS|ERROR|Fatal" "$d/sim.log" || tail -5 "$d/sim.log"
done
