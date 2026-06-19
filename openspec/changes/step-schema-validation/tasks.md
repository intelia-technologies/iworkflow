# Tasks — step-schema-validation

## 1. Investigación y grounding

- [ ] 1.1 Leer `iworkflow/workflow.py` §`_exec_agent` (líneas 1014-1036) y §`_agent_call` (750-767) para confirmar la ausencia de revalidación post-provider.
- [ ] 1.2 Leer `iworkflow/minijsonschema.py` para confirmar la firma `validate(obj, schema) -> (ok, why)` y sus capacidades (required, enum, additionalProperties).
- [ ] 1.3 Leer `iworkflow/scheduler.py` §`Runner.agent` (123-273) para confirmar los puntos de emit (`_emit`) y el `AgentResult` dataclass.
- [ ] 1.4 Consultar docs de PydanticAI sobre `result_type` y validación post-respuesta para fundamentar el diseño (citar 2 ideas en `proposal.md`).

## 2. Implementación — motor

- [ ] 2.1 En `_Executor._exec_agent` (`workflow.py:1014`): tras `res = await self._agent_call(…)` y antes de `out = self._result(res, …)`, insertar llamada a `_validate_schema(res, a, label)` cuando `res.ok and a.schema is not None`.
- [ ] 2.2 Implementar `_Executor._validate_schema(res, a, label) -> AgentResult` (método privado):
  - Obtiene el schema resuelto via `self._schema(a.schema)`.
  - Llama a `minijsonschema.validate(res.value, schema)` (o `jsonschema.validate` si disponible).
  - Si `(ok, why)` es `(False, _)`: emite evento `schema_mismatch` via `self.runner._emit(label, "schema_mismatch", provider=res.provider, why=why)`.
  - Devuelve un `AgentResult` con `status="EXHAUSTED"` para que el caller trate el fallo como proveedor agotado (activando failover o `required` semántica).
- [ ] 2.3 En `_Executor._exec_agent`: si `_validate_schema` devuelve exhausted, aplicar la semántica `required` existente (línea 1022-1026) — `WorkflowError` si `required=True`, `null` si `required=False`.
- [ ] 2.4 En `_exec_parallel` y `_exec_pipeline`: aplicar validación individual por subagente cuando `a.schema` esté definido; un mismatch en un sub-paso se registra como `ok=False` en el resultado de ese sub-paso.

## 3. Implementación — trazabilidad

- [ ] 3.1 En `Runner._emit` (`scheduler.py`): documentar (comentario inline) el nuevo evento `schema_mismatch` con campos `label`, `provider`, `why`.
- [ ] 3.2 En `Runner._record`: añadir campo opcional `schema_ok: bool | None` a `LedgerRecord` (si schema definido: True/False; si no: None); persistir en el ledger JSONL.
- [ ] 3.3 Actualizar `LedgerRecord` en `iworkflow/ledger.py` con el campo `schema_ok`.

## 4. Tests (FakeProvider / unit)

- [ ] 4.1 `tests/test_workflow.py` — Scenario: schema definido, valor válido → paso DONE, no `schema_mismatch`.
- [ ] 4.2 `tests/test_workflow.py` — Scenario: schema definido, valor inválido (campo faltante), `required=True`, un solo provider → `WorkflowError`.
- [ ] 4.3 `tests/test_workflow.py` — Scenario: schema definido, primer provider devuelve valor inválido, segundo provider devuelve valor válido → paso DONE con segundo provider (failover por mismatch).
- [ ] 4.4 `tests/test_workflow.py` — Scenario: schema definido, valor inválido, `required=False` → paso con `value=None`, sin excepción.
- [ ] 4.5 `tests/test_workflow.py` — Scenario: sin schema (`AgentSpec.schema=None`) → comportamiento idéntico al actual (sin revalidación).
- [ ] 4.6 `tests/test_scheduler.py` — Verificar que evento `schema_mismatch` se emite al `events.jsonl` con los campos correctos.
- [ ] 4.7 `tests/test_workflow.py` — Scenario: `parallel` con uno de los subagentes con schema inválido → `ok=False` en ese subresultado.

## 5. Docs y contrato

- [ ] 5.1 Actualizar `specs/iworkflow.openspec.json`: añadir entrada para capability `schema-validation` con los campos nuevos (`schema_mismatch` event, `schema_ok` ledger field).
- [ ] 5.2 Actualizar `docs/USING_IWORKFLOW.md`: sección "Agent Schema" — aclarar que el motor valida `res.value` completo cuando `schema` está definido, describir comportamiento de failover y `required=false`.
