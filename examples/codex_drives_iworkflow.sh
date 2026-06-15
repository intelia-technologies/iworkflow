#!/usr/bin/env bash
# Proof that CODEX can DRIVE iworkflow via MCP — the original goal.
#
# Registers the iworkflow MCP server inline (no global ~/.codex pollution) and
# asks headless `codex exec` to call its tool. Verified working: codex invokes
# iworkflow_ping and returns the engine's response.
#
# Swap the prompt to actually run a workflow, e.g.:
#   "Call iworkflow_workflow with goal='should we adopt event sourcing?' and
#    report the synthesized answer."
# (that one spawns real Codex/Gemini/Claude workers on your subscriptions.)
set -euo pipefail
cd "$(dirname "$0")/.."

PROMPT="${1:-Call the iworkflow_ping tool and report EXACTLY the JSON it returns, nothing else.}"

echo "$PROMPT" | codex exec \
  --ignore-user-config \
  --skip-git-repo-check --color never \
  --dangerously-bypass-approvals-and-sandbox \
  -c 'mcp_servers.iworkflow.command=".venv/bin/python"' \
  -c 'mcp_servers.iworkflow.args=["-m","iworkflow.mcp_server"]' \
  -o /dev/stdout -
