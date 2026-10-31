#!/usr/bin/env bash
# Commit today's work and publish the two branches:
#   dev  = the complete version (with the local AI model) - your local branch
#   main = the same code for the cloud: deployed with LLM_ENABLED=false (docker-compose.azure.yml), so the AI writing
#          features are switched off there. Keeping one codebase means fixes in dev reach main with a simple merge.
set -euo pipefail
cd "$(dirname "$0")/.."
git checkout -B dev                      # new branch from the current one, keeps the uncommitted work
git add -A
git commit -m "YONEXIS AI tab, AI backtest, data check, continuous futures, cloud mode without AI model

- YONEXIS AI: zone replay for a day / date range, configurations with lots and per-lot targets,
  Zone / IZ target multiplier, spike option, configurable square-off, refined data, target calculation
- AI backtest: every configuration on every future, best sets of 5-40 futures (honest pick + maximum), investment
- Data check + refetch of missing days; continuous futures history from expired contracts (Upstox Plus)
- Fix: saving candles no longer wipes the days between two fetched days; futures history uses the near month
- LLM_ENABLED=false switches the AI model features off (cloud); docker-compose.azure.yml + scripts/deploy-azure.sh

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01KkywERdwLJiEVPVZAXF5pd" || echo "(nothing new to commit)"
git push -u origin dev
git checkout main
git merge --ff-only dev
git push origin main
git checkout dev
echo
echo "dev and main pushed. You are back on dev (the complete local version)."
echo "Next: scripts/deploy-azure.sh   (deploys main to your Azure VM without the AI model)"
