---
name: orchestrate-with-iworkflow
description: Use when authoring or running iworkflow recipes, dynamic workflow specs, multi-agent fan-out, gated agent pipelines, or subscription-CLI orchestration in this repo
---

# Orchestrate With iworkflow

Read `.claude/skills/orchestrate-with-iworkflow/SKILL.md` completely and follow
it. Translate Claude-specific tool names or slash-command references to the
nearest Codex equivalents.

Hard constraints for this repo:

- Workers are subscription-authenticated CLIs, never provider APIs.
- `iworkflow/` remains stdlib-only.
- Orchestration is deterministic Python/spec execution.
- Use `FakeProvider` tests before live CLI adapter smoke tests.
