## Why

Cuando iworkflow ejecuta pasos `parallel` o `pipeline` con agentes write-capable (sandbox distinto de `read-only` o herramienta `write` declarada), todos los agentes comparten `self.runner.default_cwd` como directorio de trabajo. Esto significa que varios agentes pueden modificar el mismo árbol de ficheros de forma concurrente, provocando colisiones silenciosas: el guard de escritura `_validate_write_paths` (workflow.py:839) compara suciedad git antes/después del agente, pero si dos agentes escriben solapados en el mismo directorio los conjuntos `before_dirty`/`after_dirty` se contaminan mutuamente, produciendo falsos positivos o—peor—dejando pasar modificaciones fuera del rango permitido.

El campo `execution.worktree` existe en el spec como un hint (workflow.py:302, 323) pero el motor nunca materializa ningún git worktree: el executor no ejecuta `git worktree add` ni cambia el `cwd` por paso.

Herramienta de referencia — Claude Code `--worktree`: cuando Claude Code lanza una sesión con `--worktree <nombre>`, crea automáticamente un git worktree sobre su propia rama (`worktree-<nombre>`), de modo que las ediciones de cada sesión son físicamente imposibles de colisionar con otra porque ocupan rutas distintas en el filesystem (compartiendo solo la base de datos de objetos `.git`). Esta capacidad requiere que el repositorio sea git y recomienda `git worktree remove` tras completar para no dejar entornos huérfanos.

## What Changes

Se añade la capability `worktree-isolation`: para cada AgentSpec write-capable dentro de un paso `parallel` o `pipeline`, el executor crea un git worktree aislado bajo un directorio temporal (`<tmpdir>/iwf-<run_id>-<step_id>-<agent_id>`), ejecuta el agente con ese directorio como `cwd`, valida `write_paths` dentro del worktree y, al completar con éxito, consolida los cambios a la rama base mediante `git cherry-pick` o `git merge --squash`. El worktree se elimina al terminar (éxito o fallo). Si el directorio de trabajo no es un repositorio git, el motor emite un error descriptivo y cancela el paso; en modo `read-only` no se crea ningún worktree.

Nada cambia en la ruta de agentes `kind==agent` de nivel superior ni en pasos supervisores.

## Capabilities

### New Capabilities

- `worktree-isolation`: Creación automática de git worktrees por-agente para pasos write-capable dentro de `parallel`/`pipeline`, con validación de write_paths dentro del worktree y consolidación determinista a la rama base al completar con éxito.

### Modified Capabilities

## Impact

- Pasos `parallel`/`pipeline` existentes con `sandbox: read-only` (o sin agentes write-capable): sin cambio de comportamiento; no se crea ningún worktree.
- Pasos `parallel`/`pipeline` existentes con agentes write-capable: requieren directorio git válido; si no lo hay, falla con error descriptivo (antes podía ejecutarse sin aislamiento).
- El campo `execution.worktree` del spec sigue siendo un hint no forzado; esta capability actúa de forma automática basándose en la escriturabilidad del agente, sin requerir cambios en specs existentes.
- Se añade dependencia en runtime de `git worktree add/remove/cherry-pick` (git >= 2.5); sin dependencias de pago ni SDK externo.
