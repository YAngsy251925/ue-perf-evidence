#!/usr/bin/env bash
# Usage: coexist_run.sh <bat> <run_dir> [timeout_s] [marker1] [marker2] [linger_s]
# Launch an experiment bat in the background (it receives <run_dir> as %1 and writes
# engine.log there), poll <run_dir>/engine.log for two markers, then kill ONLY the
# editor we started: by its own PID, never /T (the editor may own the shared Zen server).
BAT="$1"; RUN="$2"; TMO="${3:-180}"
M1="${4:-Single Frame}"; M2="${5:-Events history}"; LINGER="${6:-3}"
if [ -z "$BAT" ] || [ -z "$RUN" ]; then
  echo "usage: coexist_run.sh <bat> <run_dir> [timeout_s] [marker1] [marker2] [linger_s]" >&2
  exit 2
fi
[ -f "$BAT" ] || { echo "bat not found: $BAT" >&2; exit 2; }
mkdir -p "$RUN" || exit 1
BAT_W=$(cygpath -w "$(cd "$(dirname "$BAT")" && pwd)/$(basename "$BAT")")
RUN_W=$(cygpath -w "$(cd "$RUN" && pwd)")
LOG="$RUN/engine.log"
rm -f "$LOG"
cmd //c "$BAT_W" "$RUN_W" > "$RUN/launcher_stdout.txt" 2>&1 &
BPID=$!
sleep 2
WPID=$(cat /proc/$BPID/winpid 2>/dev/null)
echo "launched bash_pid=$BPID win_pid=$WPID run_dir=$RUN_W markers=[$M1] [$M2]"
START=$(date +%s)
FOUND=0
while :; do
  sleep 2
  NOW=$(date +%s); EL=$((NOW-START))
  F=0; E=0
  grep -q "$M1" "$LOG" 2>/dev/null && F=1
  grep -q "$M2" "$LOG" 2>/dev/null && E=1
  if grep -q "Assertion failed" "$LOG" 2>/dev/null; then echo "ASSERT_SEEN at ${EL}s"; break; fi
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
