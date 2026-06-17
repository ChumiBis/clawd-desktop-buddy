#!/usr/bin/env bash
# Live view of the clawd-mood daemon: aggregate count/face + per-session
# breakdown (session id, project dir, state, age, counted/stale). Refreshes 1/s.
#   ./plugin/scripts/watch.sh
F="$(python3 -c 'import tempfile,os;print(os.path.join(tempfile.gettempdir(),"clawd-mood-status.log"))')"
while true; do
  clear
  if [ -f "$F" ]; then cat "$F"; else echo "no status yet — is the daemon running?"; fi
  sleep 1
done
