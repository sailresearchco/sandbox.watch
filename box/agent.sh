#!/usr/bin/env bash
# One headless agent turn. $1 is a prompt file written by box/turn.py.
#
# opencode runs with Sail as its model provider (see ~/.config/opencode/
# opencode.jsonc, written by launch.py). Swap the whole command by setting
# SANDBOXWATCH_AGENT_CMD if you'd rather use a different harness.
set -euo pipefail
cd "$(dirname "$0")/.."
exec opencode run --model "${SANDBOXWATCH_MODEL:-sail/zai-org/GLM-5.2-FP8}" "$(cat "$1")"
