#!/bin/bash
# Double-click in Finder to install/start YONEXIS. Safe to run again any time.
cd "$(dirname "$0")"
if bash scripts/setup_mac.sh 2>&1 | tee setup.log; [ "${PIPESTATUS[0]}" -eq 0 ]; then
  echo; echo "All done. You can close this window; YONEXIS keeps running in Docker."
else
  echo; echo "Setup stopped (see the message above; full log in setup.log). Fix it and double-click again."
fi
read -n 1 -s -r -p "Press any key to close"
