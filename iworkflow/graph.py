"""Mermaid and HTML diagram generator for iworkflow specifications."""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any
from .workflow import _TOKEN, _lookup

# --------------------------------------------------------------------------
# label rendering — resolve {{...}} templates against the spec's params and
# escape dynamic text so arbitrary values can't break Mermaid node syntax.
# Unresolved tokens are KEPT verbatim: a diagram documents which fields are
# parameterized, so an unbound `{{params.x}}` stays visible (not blanked).
# --------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _resolve(text: str, ctx: dict[str, Any]) -> Any:
    """Substitute {{...}} tokens via the runtime templating core, but keep any
    unresolved tokens intact as markers of parameterization."""
    def _sub(match: re.Match) -> str:
        token = match.group(0)
        path = match.group(1)
        val = _lookup(path, ctx)
        if val is None:
            return token  # Keep the unresolved template token verbatim
        return _fmt(val)
    return _TOKEN.sub(_sub, text)


def _esc(value: Any) -> str:
    """Escape a resolved value for embedding in a Mermaid quoted/HTML label."""
    if not isinstance(value, str):
        value = str(value)
    # Mermaid labels with quotes/HTML formatting need standard HTML entity escaping.
    return (
        html_lib.escape(value)
        .replace('"', "&quot;")
        .replace("'", "&#39;")
        .replace("\n", "<br/>")
    )


def _text(value: Any, ctx: dict[str, Any]) -> str:
    """Resolve templates (keeping unresolved tokens) then escape for a label."""
    return _esc(_resolve(value, ctx) if isinstance(value, str) else value)


def _join(seq: Any, ctx: dict[str, Any]) -> str:
    if isinstance(seq, str):
        seq = [seq]
    parts = []
    for x in (seq or []):
        if isinstance(x, dict):
            prov = x.get("provider") or x.get("name") or ""
            model = x.get("model")
            parts.append(f"{_text(prov, ctx)}:{_text(model, ctx)}" if model else _text(prov, ctx))
        else:
            parts.append(_text(x, ctx))
    return ", ".join(parts)


def _meta_lines(spec: dict[str, Any], ctx: dict[str, Any]) -> list[str]:
    """Common per-agent metadata: role/prefer/model/models/tools/timeout/heartbeat with strict fallback defaults."""
    lines: list[str] = []
    role = spec.get("role")
    lines.append(f"role: {_text(role, ctx)}" if role else "role: default")

    prefer = spec.get("prefer")
    lines.append(f"prefer: {_join(prefer, ctx)}" if prefer else "prefer: auto-route")

    model = spec.get("model")
    models = spec.get("models")
    if model:
        lines.append(f"model: {_text(model, ctx)}")
    elif models:
        models_dict = models
        if isinstance(models_dict, dict):
            m_parts = [f"{prov}→{_text(mod, ctx)}" for prov, mod in models_dict.items()]
            lines.append(f"models: {', '.join(m_parts)}")
    else:
        lines.append("model: default")

    tools = spec.get("tools")
    lines.append(f"tools: {_join(tools, ctx)}" if tools else "tools: none")

    timeout = spec.get("timeout_s")
    lines.append(f"timeout: {_esc(timeout)}s" if timeout else "timeout: 180s")

    heartbeat = spec.get("heartbeat_interval_s")
    lines.append(f"heartbeat: {_esc(heartbeat)}s" if heartbeat else "heartbeat: none")

    return lines


def render_agent(step_id: str, agent_spec: dict[str, Any],
                 ctx: dict[str, Any]) -> tuple[str, str, str]:
    label_lines = [f"<b>{_text(step_id, ctx)}</b> (agent)"]
    label_lines += _meta_lines(agent_spec, ctx)
    label = "<br/>".join(label_lines)
    node_def = f'    {step_id}["{label}"]'
    return node_def, step_id, step_id


