## 1. Parsing — extender `_parse_step` para todos los kinds

- [ ] 1.1 En `iworkflow/workflow.py`, `_parse_step`: mover la lectura de `d.get("when")` y la llamada a `_validate_when` fuera de la rama `elif kind == "supervisor"`, al bloque final (después de todos los `if/elif kind == ...`), para que aplique a cualquier kind top-level.
- [ ] 1.2 Verificar que `Step.when` (campo ya existente en el dataclass, línea ~284) no requiere cambios de tipo.
- [ ] 1.3 Asegurar que `_validate_when` lanza `WorkflowError` propagable correctamente cuando el predicado está mal formado (depth > 8, operadores inválidos).

## 2. Ejecución — skip determinista en `_Executor.run`

- [ ] 2.1 En `_Executor.run` / `run_step_with_deps` (inline closure, líneas ~659-673): antes de la comprobación de `step.id in self._completed`, evaluar `step.when` con `_eval_when(step.when, self.ctx)` si `step.when is not None`.
- [ ] 2.2 Si el predicado devuelve `False`, producir el resultado skipped: `{"skipped": True, "ok": True, "kind": step.kind}`, asignarlo a `self.ctx["steps"][step.id]` y `self._completed[step.id]`, llamar a `self._persist_steps()`.
- [ ] 2.3 Emitir evento `skipped` vía `self.runner._emit(step.id, "skipped", kind=step.kind, when=step.when)` antes de retornar.
- [ ] 2.4 Garantizar que la closure `run_step_with_deps` retorna sin error cuando el paso es saltado, de modo que los `tasks` dependientes (`needs`) puedan continuar normalmente.

## 3. Semántica de dependencias con pasos saltados

- [ ] 3.1 Confirmar que `run_step_with_deps` espera `tasks[dep]` antes de evaluar `step.when`; así un paso condicional puede referenciar en su predicado el output de un paso anterior.
- [ ] 3.2 Documentar (comentario en código) que un dependiente NO hereda automáticamente el estado skipped de su upstream; solo hereda si su propio `when` lo determina.

## 4. Tests (FakeProvider / unit)

- [ ] 4.1 En `tests/test_workflow.py`, añadir caso: step `agent` con `when: {path: "steps.gate.value.ok", truthy: true}` — cuando `gate` devuelve ok=true, el step ejecuta; cuando devuelve ok=false, el step queda skipped.
- [ ] 4.2 Añadir caso: step `command` con `when: {path: "steps.prev.value.exit_code", eq: 0}` — verifica skip cuando exit_code != 0.
- [ ] 4.3 Añadir caso: paso saltado no bloquea a un dependiente (via `needs`) que no tiene `when`.
- [ ] 4.4 Añadir caso: `when` con predicado `all`/`any` compuesto sobre múltiples steps previos.
- [ ] 4.5 Añadir caso: spec con `when` malformado en paso `agent` → `WorkflowError` en parse.
- [ ] 4.6 Verificar resume: paso skipped persiste en `wf-steps.json` y se restaura correctamente en un segundo run con mismo `run_id`.

## 5. Docs y specs

- [ ] 5.1 Actualizar `specs/iworkflow.openspec.json`: añadir `"when"` al schema de step (tipo `object`, opcional, aplica a todos los kinds).
- [ ] 5.2 Actualizar `docs/USING_IWORKFLOW.md`: añadir sección "Conditional Routing" que explique el DSL `when`, operadores disponibles (`_LEAF_OPS`), composición `all`/`any`/`not`, rutas de contexto disponibles (`steps.<id>.value.*`, `params.*`), y al menos un ejemplo completo de spec con skip condicional.
