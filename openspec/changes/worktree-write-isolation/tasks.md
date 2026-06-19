## 1. Diseño e infraestructura

- [ ] 1.1 Añadir helper `_is_git_repo(path: Path) -> bool` en `workflow.py` que ejecuta `git rev-parse --is-inside-work-tree` y devuelve False si el returncode es distinto de 0.
- [ ] 1.2 Añadir helper `_worktree_path(run_id: str, step_id: str, agent_id: str) -> Path` que construye la ruta temporal `<tempfile.gettempdir()>/iwf-<run_id>-<step_id>-<agent_id>` de forma reproducible y segura.
- [ ] 1.3 Añadir helper asíncrono `_create_worktree(base_cwd: Path, wt_path: Path, branch: str) -> None` que invoca `git worktree add --detach <wt_path>` dentro de `base_cwd` vía `asyncio.create_subprocess_exec`.
- [ ] 1.4 Añadir helper asíncrono `_remove_worktree(base_cwd: Path, wt_path: Path) -> None` que invoca `git worktree remove --force <wt_path>` con manejo de error no fatal (log warning si falla).
- [ ] 1.5 Añadir helper asíncrono `_consolidate_worktree(base_cwd: Path, wt_path: Path) -> None` que, tras éxito del agente, ejecuta `git -C <base_cwd> merge --squash <branch>` o equivalente cherry-pick del HEAD del worktree hacia la rama base, luego `git commit --no-edit` si hay cambios staged.

## 2. Lógica de aislamiento en `_exec_parallel`

- [ ] 2.1 Identificar agentes write-capable en el paso `parallel` mediante `_write_guard_needed(a)`.
- [ ] 2.2 Para cada agente write-capable, envolver `_agent_call` en una función auxiliar que: (a) verifica repo git, (b) crea worktree, (c) llama `runner.agent(…)` con `cwd=wt_path`, (d) en éxito valida `write_paths` dentro del worktree y consolida, (e) en fallo o excepción elimina el worktree y re-lanza.
- [ ] 2.3 Agentes read-only dentro del mismo paso `parallel` no se ven afectados; mantienen `default_cwd`.
- [ ] 2.4 Asegurar que la limpieza del worktree (`_remove_worktree`) se ejecuta en bloque `finally` para evitar worktrees huérfanos ante excepciones asíncronas.

## 3. Lógica de aislamiento en `_exec_pipeline`

- [ ] 3.1 Para cada stage write-capable, aplicar el mismo patrón que en 2.2: worktree por (step_id, stage_agent_id, item_index).
- [ ] 3.2 La consolidación se hace tras cada stage individual, no al final del pipeline completo, para que stages sucesivos vean los cambios ya integrados.
- [ ] 3.3 Agentes read-only dentro del pipeline mantienen `default_cwd`.

## 4. Integración con `_validate_write_paths`

- [ ] 4.1 Cuando se usa aislamiento por worktree, `_git_dirty_paths()` y `_validate_write_paths()` se ejecutan con `cwd=wt_path` (no el `default_cwd` global), de modo que el diff de suciedad es exclusivo del worktree del agente.
- [ ] 4.2 Preservar la semántica existente de `write_paths`: paths relativos se resuelven contra la raíz del worktree; paths absolutos se validan contra el `git_root` original.

## 5. Degradación y errores

- [ ] 5.1 Si `_is_git_repo(default_cwd)` devuelve False y el paso tiene agentes write-capable, lanzar `WorkflowError` con mensaje: `"worktree isolation required for write-capable agents in parallel/pipeline steps, but working directory is not a git repository: <path>"`.
- [ ] 5.2 Si `git worktree add` falla (p.ej. git < 2.5 o permisos), lanzar `WorkflowError` con el stderr del proceso.
- [ ] 5.3 Fallos de consolidación (`merge/cherry-pick`) se tratan como fallo del agente: `ok=False`, el worktree se elimina, y el paso falla con el mensaje de git.

## 6. Tests (FakeProvider/unit)

- [ ] 6.1 Test unitario de `_is_git_repo`: directorio git real → True; directorio temporal sin git → False.
- [ ] 6.2 Test de `_worktree_path`: verifica unicidad de rutas para distintas combinaciones (run_id, step_id, agent_id).
- [ ] 6.3 Test de `_exec_parallel` con dos agentes write-capable (FakeProvider): verifica que se crean dos worktrees distintos y que la consolidación se llama una vez por agente.
- [ ] 6.4 Test de degradación: `_exec_parallel` con agente write-capable en directorio no-git → `WorkflowError` con mensaje descriptivo.
- [ ] 6.5 Test de limpieza: worktree se elimina incluso si el agente falla (FakeProvider devuelve ok=False).
- [ ] 6.6 Test de `_exec_pipeline` con stage write-capable: verifica orden de consolidación (stage N consolidado antes de stage N+1).
- [ ] 6.7 Test de no-regresión: agente `read-only` dentro de un paso `parallel` no crea worktree (mock de `_create_worktree` no debe invocarse).

## 7. Docs

- [ ] 7.1 Actualizar `specs/iworkflow.openspec.json`: añadir capability `worktree-isolation` con descripción, campo `requires_git: true`, campo `applies_to: [parallel, pipeline]`.
- [ ] 7.2 Actualizar `docs/USING_IWORKFLOW.md`: añadir sección "Write isolation with git worktrees" explicando cuándo se activa (agentes write-capable en parallel/pipeline), requisitos (git >= 2.5, directorio git), comportamiento de degradación, y ejemplo de spec con `sandbox: write` dentro de `parallel`.
