#!/usr/bin/env bash
# Usage: coexist_run.sh <bat> <run_dir> [timeout_s] [marker1] [marker2] [linger_s]
# Launch an experiment bat in the background (it receives <run_dir> as %1 and writes
# engine.log there), poll <run_dir>/engine.log for two markers, then kill ONLY the
# editor we started: by its own PID, never /T (the editor may own the shared Zen server).
# Duration mode (M2.1): pass "-" for both markers. timeout_s is then the startup budget: the
# editor must log "Engine is initialized" (LaunchEngineLoop.cpp:4974, the game loop starts right
# after) within it, and is stopped linger_s seconds after that. Cold starts took 12 s to over
# 100 s, so the run length is counted from the ready marker, never from launch.
BAT="$1"; RUN="$2"; TMO="${3:-180}"
M1="${4:-Single Frame}"; M2="${5:-Events history}"; LINGER="${6:-3}"
READY_MARK="Engine is initialized"
if [ -z "$BAT" ] || [ -z "$RUN" ]; then
  echo "usage: coexist_run.sh <bat> <run_dir> [timeout_s] [marker1] [marker2] [linger_s]" >&2
  exit 2
fi
[ -f "$BAT" ] || { echo "bat not found: $BAT" >&2; exit 2; }
DURATION=0
[ "$M1" = "-" ] && [ "$M2" = "-" ] && DURATION=1
mkdir -p "$RUN" || exit 1
BAT_W=$(cygpath -w "$(cd "$(dirname "$BAT")" && pwd)/$(basename "$BAT")")
RUN_W=$(cygpath -w "$(cd "$RUN" && pwd)")
LOG="$RUN/engine.log"
rm -f "$LOG"
cmd //c "$BAT_W" "$RUN_W" > "$RUN/launcher_stdout.txt" 2>&1 &
BPID=$!
sleep 2
WPID=$(cat /proc/$BPID/winpid 2>/dev/null)
if [ $DURATION -eq 1 ]; then
  echo "launched bash_pid=$BPID win_pid=$WPID run_dir=$RUN_W mode=duration startup<=${TMO}s run=${LINGER}s after ready"
else
  echo "launched bash_pid=$BPID win_pid=$WPID run_dir=$RUN_W markers=[$M1] [$M2]"
fi
START=$(date +%s)
FOUND=0
READY_AT=""
while :; do
  sleep 2
  NOW=$(date +%s); EL=$((NOW-START))
  if grep -q "Assertion failed" "$LOG" 2>/dev/null; then echo "ASSERT_SEEN at ${EL}s"; break; fi
  if [ $DURATION -eq 1 ]; then
    if ! kill -0 $BPID 2>/dev/null; then
      # clean_exit（P15）：引擎自己在停止事件后退出是预期结果，不算失败
      if grep -q "RequestExit(0, CsvProfiler.ExitAfterCsvProfiling)" "$LOG" 2>/dev/null; then
        FOUND=1; echo "CLEAN_EXIT at ${EL}s (engine exited on its own)"; break
      fi
      echo "PROCESS_EXITED at ${EL}s (early, without the clean-exit mark)"; break
    fi
    if [ -z "$READY_AT" ]; then
      if grep -q "$READY_MARK" "$LOG" 2>/dev/null; then
        READY_AT=$EL; echo "ENGINE_READY at ${EL}s"
      elif [ $EL -ge $TMO ]; then
        echo "STARTUP_TIMEOUT at ${EL}s (no '$READY_MARK' within ${TMO}s)"; break
      fi
    elif [ $((EL - READY_AT)) -ge "$LINGER" ]; then
      FOUND=1; echo "DURATION_ELAPSED at ${EL}s (${LINGER}s after ready)"; break
    fi
    continue
  fi
  F=0; E=0
  grep -q "$M1" "$LOG" 2>/dev/null && F=1
  grep -q "$M2" "$LOG" 2>/dev/null && E=1
  if [ $F -eq 1 ] && [ $E -eq 1 ]; then FOUND=1; echo "BOTH_MARKERS at ${EL}s"; sleep "$LINGER"; break; fi
  if ! kill -0 $BPID 2>/dev/null; then echo "PROCESS_EXITED at ${EL}s (m1=$F m2=$E)"; break; fi
  if [ $EL -ge $TMO ]; then echo "TIMEOUT at ${EL}s (m1=$F m2=$E)"; break; fi
done
if kill -0 $BPID 2>/dev/null && [ -n "$WPID" ]; then
  EPID=$(powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"ParentProcessId=$WPID AND Name='UnrealEditor-Cmd.exe'\" | Select-Object -ExpandProperty ProcessId" | tr -d '\r')
  if [ -n "$EPID" ]; then
    taskkill //PID "$EPID" //F > /dev/null 2>&1 && echo "killed own editor pid=$EPID (no tree kill)"
  else
    echo "EDITOR_PID_NOT_FOUND under cmd pid=$WPID; nothing killed"
  fi
fi
wait $BPID 2>/dev/null
echo "FOUND=$FOUND"
[ $FOUND -eq 1 ]
