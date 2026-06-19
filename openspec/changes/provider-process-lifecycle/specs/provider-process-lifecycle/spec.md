## ADDED Requirements

### Requirement: Aislamiento de subprocesos de provider en grupo propio

Todo subproceso lanzado por `Provider._exec` y por `_Executor._exec_command` SHALL crearse en su propio grupo de procesos (vía `start_new_session=True`), de modo que el runner pueda señalizar al árbol completo de descendientes y no solo al proceso directo. El runner SHALL registrar el id de grupo de cada subproceso de provider despachado para poder terminarlo después.

#### Scenario: el subproceso se lanza en un grupo nuevo

- **WHEN** `Provider._exec` lanza un CLI de provider
- **THEN** el proceso se crea con `start_new_session=True` y su PID es líder de su propio grupo de procesos

#### Scenario: comando local también aislado

- **WHEN** un `command` step ejecuta su subproceso vía `_exec_command`
- **THEN** el subproceso se crea con `start_new_session=True`

### Requirement: Terminación del grupo al finalizar la llamada del provider, incluido el éxito

Cuando una llamada a un provider termina por CUALQUIER vía —retorno con éxito, error del provider, timeout o cancelación— el runner SHALL terminar el **grupo de procesos completo** del subproceso, no solo el proceso directo. Esto SHALL cubrir el caso en que el proceso directo del provider retorna normalmente pero ha dejado **descendientes detached** (shells, node, navegadores headless) ejecutándose: esos descendientes SHALL ser terminados al cerrarse la llamada. La terminación SHALL ser idempotente y no lanzar si el grupo ya finalizó.

#### Scenario: provider que retorna pero deja un hijo detached

- **WHEN** un provider devuelve su valor con éxito pero su proceso lanzó un hijo de larga vida desacoplado (p.ej. un navegador headless)
- **THEN** al cerrarse la llamada del provider, el grupo de procesos completo (incluido el hijo) queda terminado y no sigue vivo tras retornar el resultado

#### Scenario: timeout mata padre e hijos

- **WHEN** un subproceso de provider lanza un hijo de larga vida y luego se excede el `timeout_s`
- **THEN** tanto el proceso padre como el hijo quedan terminados, y `_exec` devuelve un resultado de timeout

#### Scenario: terminación idempotente

- **WHEN** se intenta terminar el grupo de un proceso que ya había terminado por su cuenta
- **THEN** no se lanza ninguna excepción y el flujo continúa normalmente

### Requirement: No quedan subprocesos huérfanos tras abort, excepción o fin de run

Al finalizar la ejecución de un workflow por cualquier vía —éxito, abort por gate/write-guard, o excepción aguas arriba— el runner SHALL garantizar, en un `finally` a nivel de run, que ningún grupo de procesos de provider despachado durante el run permanece vivo. Esto SHALL incluir tanto los subprocesos en vuelo en el momento del abort como los descendientes detached de providers que ya habían retornado antes del abort.

#### Scenario: abort tras retorno del provider no deja descendientes vivos

- **WHEN** un provider retorna con éxito dejando un descendiente detached, e inmediatamente después el write-guard lanza `WorkflowError` y aborta el workflow
- **THEN** tras retornar/relanzar `_Executor.run`, ni el provider ni su descendiente siguen vivos (este es exactamente el escenario del leak de GitHub issue #9)

#### Scenario: abort con un segundo provider en vuelo

- **WHEN** un workflow aborta en un paso mientras otro paso/provider sigue en ejecución
- **THEN** tras el teardown del run no queda vivo ningún subproceso de provider del run

#### Scenario: no hay corrupción diferida del workspace

- **WHEN** un run termina (éxito o abort) y se inspecciona el workspace tras la terminación del runner
- **THEN** ningún subproceso superviviente puede escribir ficheros nuevos en el workspace después de que el run haya terminado

#### Scenario: el camino feliz no se ve afectado

- **WHEN** todos los pasos completan con normalidad y sus procesos terminan solos
- **THEN** los resultados se devuelven intactos y la terminación de grupos no altera la salida
