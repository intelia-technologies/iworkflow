---
name: workflows-ship
description: Use when the user says "$workflows-ship", "/workflows:ship", or asks Codex to run the full iworkflow local pipeline
---

# Workflows Ship

Read `.claude/commands/workflows/ship.md` completely and execute that workflow.
Translate `/workflows:ship` to `$workflows-ship` for Codex usage.

Preserve iworkflow hard rules: subscription-only workers, deterministic
orchestration, stdlib-only core, and subscription-free verification first.