def render_supervisor(step_id: str, step_spec: dict[str, Any],
                      ctx: dict[str, Any]) -> tuple[str, str, str]:
    label_lines = [f"<b>{_text(step_id, ctx)}</b> (supervisor)"]
    label_lines += _meta_lines(step_spec, ctx)
    if step_spec.get("watch"):
        label_lines.append(f"watch: {_join(step_spec.get('watch'), ctx)}")
    if step_spec.get("when"):
        label_lines.append("when: conditional guard")
    label = "<br/>".join(label_lines)
    node_def = f'    {step_id}{{"{label}"}}'
    return node_def, step_id, step_id


def render_parallel(step_id: str, step_spec: dict[str, Any],
                    ctx: dict[str, Any]) -> tuple[str, str, str]:
    agents = step_spec.get("agents") or []
    lines = []
    lines.append(f'    subgraph {step_id} ["parallel: {_esc(step_id)}"]')
    lines.append(f'        {step_id}_entry(( ))')
    lines.append(f'        {step_id}_exit(( ))')

    for i, a in enumerate(agents):
        a_id = a.get("id") or f"agent_{i}"
        full_a_id = f"{step_id}_{a_id}"
        a_label_lines = [f"<b>{_text(a_id, ctx)}</b>"]
        a_label_lines += _meta_lines(a, ctx)
        a_label = "<br/>".join(a_label_lines)
        lines.append(f'        {full_a_id}["{a_label}"]')
        lines.append(f'        {step_id}_entry --> {full_a_id} --> {step_id}_exit')

    lines.append('    end')

    return "\n".join(lines), f"{step_id}_entry", f"{step_id}_exit"


def render_pipeline(step_id: str, step_spec: dict[str, Any],
                    ctx: dict[str, Any]) -> tuple[str, str, str]:
    items = _text(step_spec.get("items", "items"), ctx)
    stages = step_spec.get("stages") or []
    lines = []
    lines.append(f'    subgraph {step_id} ["pipeline: {_esc(step_id)} ({items})"]')

    prev_node = None
    first_node = None
    last_node = None

    for i, s in enumerate(stages):
        s_id = s.get("id") or f"stage_{i}"
        full_s_id = f"{step_id}_{s_id}"
        s_label_lines = [f"Stage {i}: <b>{_text(s_id, ctx)}</b>"]
        s_label_lines += _meta_lines(s, ctx)
        s_label = "<br/>".join(s_label_lines)
        lines.append(f'        {full_s_id}["{s_label}"]')

        if first_node is None:
            first_node = full_s_id
        if prev_node:
            lines.append(f'        {prev_node} --> {full_s_id}')
        prev_node = full_s_id
        last_node = full_s_id

    lines.append('    end')

    return "\n".join(lines), first_node or step_id, last_node or step_id


def render_loop(step_id: str, step_spec: dict[str, Any], ctx: dict[str, Any],
                depth: int = 1) -> tuple[str, str, str]:
    max_iter = step_spec.get("max_iterations", 0)
    until = step_spec.get("until") or {}
    until_kind = list(until.keys())[0] if until else "times"
    body = step_spec.get("body") or []

    lines = []
    lines.append(
        f'    subgraph {step_id} ["loop: {_esc(step_id)} '
        f'(max_iterations: {_esc(max_iter)})"]'
    )

    cond_node = f"{step_id}_cond"
    lines.append(f'        {cond_node}{{"until: {_esc(until_kind)}"}}')

    body_nodes_defs = []
    body_map = {}
    prev_body_step = None

    for b in body:
        b_id = b.get("id")
        b_def, b_entry, b_exit = render_step(b, ctx, depth + 1)
        body_nodes_defs.append(b_def)
        body_map[b_id] = (b_entry, b_exit)

        # Connect internal loop steps
        b_needs = b.get("needs") or []
        for dep in b_needs:
            if dep in body_map:
                lines.append(f"        {body_map[dep][1]} --> {b_entry}")
        if not b_needs and prev_body_step:
            lines.append(f"        {body_map[prev_body_step][1]} --> {b_entry}")
        prev_body_step = b_id

    for b_def in body_nodes_defs:
        indented = "\n".join(f"        {line.strip()}" for line in b_def.splitlines() if line.strip())
        lines.append(indented)

    if body:
        first_body_id = body[0].get("id")
        last_body_id = body[-1].get("id")

        lines.append(f"        {cond_node} -- Exec body --> {body_map[first_body_id][0]}")
        lines.append(f"        {body_map[last_body_id][1]} -- Repeat --> {cond_node}")

    lines.append('    end')

    return "\n".join(lines), cond_node, cond_node


