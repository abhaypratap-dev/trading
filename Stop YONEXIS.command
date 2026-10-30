#!/bin/bash
cd "$(dirname "$0")"
export PATH="/Applications/Docker.app/Contents/Resources/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
docker compose down && echo "YONEXIS stopped (data is kept)."
read -n 1 -s -r -p "Press any key to close"
