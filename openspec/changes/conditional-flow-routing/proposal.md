## Why

iworkflow ya posee una maquinaria de predicados `when` completa y reutilizable (`_validate_when` / `_eval_when` / `_LEAF_OPS` en `workflow.py` líneas ~157-239). Sin embargo, esa maquinaria es hoy **exclusiva de `kind==supervisor`** (documentada como "deviation guard", línea ~284; parseada solo en la rama supervisor ~476-479; evaluada solo en `_exec_supervisor` ~1085). Los pasos top-level de tipo `agent`, `parallel`, `pipeline`, `loop` y `command` no aceptan el campo `when`, por lo que no existe mecanismo para saltar un paso condicionalmente según los outputs acumulados en `ctx.steps.*` sin recurrir a un LLM que tome esa decisión.

Esto obliga a codificar toda la lógica de ramificación bien en el propio LLM (violando la regla de determinismo en control de flujo), bien en un supervisor auxiliar que consume tokens innecesarios para tomar una decisión que ya está implícita en los datos estructurados del run.

## What Changes

Se extiende el campo `when` para que sea parseable y honrado en **cualquier paso top-level** (`agent`, `parallel`, `pipeline`, `loop`, `command`):

1. **Parsing** (`_parse_step`): se llama `_validate_when` para todos los kinds en el bloque de parsing final, tras parsear el kind-specific payload. El `Step.when` (ya existe en el dataclass) se populará para cualquier kind.

2. **Ejecución** (`_Executor.run` / `run_step_with_deps`): antes de llamar a `_exec_step`, se evalúa `step.when` contra `self.ctx` con `_eval_when`. Si el predicado es falso, el paso se salta: se registra en `self._completed` y `self.ctx["steps"]` con un resultado `{"skipped": True, "ok": True, "kind": step.kind}`, se emite un evento `skipped` vía `self.runner._emit`, y se escribe a `wf-steps.json` via `_persist_steps()`.

3. **Dependencias** (`run_step_with_deps`): un paso saltado satisface las `needs` de sus dependientes como si hubiera terminado; un dependiente no salta automáticamente por tener una dependencia saltada.

4. **Sin cambios en `_eval_when` / `_validate_when`**: se reutilizan intactos. El DSL es el mismo.

## Capabilities

### New Capabilities

- `conditional-routing`: Enrutamiento condicional determinista en pasos top-level mediante predicados `when` declarativos evaluados contra el estado acumulado del run (`ctx.steps.*`, `ctx.params.*`). Sin LLM en la ruta de control.

### Modified Capabilities

- (ninguno)

## Impact

- **Aditivo**: solo se añaden rutas de código nuevas; el comportamiento de specs sin `when` en pasos no-supervisor es idéntico.
- **Breaking**: ninguno. El campo `when` en pasos no-supervisor era un campo desconocido (silenciosamente ignorado antes); ahora se valida y honra.
- **Compatibilidad de resume**: los pasos saltados se persisten en `wf-steps.json` igual que los ejecutados; un resume correcto los reconocerá como completados.
- **Tests**: se añaden casos al grupo FakeProvider en `tests/test_workflow.py`.
- **Docs**: se actualiza `docs/USING_IWORKFLOW.md` con la sección de conditional routing y `specs/iworkflow.openspec.json` con el campo `when` en el schema de step.
