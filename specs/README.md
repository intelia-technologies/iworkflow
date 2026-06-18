## iworkflow OpenSpec (planner/LLM reference)

`iworkflow.openspec.json` codifies the public contract for plans that the engine can execute over CLI/MCP/SDK. A planner (LLM) can emit a `WorkflowSpec` that iworkflow will run deterministically with the subscription CLIs (codex, gemini, claude).

### How a planner should use it
1) Generate a `WorkflowSpec` conforming to the spec:
   - Step kinds: `agent`, `parallel`, `pipeline`, `loop`, `supervisor`.
   - Templating: `{{params.*}}`, `{{steps.<id>.value.*}}`, `{{loop.collected}}`, `{{loop.decision.*}}`, `{{item}}`, `{{prev}}`, `{{supervisor.steps}}`.
   - Optional `execution` hints at workflow level: `worktree` (`current` / `new:<name>` / `existing:<name>`), `branch` (`reuse` / `none` / `new:<name>`), `checkout` (bool), `gh_required` (bool).
   - Optional `instructions` per agent: `run_in_worktree`, `branch`, `gh` (command hints for GitHub CLI).
2) Run it:
   ```bash
   # Blocking run
   iworkflow run --spec plan.json --params '{"q":"..."}'

   # Async pattern (MCP/CLI-compatible)
   start=$(iworkflow run --spec plan.json --params '{"q":"..."}' --json)  # or workflows.start via MCP
   # then stream/poll events with run_id
   ```
3) Visualize:
   ```bash
   iworkflow graph --spec plan.json --html /tmp/plan.html  # writes HTML (and opens)
   iworkflow graph --spec plan.json --mermaid              # raw mermaid
   ```

### Key types (short)
- `WorkflowSpec`: `steps` (required), optional `params`, `schemas`, `output`, `artifacts`, `execution`.
- `Step`:
  - `agent`: `prompt`, optional `schema`, `prefer`, `role`, `model`, `sandbox`, `tools`, `gate`, `required`, `instructions`.
  - `parallel`: `agents` (array of AgentSpec).
  - `pipeline`: `items` (list or template), `stages` (AgentSpec list).
  - `loop`: `body` (steps), `until` (times/count/dry/budget/agent/vote), `max_iterations`, optional `collect`.
  - `supervisor`: `supervisor` (AgentSpec), optional `watch`, `when`.
- `WorkflowRunBundle`: `{status, name?, output?, steps, aborted_at?, run_id?}`.

### Notes
- The engine still applies its own safety policy (`Limits`) unless you explicitly widen it in a trusted caller (CLI/SDK). For now you can omit `limits` entirely.
- `execution` and `instructions` are hints for how sub-agents should operate (worktree/branch/gh); the engine does not enforce git/gh operations.

See the JSON spec for full fields and examples (including `complex_security_audit` and a loop with decider).***
[specs/README.md#D307]
1:# iworkflow OpenSpec
2:
3:Esta carpeta contiene el contrato OpenSpec para iworkflow (`iworkflow.openspec.json`).
4:
5:## Uso rápido
6:- Genera un plan (WorkflowSpec) conforme al OpenSpec. El plan puede venir de un LLM planner.
7:- Ejecuta con la CLI/MCP/SDK:
8:  ```bash
9:  iworkflow run --spec plan.json --params '{"foo":"bar"}'
10:  ```
11:- Para flujos largos: `workflows.start` + `workflows.stream`/`workflows.poll`.
12:- `graph.generate` permite renderizar el spec en Mermaid/HTML (y publicar si se desea).
13:
14:## Campos clave para planificación
15:- `WorkflowSpec.execution`: hints opcionales de planificación: `worktree` (current/new:<name>/existing:<name>), `branch` (reuse/new:<name>/none), `gh_required` (bool), `checkout` (bool).
16:- `AgentSpec.instructions`: hints libres para subagentes (ej. `run_in_worktree`, `branch`, comandos `gh`).
17:- `AgentSpec.prefer`/`model`/`role`: guían el router hacia el proveedor adecuado.
18:
19:## Límites
20:- `Limits` es opcional; si no se pasa, se usan los defaults del motor. Úsalo solo si quieres endurecer o ampliar la política desde una llamada de confianza.
21:
22:## Ejemplos
23:- `examples.recipe.complex_security_audit`: receta registrada.
24:- `examples.dynamic.loop.decider`: spec dinámico con loop decidido por un agente.
25: