## ADDED Requirements

### Requirement: Socket Dedicado por Run

`ClaudeInteractiveProvider` SHALL aceptar un atributo de configuración `tmux_socket: str | None`. Cuando `tmux_socket` no sea `None`, TODA invocación interna de `tmux` realizada por el provider MUST incluir los flags `-L <tmux_socket>` inmediatamente después del ejecutable `tmux`, antes de cualquier subcomando. Esto enruta el comando al servidor tmux del socket especificado en lugar del servidor por defecto del sistema.

El valor canónico del socket para un run con identificador `run_id` SHALL ser `iw_{run_id}` (prefijo `iw_` seguido del run_id sin modificación adicional). Cuando `tmux_socket` sea `None` (valor por defecto), el comportamiento SHALL ser idéntico al actual: tmux opera en el servidor por defecto, sin flag `-L`.

#### Scenario: provider con socket usa -L en new-session

- **WHEN** se construye `ClaudeInteractiveProvider(tmux_socket="iw_abc123")` y se invoca `_tmux("new-session", "-d", "-s", "iwf-99-1", ...)`
- **THEN** el proceso lanzado MUST tener argv `["tmux", "-L", "iw_abc123", "new-session", "-d", "-s", "iwf-99-1", ...]`

#### Scenario: provider sin socket no añade -L

- **WHEN** se construye `ClaudeInteractiveProvider()` (sin `tmux_socket`) y se invoca `_tmux("new-session", ...)`
- **THEN** el proceso lanzado MUST tener argv `["tmux", "new-session", ...]` (sin `-L` en ninguna posición)

#### Scenario: todos los comandos tmux del run usan el mismo socket

- **WHEN** `ClaudeInteractiveProvider(tmux_socket="iw_run42")` ejecuta un ciclo completo: kill-session preemptivo, new-session, set-buffer, paste-buffer, send-keys, capture-pane, kill-session final
- **THEN** CADA uno de esos subprocesos MUST incluir `-L iw_run42` en argv

---

### Requirement: Inyección del Socket desde Runner

`Runner` (scheduler.py) SHALL calcular el socket name `f"iw_{self.run_id}"` en su constructor. Para cada provider en `self.providers` cuyo tipo sea `ClaudeInteractiveProvider`, el Runner MUST asignar `provider.tmux_socket` con ese valor antes de que se ejecute cualquier agente del workflow. Esta asignación MUST ocurrir en `Runner.__init__`, no en tiempo de dispatch.

El Runner SHALL exponer un método `teardown_tmux()` (corrutina async) que, cuando sea invocado, ejecute `tmux -L iw_{run_id} kill-server` para destruir el servidor del socket de ese run. Si el servidor ya no existe (proceso terminado anticipadamente), el método MUST ignorar el error en lugar de propagarlo.

#### Scenario: runner inyecta socket en ClaudeInteractiveProvider

- **WHEN** se construye `Runner(run_id="myrun", providers={"claude": ClaudeInteractiveProvider()}, caps={...})`
- **THEN** `runner.providers["claude"].tmux_socket` MUST ser igual a `"iw_myrun"` inmediatamente tras la construcción

#### Scenario: runner no afecta a providers que no son ClaudeInteractive

- **WHEN** se construye `Runner(run_id="x", providers={"fake": FakeProvider(responses=[...]), "claude": ClaudeInteractiveProvider()}, caps={...})`
- **THEN** `runner.providers["fake"]` MUST NO tener atributo `tmux_socket` modificado (FakeProvider no tiene dicho atributo)
- **AND** `runner.providers["claude"].tmux_socket` MUST ser `"iw_x"`

#### Scenario: teardown_tmux emite kill-server en el socket correcto

- **WHEN** se invoca `await runner.teardown_tmux()` con `run_id="abc"`
- **THEN** el subproceso lanzado MUST tener argv `["tmux", "-L", "iw_abc", "kill-server"]`

---

### Requirement: Teardown del Servidor de Socket al Finalizar el Workflow

El `_Executor` de `workflow.py` (o el punto de cierre del workflow en `cli.py` / `mcp_server.py`) SHALL invocar `runner.teardown_tmux()` en su bloque `finally` tras la finalización (exitosa o con error) de todos los pasos del workflow. Esta llamada MUST ocurrir después de que los providers hayan completado sus operaciones individuales (que ya incluyen `kill-session` por sesión), pero en el mismo contexto de ejecución async que el run.

El teardown del servidor de socket MUST ser idempotente: si ya fue destruido o nunca llegó a crearse (p.ej. porque `ClaudeInteractiveProvider` nunca fue despachado), la llamada no deberá lanzar excepciones visibles al usuario.

#### Scenario: teardown ocurre en path de éxito del workflow

- **WHEN** un workflow completa todos sus pasos sin error y el executor finaliza normalmente
- **THEN** `teardown_tmux()` MUST ser llamado exactamente una vez durante el cierre del executor

#### Scenario: teardown ocurre en path de error del workflow

- **WHEN** un workflow lanza `WorkflowError` durante la ejecución de un paso
- **THEN** el bloque `finally` del executor MUST invocar `teardown_tmux()` antes de propagar la excepción

#### Scenario: teardown es no-op cuando no hay ClaudeInteractiveProvider

- **WHEN** el workflow no usa `ClaudeInteractiveProvider` en ningún paso (solo `FakeProvider` o `CodexProvider`)
- **AND** `teardown_tmux()` es invocado en el finally del executor
- **THEN** la llamada MUST completar sin error (el proceso `tmux kill-server` retornará error porque el socket no existe, y ese error MUST ser suprimido)
