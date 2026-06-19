## Why

Cuando un paso aborta o falla, el runner lanza la excepción aguas arriba pero **no termina el subproceso del provider ni su árbol de hijos**. Esto se observó en producción: tras abortar runs por el write-guard, procesos huérfanos (`agy` → `python download_tactiq.py` → `chrome-headless-shell` de playwright) siguieron ejecutándose **~1 hora** y escribieron ficheros basura en el workspace (`download_tactiq.py`, `tactiq_profile/`, `page_source.html`, `screenshot_loaded.png`) mucho después de que el run "terminara". Es una fuga de procesos + corrupción diferida del workspace + brecha de seguridad.

Raíz en el código actual:

1. `Provider._exec` (`iworkflow/providers.py`) lanza el CLI con `asyncio.create_subprocess_exec` **sin** `start_new_session`, y en timeout hace `proc.kill()` — que mata **solo el proceso directo, no el grupo**. Un agente CLI que abre un navegador headless (node/chromium/playwright) deja esos nietos vivos.
2. `_Executor._exec_command` (`iworkflow/workflow.py`) tiene el mismo patrón (`asyncio.create_subprocess_shell/exec` + `proc.kill()`), matando solo el shell directo y no su árbol.
3. El bucle DAG de `_Executor.run` cancela las tareas asyncio pendientes ante la primera excepción (`asyncio.wait(..., FIRST_EXCEPTION)` + `task.cancel()`), pero **la cancelación de una tarea asyncio no termina el subproceso del sistema operativo** que esa tarea había lanzado: el proceso del provider en vuelo queda huérfano.

`ClaudeInteractiveProvider` ya limpia su sesión tmux en un `finally` (`kill-session`), pero los providers basados en `_exec` (Codex, Gemini, Cursor) no tienen equivalente para su árbol de procesos.

## What Changes

- `Provider._exec` y `_Executor._exec_command` lanzan cada subproceso en su **propio grupo de procesos** (`start_new_session=True`).
- Al **finalizar la llamada del provider por cualquier vía** (retorno con éxito, error, timeout o cancelación) se mata el **grupo entero** con `os.killpg(os.getpgid(pid), SIGKILL)` (fallback a `proc.kill()` si el grupo ya no existe), no solo el PID directo. Esto cubre el caso del leak de #9: el provider retornó con éxito pero dejó descendientes detached (navegador headless) vivos.
- La ejecución del workflow (`_Executor.run`) se envuelve para que, ante cualquier salida (éxito, abort o excepción), **todos los subprocesos de provider en vuelo** sean terminados y esperados (`finally` que cancela y await-ea su cierre).
- Comportamiento sin cambios en el camino feliz: los procesos que terminan solos no se ven afectados.

## Capabilities

### New Capabilities

- `provider-process-lifecycle`: El runner posee el ciclo de vida de los subprocesos de provider y de sus árboles de hijos; los termina de forma fiable en timeout, cancelación, abort o finalización, sin dejar huérfanos ni permitir escrituras posteriores al workspace.

### Modified Capabilities

- (ninguno — los contratos públicos de WorkflowSpec/AgentSpec no cambian)

## Impact

- **Código**: `iworkflow/providers.py` (`Provider._exec`, adaptadores), `iworkflow/workflow.py` (`_exec_command`, `_Executor.run` teardown). Posible helper compartido para matar grupos de procesos (stdlib `os`/`signal`).
- **Seguridad/fiabilidad**: elimina fuga de procesos y corrupción no determinista del workspace tras fallos. Especialmente relevante junto a `worktree-write-isolation` (un worktree huérfano con un proceso vivo dentro es peor).
- **Plataforma**: `os.killpg`/`os.getpgid`/`start_new_session` son POSIX (macOS/Linux). Mantener un fallback a `proc.kill()` para portabilidad.
- **Tests**: `tests/test_providers.py`, `tests/test_workflow.py` (FakeProvider/comando real con un hijo que sobrevive).
- **Docs**: `docs/USING_IWORKFLOW.md` (nota de fiabilidad). Relacionado con GitHub issue #9.