def render_step(step: dict[str, Any], ctx: dict[str, Any],
                depth: int = 1) -> tuple[str, str, str]:
    step_id = step.get("id")
    kind = step.get("kind")
    if kind == "agent":
        agent_spec = step.get("agent") or step
        return render_agent(step_id, agent_spec, ctx)
    elif kind == "supervisor":
        return render_supervisor(step_id, step, ctx)
    elif kind == "parallel":
        return render_parallel(step_id, step, ctx)
    elif kind == "pipeline":
        return render_pipeline(step_id, step, ctx)
    elif kind == "loop":
        return render_loop(step_id, step, ctx, depth)
    else:
        return f'    {step_id}["{_text(step_id, ctx)} (unknown: {_esc(kind)})"]', step_id, step_id


def spec_to_mermaid(spec: dict[str, Any]) -> str:
    ctx = {"params": spec.get("params") or {}}
    lines = ["graph TD"]

    steps = spec.get("steps") or []
    step_defs = []
    step_map = {}
    prev_step_id = None

    for step in steps:
        step_id = step.get("id")
        if not step_id:
            continue

        node_def, entry_node, exit_node = render_step(step, ctx, depth=1)
        step_defs.append(node_def)
        step_map[step_id] = (entry_node, exit_node)

        # Connect to dependencies
        needs = step.get("needs") or []
        for dep in needs:
            if dep in step_map:
                lines.append(f"    {step_map[dep][1]} --> {entry_node}")

        # Sequential fallback if no explicit needs and not first step
        if not needs and prev_step_id:
            lines.append(f"    {step_map[prev_step_id][1]} --> {entry_node}")

        prev_step_id = step_id

    for sd in step_defs:
        lines.append(sd)

    return "\n".join(lines)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <title>iworkflow Visualizer</title>
  <script type="module">
    import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
    mermaid.initialize({ startOnLoad: true });
  </script>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      margin: 20px;
      background-color: #f8f9fa;
      color: #212529;
    }
    .container {
      max-width: 1000px;
      margin: 0 auto;
      background: #ffffff;
      padding: 30px;
      border-radius: 8px;
      box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
    }
    h1 {
      font-size: 24px;
      margin-bottom: 5px;
    }
    .description {
      color: #6c757d;
      margin-bottom: 30px;
    }
    .mermaid {
      display: flex;
      justify-content: center;
      margin-top: 20px;
    }
  </style>
</head>
<body>
  <div class="container">
    <h1 id="wf-title">Workflow Graph</h1>
    <p class="description" id="wf-desc">Cargando detalles...</p>
    <div class="mermaid">
<!-- GRAPH_SOURCE -->
    </div>
  </div>
  <script>
    const name = <!-- WF_NAME -->;
    const desc = <!-- WF_DESC -->;
    if (name) document.getElementById("wf-title").innerText = name;
    if (desc) document.getElementById("wf-desc").innerText = desc;
  </script>
</body>
</html>
"""


def spec_to_html(spec: dict[str, Any]) -> str:
    mermaid_code = spec_to_mermaid(spec)
    name = spec.get("name", "Workflow Graph")
    desc = spec.get("description", "")

    html = HTML_TEMPLATE
    html = html.replace("<!-- GRAPH_SOURCE -->", mermaid_code)
    html = html.replace("<!-- WF_NAME -->", json.dumps(name))
    html = html.replace("<!-- WF_DESC -->", json.dumps(desc))
    return html
