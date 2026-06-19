## Why

El motor de `iworkflow` está construido para ejecución **desatendida (headless)**: el control de flujo es 100% determinista y no hay forma de pausar para que un humano apruebe, aporte datos y luego reanudar en el mismo flujo. Esto bloquea casos reales de "human-in-the-loop" como el skill `review-client-v4` (revisión semanal de cliente), cuyo diseño exige **3 gates de aprobación humana** (triage→`decisions.json`, draft approve/edit/abort, send con confirmación explícita) y cuya doctrina es *"writes happen only after a gate"* y *"Run supervised only"*.

Estado actual verificado en el código:

- Los kinds válidos son `_VALID_KINDS = {"agent","parallel","pipeline","loop","supervisor","command"}` (`iworkflow/workflow.py:457`). No existe un kind de pausa/aprobación/input.
- El campo `gate` de un paso es **solo aborto determinista**: `_exec_agent` (`workflow.py:1279-1283`) y `_exec_command` (`workflow.py:1252-1258`) comparan `abort_on` y lanzan `_Abort`, que pone el run en estado `ABORTED` (`workflow.py:638-845`). No pausa ni captura input.
- No existe ninguna primitiva tipo `AskUserQuestion`/`wait_for_input`/`approval`/`paused` en `iworkflow/`. El `supervisor` decide por agente, no por humano.
- El único patrón hoy para "esperar a un humano" es un workaround: hacer que un `command` aborte si falta un fichero, que el copiloto lo escriba a mano y relance `iworkflow run --run-id` para reanudar vía el journal (`wf-steps.json` + `_completed`). Funciona, pero el run queda en estado `ABORTED`/`ERROR` (no `PAUSED`), no captura input estructurado y no valida su forma.

Mapeo verificado: la **columna vertebral** de `review-client-v4` SÍ encaja hoy (scripts como `command`; anotadores en `pipeline`/`parallel`; contratos con la validación de `schema`; orden con `needs`). El **único bloqueo real** son los gates humanos interactivos.

Fuera de alcance (gap secundario, no bloqueante): un `kind: "workflow"` para sub-workflows nativos en memoria; hoy se cubre invocando el CLI desde un `command`.

## What Changes

- Nuevo kind de paso **`checkpoint`**: un punto de aprobación humana declarativo con `prompt`/`title`, `artifact` opcional (ruta a un HTML/fichero renderizado a mostrar), `schema` opcional (forma del input estructurado esperado), `output` (fichero donde se lee/escribe la resolución) y `mode` (`approval` | `input` | `confirm`).
- Nuevo estado de run de primera clase **`PAUSED`** (distinto de `DONE`/`ABORTED`/`ERROR`): cuando un `checkpoint` no tiene resolución y el run es desatendido, el motor se detiene limpiamente con un payload `pending_input` (`{step_id, prompt, artifact, schema, output}`) y emite un evento `checkpoint_pending`.
- **Reanudación desde resolución**: al relanzar con el mismo `run_id`, un `checkpoint` cuyo `output` ya existe y valida contra `schema` se completa de forma determinista, inyecta la resolución en `ctx.steps.<id>.value` y el run continúa. Reutiliza la maquinaria de resume existente (`_completed`/`wf-steps.json`).
- **Captura validada**: si hay `schema`, la resolución se valida con `minijsonschema.validate`; un input mal formado mantiene el checkpoint pendiente con un error de validación en lugar de avanzar con datos corruptos.
- **Resolución interactiva (modo atendido)**: si el caller provee un resolvedor (prompt de consola con `--interactive`, o un canal de aprobación de MCP/dashboard), el checkpoint bloquea en línea esperando la resolución y continúa en el mismo proceso sin necesidad de relanzar.
- **Semántica de confirmación afirmativa**: un checkpoint `mode: "confirm"` solo avanza ante una resolución afirmativa explícita; ausencia/ambigüedad/negación no avanza (mapea el Gate 3: *"an ambiguous reply is NOT a go"*).
- CLI, MCP y dashboard exponen el estado `PAUSED`/`WAITING_INPUT` y la petición pendiente, diferenciado de los estados de error.

## Capabilities

### New Capabilities

- `interactive-gates`: el motor soporta gates de aprobación humana (human-in-the-loop) que pausan el run con estado de primera clase, capturan input humano validado por schema y reanudan de forma determinista y resume-safe, tanto en modo desatendido (pausa+relance) como atendido (resolución en línea).

### Modified Capabilities

- (ninguno — el contrato existente de WorkflowSpec/AgentSpec y el campo `gate` de aborto determinista no cambian; `checkpoint` es aditivo)

## Impact

- **Código**: `iworkflow/workflow.py` (`_VALID_KINDS`, `_parse_step`, nuevo `_exec_checkpoint`, estado `PAUSED` en `_Executor.run`, payload `pending_input`), `iworkflow/minijsonschema.py` (validación reutilizada), `iworkflow/scheduler.py` (emisión de evento `checkpoint_pending`; hook de resolvedor interactivo).
- **Superficies**: `iworkflow/cli.py` (flag `--interactive`, `status` muestra `PAUSED`), `iworkflow/mcp_server.py` (estado `PAUSED` + petición pendiente en `workflow_poll`/`status`), `iworkflow/dashboard.py` (estado visual `PAUSED`/`WAITING_INPUT`).
- **Contrato/docs**: `specs/iworkflow.openspec.json` (nuevo kind `checkpoint`, estado `PAUSED`), `docs/USING_IWORKFLOW.md`.
- **Tests**: `tests/test_workflow.py` (pausa/resume/validación/confirm con `FakeProvider` y resolvedor simulado), `tests/test_mcp_server.py` (superficie `PAUSED`).
- **Reglas duras respetadas**: el input humano es DATO inyectado en `ctx`, el control de flujo sigue siendo determinista (cero LLM en el router); core stdlib-only; subscription-only intacto.
- **Habilita**: construir `review-client-v4` de Intelia y cualquier flujo supervisado (los 3 gates) sobre `iworkflow`.
