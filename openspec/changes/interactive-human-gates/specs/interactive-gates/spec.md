## ADDED Requirements

### Requirement: Paso `checkpoint` de aprobación humana

El motor SHALL soportar un kind de paso `checkpoint` que declara un punto de intervención humana con `prompt`/`title`, `artifact` opcional (ruta a un fichero renderizado a mostrar), `schema` opcional (forma del input estructurado esperado), `output` (ruta del fichero de resolución) y `mode` (`approval` | `input` | `confirm`). El input humano SHALL tratarse como dato inyectado en el contexto; el control de flujo SHALL permanecer determinista (sin LLM decidiendo el routing).

#### Scenario: parseo de un checkpoint válido

- **WHEN** una spec declara un paso `{"id": "gate1", "kind": "checkpoint", "mode": "input", "schema": <S>, "output": "decisions.json", "artifact": "gate1.html"}`
- **THEN** `WorkflowSpec.parse` lo acepta como paso de tipo checkpoint con esos campos

#### Scenario: `mode: input` exige schema

- **WHEN** se declara un checkpoint `mode: "input"` sin `schema`
- **THEN** el parseo falla con un error explícito que nombra el requisito de `schema`

### Requirement: Pausa con estado `PAUSED` de primera clase

Cuando un `checkpoint` no tiene resolución disponible y el run es desatendido, el motor SHALL detener la ejecución con estado `PAUSED` (distinto de `DONE`, `ABORTED` y `ERROR`) y SHALL incluir un payload `pending_input` con `{step_id, prompt, artifact, schema, output}`. El motor SHALL emitir un evento `checkpoint_pending` con esos campos.

#### Scenario: run desatendido se pausa en el checkpoint

- **WHEN** un run sin resolvedor interactivo alcanza un `checkpoint` cuyo `output` no existe
- **THEN** el run termina con `status == "PAUSED"`, el bundle contiene `pending_input.step_id` igual al id del checkpoint, y `events.jsonl` contiene un evento `checkpoint_pending`

#### Scenario: la pausa no es un error

- **WHEN** un run queda pausado en un checkpoint
- **THEN** el estado NO es `ABORTED` ni `ERROR`, y los pasos `needs`-dependientes del checkpoint no se han ejecutado

### Requirement: Reanudación determinista desde la resolución

Al relanzar un run con el mismo `run_id`, un `checkpoint` cuyo `output` ya existe SHALL completarse de forma determinista: su contenido se inyecta en `ctx.steps.<id>.value`, se persiste en el journal como paso completado y el run continúa con los pasos siguientes. La reanudación SHALL reutilizar la maquinaria de resume existente (los pasos completados no se reejecutan).

#### Scenario: resume tras aportar la resolución

- **WHEN** un run pausado en `gate1` se relanza con el mismo `run_id` después de que el `output` (`decisions.json`) exista y sea válido
- **THEN** `gate1` se resuelve, su valor queda disponible en `ctx.steps.gate1.value`, los pasos posteriores se ejecutan y el run alcanza `status == "DONE"`

#### Scenario: pasos previos no se reejecutan al reanudar

- **WHEN** se reanuda un run que ya tenía pasos completados antes del checkpoint
- **THEN** esos pasos se saltan (journaled) y solo se ejecutan el checkpoint resuelto y lo que va después

### Requirement: Captura de input validada por schema

Cuando un `checkpoint` declara `schema`, la resolución SHALL validarse con el validador de esquemas del motor antes de avanzar. Una resolución que no cumple el `schema` NO SHALL avanzar el run: el checkpoint permanece pendiente y el payload reporta el error de validación.

#### Scenario: resolución que no cumple el schema mantiene la pausa

- **WHEN** el `output` de un checkpoint con `schema` existe pero no valida (p.ej. falta una clave requerida)
- **THEN** el run permanece pausado/no-avanza y el payload `pending_input` (o el error reportado) indica el motivo de la validación fallida

#### Scenario: resolución válida avanza

- **WHEN** el `output` valida contra el `schema`
- **THEN** el checkpoint resuelve y el run continúa

### Requirement: Resolución interactiva en modo atendido

Cuando el caller provee un resolvedor interactivo (p.ej. CLI `--interactive` o un canal de aprobación de MCP/dashboard), un `checkpoint` SHALL obtener su resolución en línea y continuar en el mismo proceso sin requerir un relanzamiento. La ausencia de resolvedor SHALL equivaler al modo desatendido (pausa).

#### Scenario: resolvedor inyectado resuelve sin relanzar

- **WHEN** un run con un resolvedor interactivo que devuelve una resolución válida alcanza un `checkpoint`
- **THEN** el checkpoint resuelve en línea y el run alcanza `status == "DONE"` en una sola corrida, sin pasar por `PAUSED`

### Requirement: Confirmación afirmativa explícita

Un `checkpoint` con `mode: "confirm"` SHALL avanzar únicamente ante una resolución afirmativa explícita. Una resolución ausente, ambigua o negativa NO SHALL avanzar el run.

#### Scenario: respuesta afirmativa explícita avanza

- **WHEN** la resolución de un checkpoint `mode: "confirm"` es afirmativa explícita (p.ej. `{"approved": true}`)
- **THEN** el run continúa

#### Scenario: respuesta ambigua no avanza

- **WHEN** la resolución de un checkpoint `mode: "confirm"` es ambigua o negativa
- **THEN** el run NO continúa (permanece pausado o aborta según configuración) y no se ejecuta ninguna escritura aguas abajo
