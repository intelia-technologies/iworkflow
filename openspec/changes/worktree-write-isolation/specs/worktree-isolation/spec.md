## ADDED Requirements

### Requirement: Creación de worktree aislado por agente write-capable

El executor SHALL crear un git worktree dedicado por cada `AgentSpec` dentro de un paso `kind==parallel` o `kind==pipeline` para el que `_write_guard_needed(a)` devuelva `True` (es decir, `a.sandbox != "read-only"` o `"write" in (a.tools or [])`). El worktree SHALL ubicarse bajo un directorio temporal con ruta derivada de `(run_id, step_id, agent_id)` siguiendo el patrón `<tmpdir>/iwf-<run_id>-<step_id>-<agent_id>` para garantizar unicidad y reproducibilidad. El agente SHALL ejecutarse con ese directorio como `cwd` en lugar de `self.runner.default_cwd`.

Un agente `read-only` dentro del mismo paso `parallel` o `pipeline` SHALL continuar usando `self.runner.default_cwd` sin creación de worktree.

#### Scenario: agente write-capable en paso parallel crea su propio worktree

- **WHEN** un paso `kind==parallel` contiene dos AgentSpecs ambos con `sandbox != "read-only"` y el directorio de trabajo es un repositorio git válido
- **THEN** el executor crea dos worktrees con rutas distintas derivadas de sus respectivos `agent_id`
- **AND** cada agente recibe como `cwd` la ruta de su propio worktree exclusivo
- **AND** las escrituras de un agente no son visibles para el otro durante la ejecución

#### Scenario: agente read-only en el mismo paso parallel no crea worktree

- **WHEN** un paso `kind==parallel` contiene un AgentSpec con `sandbox=="read-only"` junto a otro write-capable
- **THEN** el agente read-only se ejecuta con `cwd == self.runner.default_cwd` sin ninguna creación de worktree
- **AND** `_create_worktree` no se invoca para ese agente

---

### Requirement: Validación de write_paths dentro del worktree

Cuando se usa aislamiento por worktree, el executor SHALL ejecutar `_git_dirty_paths()` con el `cwd` del worktree (no el `default_cwd` global) tanto antes como después de la llamada al agente. `_validate_write_paths` SHALL resolver paths relativos contra la raíz del worktree. El resultado de validación SHALL ser funcionalmente equivalente al comportamiento existente descrito en `workflow.py:839–853`, pero con scope de suciedad git limitado al worktree individual.

#### Scenario: agente escribe dentro de write_paths permitidas en el worktree

- **WHEN** un agente write-capable ejecutado en su worktree crea o modifica ficheros dentro de las rutas declaradas en `write_paths`
- **THEN** `_validate_write_paths` no lanza `WorkflowError`
- **AND** los ficheros modificados son visibles en el worktree pero no en `default_cwd` hasta la consolidación

#### Scenario: agente escribe fuera de write_paths en el worktree

- **WHEN** un agente write-capable ejecutado en su worktree modifica un fichero fuera de las rutas declaradas en `write_paths`
- **THEN** `_validate_write_paths` lanza `WorkflowError` con el mensaje habitual `"agent step ... wrote outside allowed paths: ..."`
- **AND** la consolidación NO se ejecuta
- **AND** el worktree es eliminado en el bloque `finally`

---

### Requirement: Consolidación determinista y limpieza del worktree

Tras la ejecución exitosa de un agente write-capable en su worktree, el executor SHALL consolidar los cambios a la rama base del repositorio mediante `git merge --squash` aplicado desde `default_cwd` hacia el commit HEAD del worktree, seguido de `git commit --no-edit` si existen cambios staged. La consolidación SHALL completarse antes de devolver el resultado del agente al paso padre. El worktree SHALL eliminarse mediante `git worktree remove --force` en un bloque `finally`, tanto en caso de éxito como de fallo, para evitar acumulación de worktrees huérfanos. Si el directorio de trabajo no es un repositorio git, el executor SHALL lanzar `WorkflowError` con el mensaje `"worktree isolation required for write-capable agents in parallel/pipeline steps, but working directory is not a git repository: <path>"` antes de crear ningún worktree.

#### Scenario: consolidación exitosa integra cambios en la rama base

- **WHEN** un agente write-capable completa con éxito en su worktree y ha modificado ficheros dentro de `write_paths`
- **THEN** el executor ejecuta `git merge --squash` desde `default_cwd` integrando los cambios del worktree
- **AND** los cambios son visibles en `default_cwd` tras la consolidación
- **AND** el worktree es eliminado con `git worktree remove --force`

#### Scenario: limpieza de worktree tras fallo del agente

- **WHEN** un agente write-capable falla (provider devuelve `ok=False`) o lanza excepción
- **THEN** la consolidación NO se ejecuta
- **AND** el worktree es eliminado en el bloque `finally` incluso si el agente lanzó excepción
- **AND** el paso padre propaga el fallo del agente con la semántica habitual de `required`

#### Scenario: directorio no-git con agente write-capable

- **WHEN** el `default_cwd` del runner no es un repositorio git (git rev-parse falla)
- **AND** el paso `parallel` o `pipeline` contiene al menos un AgentSpec write-capable
- **THEN** el executor lanza `WorkflowError` antes de llamar a ningún provider
- **AND** el mensaje de error incluye la ruta del directorio y la causa
