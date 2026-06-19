# Tasks: tmux-socket-isolation

## 1. Implementación en providers.py

- [x] 1.1 Añadir campo `tmux_socket: str | None = None` al dataclass `ClaudeInteractiveProvider` (providers.py ~línea 631).
- [x] 1.2 Modificar `ClaudeInteractiveProvider._tmux` para que, cuando `self.tmux_socket` no sea `None`, inserte `["-L", self.tmux_socket]` después de `"tmux"` y antes de `*args` (providers.py:637).
- [x] 1.3 Verificar que todos los call-sites de `_tmux` dentro de `ClaudeInteractiveProvider.run` (kill-session preemptivo, new-session, set-buffer, paste-buffer, send-keys, capture-pane, kill-session en finally) reciben el socket automáticamente vía `_tmux`.

## 2. Inyección del socket en scheduler.py

- [x] 2.1 En `Runner.__init__` (scheduler.py:64), calcular `self._tmux_socket = f"iw_{run_id}"`.
- [x] 2.2 Tras construir los providers, iterar `self.providers` y, para cada `ClaudeInteractiveProvider` encontrado, asignar `provider.tmux_socket = self._tmux_socket`.
- [x] 2.3 Añadir método `Runner.teardown_tmux()` (async) que ejecute `tmux -L iw_{run_id} kill-server` usando `asyncio.create_subprocess_exec`; ignorar error si el servidor ya no existe (returncode != 0 es aceptable).

## 3. Teardown del servidor de socket en workflow.py

- [x] 3.1 Identificar el punto de finalización del `_Executor.run()` (workflow.py) donde se cierra el runner o se completa el workflow.
- [x] 3.2 Llamar a `runner.teardown_tmux()` (si el runner expone el método) en el bloque `finally` del executor, tras el join de todas las tareas.
- [x] 3.3 Asegurar que el teardown ocurra también en los paths de error/cancelación (WorkflowError, KeyboardInterrupt).

## 4. Tests unitarios

- [x] 4.1 En `tests/test_providers.py`: añadir test `test_claude_interactive_tmux_socket_prefix` que instancie `ClaudeInteractiveProvider(tmux_socket="iw_testrun")`, monkeypatchee `asyncio.create_subprocess_exec` para capturar argv, y verifique que el primer elemento de argv tras `"tmux"` es `"-L"` seguido de `"iw_testrun"`.
- [x] 4.2 En `tests/test_providers.py`: añadir test `test_claude_interactive_no_socket_no_prefix` que instancie sin `tmux_socket` y verifique que argv NO contiene `"-L"`.
- [x] 4.3 En `tests/test_scheduler.py`: añadir test `test_runner_injects_tmux_socket` que construya un `Runner` con un `ClaudeInteractiveProvider` en `providers`, y verifique que `provider.tmux_socket == f"iw_{run_id}"` tras la inicialización.
- [x] 4.4 En `tests/test_scheduler.py`: añadir test `test_runner_teardown_tmux_called` que verifique que `teardown_tmux()` emite `["tmux", "-L", "iw_<run_id>", "kill-server"]` al ser invocado.

## 5. Documentación y spec

- [x] 5.1 Actualizar `specs/iworkflow.openspec.json`: añadir entrada para la capability `tmux-isolation` en la sección correspondiente.
- [x] 5.2 Actualizar `docs/USING_IWORKFLOW.md`: añadir sección breve sobre aislamiento tmux — cómo opera el socket por run, que el usuario no verá sesiones `iwf-*` en su tmux, y que el servidor se destruye al terminar el workflow.
