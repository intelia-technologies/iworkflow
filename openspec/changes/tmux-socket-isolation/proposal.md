## Why

`ClaudeInteractiveProvider._tmux` (providers.py:636-641) ejecuta cada comando tmux contra el servidor por defecto del sistema — sin ningún flag `-L`. Las sesiones se nombran `iwf-{os.getpid()}-{self._seq}` (providers.py:658), lo cual proporciona unicidad a nivel de proceso, pero no aísla el servidor tmux. Dos runs iworkflow paralelos (o un run y una sesión tmux de usuario) comparten el mismo servidor tmux, con las consecuencias siguientes:

1. **Contaminación del namespace del usuario**: las sesiones `iwf-*` aparecen en `tmux ls` del usuario. Un `kill-server` accidental del usuario destruye las sesiones activas del workflow.
2. **Colisión entre runs concurrentes**: si dos procesos iworkflow comparten PID por reúso (raro pero posible en contenedores) o si una sesión anterior no fue limpiada, `kill-session` en el `finally` puede matar la sesión equivocada.
3. **Riesgo de autenticación**: el servidor tmux por defecto puede ser un proceso de larga vida sin el linaje de proceso correcto para acceder al keychain de macOS, lo que puede provocar fallos de autenticación en `claude` cuando se usa en ese servidor.

Este cambio no corrige un bug; introduce una capacidad de aislamiento que hoy no existe.

## What Changes

`ClaudeInteractiveProvider` recibe un atributo `tmux_socket: str | None` (nuevo). Cuando está definido, `_tmux` antepone `-L <socket>` a cada invocación de tmux. El valor canónico es `iw_{run_id}`.

`Runner` (scheduler.py) construye el socket name como `iw_{run_id}` y lo pasa al `ClaudeInteractiveProvider` en tiempo de construcción, o bien expone un método factory `Runner.build_claude_provider(...)` que lo inyecta. Al finalizar el workflow (en el punto de teardown del executor o del runner), se ejecuta `tmux -L iw_{run_id} kill-server` para destruir el servidor de socket de ese run.

`FakeProvider` y el resto de providers no usan tmux; no se ven afectados.

## Capabilities

### New Capabilities

- `tmux-isolation`: Aislamiento del servidor tmux por run. `ClaudeInteractiveProvider` opera en un socket tmux dedicado (`tmux -L iw_{run_id}`) en lugar del servidor por defecto, evitando contaminación del namespace de usuario y colisiones entre runs concurrentes. El servidor se destruye automáticamente al finalizar el workflow.

### Modified Capabilities

## Impact

- **providers.py**: `ClaudeInteractiveProvider` añade campo `tmux_socket: str | None = None`; `_tmux` prepend `-L <socket>` cuando está definido.
- **scheduler.py**: `Runner.__init__` construye `tmux_socket = f"iw_{run_id}"` y lo inyecta en el provider Claude cuando esté presente en `self.providers`.
- **workflow.py**: El `_Executor` (o el teardown de `Runner`) emite `tmux -L iw_{run_id} kill-server` al finalizar el run (tras `kill-session` individual).
- **tests/test_providers.py**: Verificación de que `_tmux` recibe `-L <socket>` cuando `tmux_socket` está configurado.
- **tests/test_scheduler.py**: Verificación de que `Runner` inyecta el socket correcto en `ClaudeInteractiveProvider`.
- Retro-compatibilidad: `tmux_socket=None` (defecto) mantiene el comportamiento actual sin `-L`.
