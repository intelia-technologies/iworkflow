## ADDED Requirements

### Requirement: Parseo de `when` en pasos no-supervisor

Todo paso top-level de kind `agent`, `parallel`, `pipeline`, `loop` o `command` SHALL aceptar un campo opcional `when` con la misma estructura de predicado DSL que `kind==supervisor`. El motor SHALL invocar `_validate_when` sobre el valor al parsear el step (en `_parse_step`), antes de retornar el `Step`, y SHALL propagar `WorkflowError` si el predicado está mal formado. Un step sin `when` SHALL comportarse exactamente igual que hasta ahora (sin cambio de semántica observable).

#### Scenario: predicado bien formado en paso agent

- **WHEN** una spec contiene un `kind: agent` con `when: {path: "steps.gate.value.ok", truthy: true}`
- **THEN** `_parse_step` completa sin error y `step.when` contiene el dict del predicado

#### Scenario: predicado mal formado en paso command

- **WHEN** una spec contiene un `kind: command` con `when: {path: "steps.x.value", unknown_op: 1}`
- **THEN** `_parse_step` lanza `WorkflowError` con mensaje que incluye la lista de operadores válidos

#### Scenario: paso sin when no cambia

- **WHEN** una spec contiene un `kind: parallel` sin campo `when`
- **THEN** `step.when is None` y el comportamiento de ejecución es idéntico al anterior

---

### Requirement: Evaluación del predicado y skip determinista

El executor SHALL evaluar `step.when` contra `self.ctx` (estado acumulado del run: `ctx["steps"]`, `ctx["params"]`) mediante `_eval_when` antes de despachar `_exec_step`. Si `_eval_when` retorna `False`, el step SHALL ser saltado de forma determinista: el executor SHALL registrar un resultado `{"skipped": True, "ok": True, "kind": step.kind}` en `self.ctx["steps"][step.id]` y `self._completed[step.id]`, SHALL llamar a `_persist_steps()` para durabilidad, y SHALL emitir un evento `skipped` vía `runner._emit`. El step saltado NO SHALL invocar ningún provider, CLI ni subproceso. Si `_eval_when` retorna `True` o `step.when is None`, el comportamiento es el habitual.

#### Scenario: step agent saltado cuando predicado es falso

- **WHEN** el step `audit` tiene `when: {path: "steps.build.value.exit_code", eq: 0}` y `steps.build.value.exit_code` es `1`
- **THEN** el executor no llama a ningún provider para `audit`, `ctx["steps"]["audit"]["skipped"]` es `True`, y `wf-steps.json` contiene la entrada `audit` con `skipped: true`

#### Scenario: step command ejecutado cuando predicado es verdadero

- **WHEN** el step `deploy` tiene `when: {path: "steps.tests.value.exit_code", eq: 0}` y `steps.tests.value.exit_code` es `0`
- **THEN** el executor despacha `_exec_command` para `deploy` y `ctx["steps"]["deploy"]["skipped"]` no existe

#### Scenario: evento skipped emitido a events.jsonl

- **WHEN** un step con `when` falso es saltado
- **THEN** `events.jsonl` contiene una línea JSON con `event: "skipped"` y el `label` del step saltado

---

### Requirement: Paso saltado satisface dependencias de pasos posteriores

Un step saltado (con `when` falso) SHALL ser considerado completado a efectos de resolución de `needs`. Los steps que declaran `needs: [<step_saltado>]` SHALL ejecutarse normalmente una vez el step saltado haya registrado su resultado en `ctx["steps"]`. Un step dependiente NO SHALL heredar automáticamente el estado skipped de su upstream; su propio `when` (si lo tiene) se evalúa de forma independiente. El resultado `{"skipped": True, "ok": True}` de un upstream saltado SHALL estar disponible en `ctx["steps"].<id>` para que predicados downstream puedan inspeccionarlo si lo desean.

#### Scenario: dependiente se ejecuta tras upstream saltado

- **WHEN** el step `report` tiene `needs: ["audit"]` y el step `audit` es saltado por su `when` falso
- **THEN** `report` se ejecuta normalmente (su tarea asyncio no queda bloqueada) y recibe en `ctx["steps"]["audit"]` el resultado `{"skipped": True, "ok": True, "kind": "agent"}`

#### Scenario: predicado downstream puede inspeccionar skip de upstream

- **WHEN** el step `notify` tiene `when: {path: "steps.audit.skipped", truthy: true}` y `audit` fue saltado
- **THEN** `_eval_when` para `notify` evalúa `True` y `notify` se ejecuta

#### Scenario: resume reconoce paso skipped como completado

- **WHEN** un run se interrumpe tras saltar el step `lint`, y se reinicia con el mismo `run_id`
- **THEN** `_load_steps` restaura `lint` con `skipped: True` en `self._completed`, y el executor no re-evalúa ni re-salta `lint` en el segundo run

---

### Requirement: Cero LLM en la ruta de decisión del routing

La evaluación de `when` SHALL ejecutarse exclusivamente mediante `_eval_when` (código Python puro, sin IO ni llamadas a providers). El executor SHALL garantizar que ningún provider, CLI de suscripción ni subprocess es invocado como parte de la evaluación del predicado `when`. El DSL `when` opera únicamente sobre datos ya presentes en `ctx` (resultados de pasos anteriores, parámetros del workflow), sin efectos secundarios.

#### Scenario: predicado evaluado sin llamada a provider

- **WHEN** `_eval_when` es llamado para evaluar `when: {path: "steps.a.value.score", gte: 0.8}` sobre un ctx real
- **THEN** la función retorna `True` o `False` sin ninguna operación de IO, subprocess ni llamada a `runner.agent()`

#### Scenario: routing determinista con misma entrada produce mismo resultado

- **WHEN** el mismo ctx con `steps.a.value.score = 0.9` se pasa a `_eval_when` en dos ejecuciones independientes
- **THEN** ambas retornan `True` sin varianza
