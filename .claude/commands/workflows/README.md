# iworkflow local workflow commands

Project-local Claude Code workflow commands for iworkflow.

These commands use iworkflow artifacts:

- `docs/design/*.md` for durable brainstorms, designs, and plans.
- `.iworkflow/recipes/*.json` for host-registered reusable workflow specs.
- `.iworkflow/runs/<run_id>/` for execution journals and resume evidence.

Core verification commands:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
python3 examples/demo_fakes.py
python3 examples/workflow_dynamic.py
```

Live adapter smoke tests such as `python3 examples/demo_live.py codex gemini`
spend subscription quota and require explicit user approval.
