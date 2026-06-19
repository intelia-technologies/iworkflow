## 1. Parseo del kind `checkpoint`

- [x] 1.1 Añadir `"checkpoint"` a `_VALID_KINDS` (`iworkflow/workflow.py:457`).
- [x] 1.2 Extender `_parse_step` para un `Step` de tipo checkpoint con campos: `prompt`/`title` (str), `artifact` (str opcional, ruta a fichero a mostrar), `schema` (dict | nombre de schema registrado, opcional), `output` (str, ruta del fichero de resolución), `mode` (`approval` | `input` | `confirm`, default `approval`).
- [x] 1.3 Validar en parseo: `mode` válido; si `mode == "input"` entonces `schema` es obligatorio; `output` obligatorio salvo modo atendido puro.

## 2. Estado `PAUSED` y payload de petición pendiente

- [x] 2.1 Añadir excepción interna `_Pause(step_id, request)` análoga a `_Abort`, y capturarla en `_Executor.run` para fijar `status="PAUSED"` (sin tratarla como error).
- [x] 2.2 El bundle de resultado en pausa incluye `pending_input = {step_id, prompt, artifact, schema, output}` además del `status`.
- [x] 2.3 Emitir evento `checkpoint_pending` al ledger/eventos vía `runner._emit` con los mismos campos.

## 3. Ejecución del checkpoint (`_exec_checkpoint`)

- [x] 3.1 Orden de resolución: (a) si `output` existe y (con `schema`) valida → resolver; (b) si hay resolvedor interactivo inyectado → obtener resolución en línea; (c) si no hay ninguno y el run es desatendido → `_Pause`.
- [x] 3.2 Al resolver, inyectar la resolución en `ctx["steps"][step.id]` como `{"value": <resolución>, "ok": True}` y persistir vía `_persist_steps` (resume-safe).
- [x] 3.3 Validar la resolución con `minijsonschema.validate` cuando hay `schema`; si no valida, NO avanzar: re-pausar con un error de validación en el payload (no consumir input corrupto).
- [x] 3.4 `mode: "confirm"`: avanzar solo ante resolución afirmativa explícita (p.ej. `{"approved": true}` / `"go"`); ausencia/ambigüedad/negación → no avanzar (pausa o abort según config). Ambiguo NUNCA es un go.

## 4. Reanudación determinista

- [x] 4.1 Verificar que un checkpoint resuelto en una corrida previa queda en `_completed`/`wf-steps.json` y se salta en el resume igual que cualquier paso journaled.
- [x] 4.2 Garantizar que los pasos con `needs` sobre un checkpoint no ejecutan hasta que el checkpoint resuelve (orden DAG ya existente; cubrir con test).

## 5. Resolvedor interactivo (modo atendido)

- [x] 5.1 Definir un hook de resolvedor en `Runner`/`_Executor` (callable opcional) que, dado el `pending_input`, devuelve la resolución; la ausencia de hook = modo desatendido (pausa).
- [x] 5.2 CLI: flag `--interactive` que conecta un resolvedor de consola (muestra `prompt`+`artifact`, lee la entrada/decisión). Sin el flag, comportamiento desatendido por defecto.

## 6. Superficies (CLI / MCP / dashboard)

- [x] 6.1 `iworkflow status` y `mcp_server` (`workflow_poll`/status) reportan `PAUSED` con el `pending_input`, distinto de `error`/`aborted`.
- [x] 6.2 Dashboard (`dashboard.py`): estado visual `PAUSED`/`WAITING_INPUT` en el header y el nodo del checkpoint (color distinto de error), mostrando qué gate espera input.

## 7. Tests (FakeProvider + resolvedor simulado)

- [x] 7.1 Desatendido sin resolución → run termina con `status="PAUSED"` y `pending_input` correcto; el evento `checkpoint_pending` queda en `events.jsonl`.
- [x] 7.2 Resume: tras escribir un `output` válido, relanzar → el checkpoint resuelve, su valor entra en `ctx` y los pasos posteriores se ejecutan; `status="DONE"`.
- [x] 7.3 `schema` inválido en `output` → sigue pausado con error de validación; no avanza.
- [x] 7.4 Modo atendido: resolvedor inyectado → el checkpoint resuelve en línea sin relanzar; el run llega a `DONE` en una sola corrida.
- [x] 7.5 `mode: "confirm"`: resolución ambigua/negativa no avanza; afirmativa avanza.
- [x] 7.6 Superficie MCP/status: un run pausado expone `PAUSED` + petición pendiente.

## 8. Docs y contrato

- [x] 8.1 `specs/iworkflow.openspec.json`: documentar el kind `checkpoint` (campos) y el estado `PAUSED`.
- [x] 8.2 `docs/USING_IWORKFLOW.md`: sección de gates humanos (desatendido vs atendido), con el ejemplo de los 3 gates de `review-client-v4`.
