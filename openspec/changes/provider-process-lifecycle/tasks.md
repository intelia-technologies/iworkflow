## 1. Helper de grupos de procesos

- [ ] 1.1 Añadir un helper interno (stdlib `os`/`signal`) que, dado un `asyncio.subprocess.Process`, mate su grupo de procesos completo: `os.killpg(os.getpgid(proc.pid), SIGKILL)` con fallback a `proc.kill()` si `ProcessLookupError`/no-POSIX.
- [ ] 1.2 El helper debe ser idempotente y no lanzar si el proceso/grupo ya terminó.

## 2. Aislamiento de procesos en providers

- [ ] 2.1 `Provider._exec` lanza con `start_new_session=True` para crear un grupo de procesos por subproceso de provider.
- [ ] 2.2 En timeout, `Provider._exec` mata el grupo entero con el helper (no solo `proc.kill()`), y await-ea el cierre.
- [ ] 2.3 Aplicar el mismo patrón en `_Executor._exec_command` (`start_new_session=True` + kill de grupo en timeout).

## 3. Teardown del runner ante abort/cancelación

- [ ] 3.1 Registrar los subprocesos de provider en vuelo de modo que `_Executor.run` pueda terminarlos.
- [ ] 3.2 Envolver `_Executor.run` (o el dispatch del batch DAG) en un `try/finally` que, ante éxito/abort/excepción, termine y await-ee todos los subprocesos en vuelo.
- [ ] 3.3 Verificar que la cancelación de una tarea asyncio del batch propaga a matar el subproceso del provider que esa tarea lanzó.

## 4. Tests

- [ ] 4.1 Test: un `command` step cuyo proceso lanza un hijo de larga vida; al hacer timeout, el árbol (padre + hijo) queda terminado.
- [ ] 4.2 Test: un workflow que aborta en un paso (gate/write-guard) mientras otro provider está en vuelo; tras el abort no quedan subprocesos de provider vivos.
- [ ] 4.3 Test: el camino feliz no se ve afectado (procesos que terminan solos siguen devolviendo su salida).

## 5. Docs

- [ ] 5.1 Nota de fiabilidad en `docs/USING_IWORKFLOW.md` sobre el ciclo de vida de subprocesos de provider y la garantía de no-huérfanos.
- [ ] 5.2 Referenciar GitHub issue #9 en el proposal/commit.
