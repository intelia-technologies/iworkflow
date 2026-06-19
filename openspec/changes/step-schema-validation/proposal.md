## Why

`AgentSpec.schema` existe desde el primer día: el scheduler lo pasa al provider (`Runner.agent` → `prov.run(schema=…)`) y los providers nativos (Codex, ClaudeInteractive) lo usan para forzar formato de salida. Sin embargo, el motor (`_Executor._exec_agent`, `workflow.py:1014-1036`) **nunca revalida `res.value` contra el schema** una vez que el provider devuelve. La validación es responsabilidad _optativa_ de cada provider, y providers que no soportan schema nativo (GeminiProvider parsea un bloque JSON libre) o que producen salida malformada propagan `res.value` corrompido a `ctx["steps"]` sin ningún control. El `gate` (campo `abort_on`) solo mira un único campo escalar; no verifica la estructura completa del objeto.

El resultado: un paso que depende de `{{steps.prev.value.field}}` puede fallar con un error de plantilla opaco, o — peor — continuar silenciosamente con datos incorrectos, sin que el motor sepa cuándo ocurrió el fallo ni pueda actuar (failover, marcar required=false, etc.).

## What Changes

- **`_Executor._exec_agent`** (y los análogos en `_exec_parallel` / `_exec_pipeline` para los subpasos): tras obtener `res` exitoso (`res.ok`) y cuando `a.schema` está definido, llama a `minijsonschema.validate(res.value, schema)` (con fallback a `jsonschema` si está disponible). Si falla: re-intenta el proveedor siguiente via `_agent_call` redispatch (si quedan targets); si se agotan todos, aplica la semántica `required` existente (`WorkflowError` vs `null`).
- **`Runner._emit`**: nuevo evento `schema_mismatch` con campos `label`, `provider`, `why` para trazabilidad.
- **`Runner._record`**: el `AgentResult` resultante lleva `schema_ok: bool` para que el ledger refleje si el valor fue validado.
- Providers y specs **sin** schema: comportamiento idéntico al actual (sin regresión).

## Capabilities

### New Capabilities

- `schema-validation`: Validación motor-side de `res.value` contra `AgentSpec.schema` antes de marcar el paso DONE; failover por mismatch; evento `schema_mismatch`; degradación graceful cuando `required=false`.

### Modified Capabilities

- (ninguno)

## Impact

- **Rotura de specs existentes**: ninguna. Solo actúa cuando `AgentSpec.schema != None`, que ya implicaba expectativa de estructura.
- **Providers afectados**: todos por igual; GeminiProvider (sin schema nativo) es el beneficiario principal porque hoy puede devolver JSON malformado sin detección.
- **Rendimiento**: la validación es síncrona y O(campos); coste despreciable frente a la latencia del CLI.
- **Tests**: se añaden casos con `FakeProvider` / `ScriptedProvider` que devuelven valores inválidos para cobrir los tres caminos (ok, mismatch+failover, mismatch+exhausted).

## Grounding: PydanticAI

- PydanticAI usa el `output_type` para construir un JSON Schema y validar los datos estructurados devueltos por el modelo antes de exponer `result.output`; esa separación entre "pedir formato" y "validar el dato recibido" es el patrón que esta change aplica en el motor. Fuente: <https://pydantic.dev/docs/ai/core-concepts/output/#structured-output-data>.
- En el camino de JSON Schema personalizado, PydanticAI advierte que el objeto JSON recibido no se valida automáticamente y recomienda validadores de output para rechazar respuestas inválidas/reintentar; esto justifica que iworkflow no delegue la seguridad del contrato al prompt o al provider. Fuente: <https://pydantic.dev/docs/ai/core-concepts/output/#custom-json-schema>.
