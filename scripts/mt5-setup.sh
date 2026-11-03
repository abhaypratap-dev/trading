#!/usr/bin/env bash
# MetaTrader 5 on this Mac -> YONEXIS (file bridge).
#  1. finds MT5's "Common Files" folder inside the MetaTrader 5 app's Wine prefix and saves it as MT5_COMMON_DIR in .env
#     (docker-compose mounts it into the api container at /mt5_common);
#  2. copies the YonexisBridge Expert Advisor into MT5's MQL5/Experts folder.
# Then: open MetaEditor in MT5 (F4), open Experts/YonexisBridge.mq5, press Compile, drag it onto any chart.
set -euo pipefail
cd "$(dirname "$0")/.."
PREFIX="$HOME/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c"
[ -d "$PREFIX" ] || { echo "MetaTrader 5's Wine folder not found at: $PREFIX - start MT5 once, then rerun."; exit 1; }
COMMON=$(find "$PREFIX/users" -type d -path "*MetaQuotes/Terminal/Common" 2>/dev/null | head -1)
if [ -z "$COMMON" ]; then
  U=$(ls "$PREFIX/users" | grep -v -i public | head -1)
  COMMON="$PREFIX/users/$U/AppData/Roaming/MetaQuotes/Terminal/Common"
fi
mkdir -p "$COMMON/Files/YONEXIS/in" "$COMMON/Files/YONEXIS/out"
DIR="$COMMON/Files"
echo "MT5 Common Files folder: $DIR"
if grep -q '^MT5_COMMON_DIR=' .env 2>/dev/null; then
  grep -v '^MT5_COMMON_DIR=' .env > .env.tmp && mv .env.tmp .env
fi
echo "MT5_COMMON_DIR=\"$DIR\"" >> .env      # quoted: the path has spaces
echo "saved MT5_COMMON_DIR in .env"
EXPERTS=$(find "$PREFIX" -type d -path "*MQL5/Experts" 2>/dev/null | grep -v -i "/Tester/" | head -1)
if [ -n "$EXPERTS" ]; then
  cp backend/yonexis/mt5/YonexisBridge.mq5 "$EXPERTS/YonexisBridge.mq5"
  echo "copied the Expert Advisor to: $EXPERTS/YonexisBridge.mq5"
else
  echo "MQL5/Experts folder not found - in MT5 use File > Open Data Folder > MQL5 > Experts and copy backend/yonexis/mt5/YonexisBridge.mq5 there"
fi
echo
echo "Next:"
echo "  1. restart YONEXIS (Stop + Start YONEXIS) so the api container sees the folder"
echo "  2. in MT5 press F4 (MetaEditor) > Experts > YonexisBridge.mq5 > Compile"
echo "  3. drag YonexisBridge onto any chart, tick 'Allow Algo Trading', switch on Algo Trading in the toolbar"
echo "  4. YONEXIS > System > MT5 connection: transport File, mode Dry run or Demo; MT5 tab > Ping"
