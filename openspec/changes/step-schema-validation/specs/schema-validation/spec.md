## ADDED Requirements

### Requirement: Validación post-proveedor obligatoria

Cuando `AgentSpec.schema` esté definido y el proveedor devuelva `res.ok == True`, el motor SHALL llamar a `minijsonschema.validate(res.value, schema)` (o a `jsonschema.validate` si el paquete está disponible) antes de construir `out` con `_result(res, …)` y antes de persistir el paso como DONE. Esta validación ocurre en `_Executor._exec_agent` (`workflow.py`) inmediatamente después de `_agent_call` y antes de `_write_guard_needed` / `gate`. Un resultado `res.ok == False` (proveedor agotado) no dispara validación.

#### Scenario: schema válido pasa sin efecto secundario

- **WHEN** un paso `kind=agent` tiene `schema: {type: object, required: [verdict]}` y el proveedor devuelve `{"verdict": "PASS"}`
- **THEN** `minijsonschema.validate` retorna `(True, "")` y el paso se marca DONE con `value = {"verdict": "PASS"}`
- **AND** no se emite evento `schema_mismatch`

#### Scenario: ausencia de schema no activa validación

- **WHEN** un paso `kind=agent` no tiene `schema` definido (`AgentSpec.schema is None`)
- **THEN** el motor NO llama a `minijsonschema.validate` y el paso se marca DONE con el valor devuelto por el proveedor sin modificación

---

### Requirement: Fallo de validación dispara failover entre proveedores

Cuando `minijsonschema.validate` retorna `(False, why)`, el motor SHALL tratar el resultado como si el proveedor hubiese fallado (equivalente a `status=EXHAUSTED` para ese proveedor) y MUST intentar el siguiente proveedor en la lista de targets (orden determinado por `route()` o `prefer`). Si un proveedor posterior devuelve un valor que sí supera la validación, el paso se marca DONE con ese valor. El motor SHALL emitir el evento `schema_mismatch` con campos `{label, provider, why}` en `events.jsonl` por cada intento fallido de validación.

#### Scenario: failover exitoso tras mismatch

- **WHEN** un paso tiene `schema: {type: object, required: [score]}` y el proveedor A devuelve `{"result": "ok"}` (falta `score`)
- **AND** el proveedor B devuelve `{"score": 0.9}`
- **THEN** el motor emite `schema_mismatch` para el intento con proveedor A
- **AND** el paso se marca DONE con `value = {"score": 0.9}` y `provider = B`

#### Scenario: evento schema_mismatch contiene diagnóstico

- **WHEN** la validación falla con `why = "missing required key: 'score'"`
- **THEN** el evento `schema_mismatch` en `events.jsonl` contiene los campos `label`, `provider` (nombre del proveedor que devolvió el valor), y `why` con el mensaje exacto de `minijsonschema.validate`

---

### Requirement: Semántica required tras agotamiento por mismatch

Si todos los proveedores disponibles devuelven valores que no superan `minijsonschema.validate`, el motor SHALL aplicar la semántica existente del campo `AgentSpec.required`:

- Si `required=True` (valor por defecto): lanzar `WorkflowError` con mensaje que incluya el label del paso y la causa del último mismatch.
- Si `required=False`: continuar con `value=None` en `ctx["steps"][step_id]`, marcando `ok=False`; no lanzar excepción.

Esta semántica es idéntica a la que aplica cuando todos los proveedores fallan por error/timeout (`_exec_agent` línea 1022-1026), extendida al caso de agotamiento por mismatch de schema.

#### Scenario: required=True agotado por mismatch lanza WorkflowError

- **WHEN** un paso tiene `required=True`, `schema: {type: object, required: [verdict]}`, y el único proveedor disponible devuelve `{"status": "ok"}` (falta `verdict`)
- **THEN** el motor lanza `WorkflowError` con mensaje que referencia el label del paso y el diagnóstico del mismatch
- **AND** el evento `schema_mismatch` se emite antes de la excepción

#### Scenario: required=False agotado por mismatch continúa con null

- **WHEN** un paso tiene `required=False`, `schema: {type: object, required: [verdict]}`, y todos los proveedores devuelven valores sin `verdict`
- **THEN** el paso finaliza con `value=None` y `ok=False` en `ctx["steps"]`
- **AND** el workflow continúa ejecutando los pasos siguientes sin excepción

---

### Requirement: Compatibilidad con parallel y pipeline

En pasos `kind=parallel` y `kind=pipeline`, la validación SHA aplicarse **por subagente**: cada `AgentSpec` en `step.agents` / `step.stages` que tenga `schema` definido MUST validar su `res.value` individual. Un fallo de validación en un subagente se registra como `ok=False` en el elemento correspondiente del resultado compuesto; el resultado del paso paralelo/pipeline refleja `ok=False` agregado si algún subresultado falla (comportamiento análogo al actual para `res.ok` de cada subagente).

#### Scenario: parallel con un subagente inválido

- **WHEN** un paso `kind=parallel` tiene dos subagentes A y B, ambos con `schema: {type: object, required: [verdict]}`
- **AND** el proveedor devuelve `{"verdict": "PASS"}` para A y `{}` para B
- **THEN** el resultado del paso tiene `value[0].ok == True` y `value[1].ok == False`
- **AND** el paso paralelo tiene `ok == False` en su resultado agregado
- **AND** se emite `schema_mismatch` solo para el subagente B
