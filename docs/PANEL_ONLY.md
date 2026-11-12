# Sin agente principal: el panel lo gestiona todo

Estado: implementado en la rama `PabloMoralesEscandon/rename` (sin fusionar ni publicar). Fecha: 2026-11-12.

## La decisión

Indicación del usuario: «si hay agente principal, que se elimine; toda la gestión la debe llevar el menú de
creación; cada usuario decide sus agentes».

- **No hay agente principal.** El bot base (`TELEGRAM_BOT_TOKEN`) es solo el panel de control. No tiene
  sesión de Claude, ni manual, ni memoria, ni personalidad.
- **Toda la gestión la hace el proceso de Python del panel**: crear, configurar, pausar, reanudar y destruir
  bots; permisos, presets y acceso entre agentes con su auditoría; el planificador y sus entregas; la memoria
  compartida; el monitor de uso; la personalidad de cada bot; el servicio. No la hace ningún agente.
- **Todo se gestiona desde el menú `/bot`.** Los comandos de texto quedan como atajos.
- **Cada usuario decide sus agentes.** A nadie se le crea un bot por defecto. Cada usuario permitido crea,
  ve, configura y destruye solo los suyos, y no puede tocar los de otro.

## 1. Restos del agente principal y qué se ha hecho con cada uno

| # | Dónde | Qué era | Qué se ha hecho |
|---|---|---|---|
| 1 | `agent.py`, `Agent` | Se podía crear con la configuración del panel (bot 0) y trabajar en el home | `Agent` rechaza la configuración del panel (`NoAgent`). Solo un bot tiene agente |
| 2 | `agent.py`, `_prepare_workspace` | Para el bot 0 refrescaba `SHARED_MEMORY.md` en el home | Eliminado: siempre prepara el directorio del bot |
| 3 | `agent.py`, `_environment` | `ALFRED_WORKSPACE` solo para bots | Siempre: todo agente es un bot |
| 4 | `telegram_bot.py`, `TelegramBot` | `record=None` era «el bot principal», con `/help` «I'm Alfred», `title` «Alfred» y su propio planificador (`_post_init`/`_post_shutdown`, `run_polling`) | Exige un bot registrado y su token. Eliminados la rama principal, el planificador propio y el arranque suelto |
| 5 | `home.py`, `bootstrap` | Escribía `CLAUDE.md`, `AGENTS.md`, `SOUL.md`, `MEMORY.md`, `memory/`, `.claude/skills/`, `extensions/` y `inbox/` en el home, iniciaba un repo git, importaba el diseño de la primera versión y retiraba skills sembradas | Sustituido por `home.prepare`: solo `state/`, el lanzador `bin/alfred` y la memoria compartida (fuera del home). Eliminados la importación antigua, la memoria de un fichero y la retirada de skills |
| 6 | `home.autosave` | Hacía commit del home tras cada turno del agente principal | Nunca hace commit del home; solo del directorio de un bot |
| 7 | `templates/CLAUDE.md`, `templates/AGENTS.md` | Manual del agente principal | Borrados. El manual de cada bot es `BOT.md` |
| 8 | `templates/SOUL.md` | «Your name is Alfred.», personalidad del agente principal | Ahora es la plantilla de la personalidad base de los bots (§5) |
| 9 | `shared_memory.py`, modo `MAIN` | El agente principal escribía la memoria compartida | Eliminado. La escriben el usuario desde el panel y los bots con el permiso `shared` |
| 10 | `bots.py`, `other_agents` | El agente principal era un «otro agente» (`OtherAgent("Alfred", home, main=True)`) | Eliminado. Los «otros agentes» son solo bots |
| 11 | `bots.py`, `blacklist` | Con acceso entre agentes se abrían la memoria y las herramientas del home | El home queda cerrado siempre, a todos los niveles y también con acceso entre agentes, incluidas sus transcripciones de Claude Code |
| 12 | `extensions.held_back` | El bot 0 cargaba hooks y servidores MCP de sus extensiones sin límites | Sin excepción: las mismas reglas para todos |
| 13 | `cli.py`, `resolve_bot_id` | Sin `--bot` ni `ALFRED_BOT_ID`, el bot 0; alias `main` y `alfred` | Sin alias. `send`, `schedule`, `event` y `usage-alert` exigen un bot; `skills` y `ext`, el turno de un bot |
| 14 | `cli.py`, `chat` | Sin `--bot` hablaba con el agente principal en el home | `--bot` obligatorio |
| 15 | `cli.py`, `screenshot` | Guardaba en `~/.alfred/outbox` | En el `outbox/` del bot |
| 16 | `cli.py`, `init`, `browser-setup`, `service install` | Llamaban a `bootstrap` | Llaman a `home.prepare` |
| 17 | Planificador, `bot_id = 0` | Recordatorios y tareas del chat principal | Se conservan; ver §3 |
| 18 | Comandos de bot en el chat del panel (`/new`, `/stop`, `/memory`, `/skills`, `/backend`) | Asumían una sesión principal | El panel nunca los tuvo registrados (`PANEL_COMMANDS`); escritos como texto, el panel responde que no tiene agente. Un test lo comprueba |
| 19 | Textos: panel, menús, presets, manuales, skill `extensions`, README, docs | «the other bots and Alfred», «Alfred's main agent», «your home» | Corregidos: «your other bots», «the bot's own directory», etc. |
| 20 | Textos de la fase 1 del renombrado | Decían que Alfred seguía como agente principal | Corregidos en el código y en `RENAME_PLAN.md` |

Dos errores que el agente principal tapaba y que han salido al quitarlo: la primera nota de progreso se
perdía en los primeros 2,5 s del proceso (`_last_edit = 0.0` con un `time.monotonic()` que empieza en 0), y
el manual de un bot con acceso entre agentes y un solo bot más decía también «No other agent has memory or
tools yet». Ambos corregidos.

## 2. Qué se conserva: el home como estado del panel

`~/.alfred` (o `ALFRED_HOME`) sigue existiendo, pero no tiene agente. Es solo estado del sistema y está
siempre fuera del alcance de los bots: `Read` y `Edit` denegados en todo el home, reglas de shell sobre sus
partes privadas y `denyRead`/`denyWrite` en el sandbox, con o sin acceso entre agentes.

| Ruta | Qué es |
|---|---|
| `bin/alfred` | El lanzador que todo bot tiene en el PATH (lo ejecuta; no puede leerlo ni editarlo) |
| `.env` | Ajustes y token del panel |
| `state/bots/<id>/` | Registro de cada bot: `bot.json` (propietario, ajustes, permisos), `token`, `sessions.json`, `claude-settings.json`, alertas de uso |
| `state/bots/pending.json` | Peticiones de creación en curso, por usuario |
| `state/scheduler.sqlite3` | El planificador de todos los bots |
| `state/audit.log` | Concesiones y retiradas de acceso entre agentes (ahora con el propietario del bot) |
| `state/usage.json` | Lecturas recientes del monitor de uso |
| `state/soul-base.md` | La personalidad base, cuando se edita |
| `state/alfred.log` | El log del servicio |
| `~/.alfred-shared/memory` | La memoria compartida y su índice (junto al home, no dentro) |

## 3. Recordatorios y tareas del chat principal

Decisión: **se conservan y no se borran.**

- El planificador los sigue entregando el panel en el chat donde se crearon. Un recordatorio (mensaje) se
  publica como siempre. Una tarea o un evento se anuncia («This task was scheduled for the old main agent.
  There is no main agent any more…») y no se ejecuta, porque no hay agente que lo haga.
- En el menú, **⏰ Old reminders (N)** aparece en el inicio solo si el usuario tiene alguno pendiente en su
  chat. Los lista y permite cancelarlos uno a uno. Solo se ven los del chat de cada usuario.
- No se puede crear ninguno nuevo para el bot 0: `alfred schedule` exige un bot.
  `SchedulerStore.delete_for_bot(0)` sigue rechazándose.
- Para que una tarea antigua vuelva a ejecutarse, se pide a uno de los bots en su chat («recuérdame cada
  lunes…») o el bot la programa con `alfred schedule`. No hay migración automática: decidir a qué bot va
  cada tarea es cosa del usuario.

## 4. Lo que queda del antiguo agente principal en un home existente

En el home real (`~/.alfred`) siguen `CLAUDE.md`, `AGENTS.md`, `SOUL.md`, `memory/` (con su `MEMORY.md`),
`extensions/`, `inbox/`, `outbox/`, `.claude/`, `.git/` y, fuera del home,
`~/.claude/projects/-Users-…--alfred/` (sus transcripciones y su memoria automática de Claude Code).

- Nada los lee, los reescribe ni los borra. `home.prepare` no los toca (hay un test que lo comprueba).
- Todos los bots los tienen denegados, también con acceso entre agentes.
- `alfred chat` ya no puede hablar con ese agente.

Qué hacer con ellos es decisión del usuario (ver §6). Nada de esto se ha tocado en `~/.alfred`.

## 5. Personalidad por bot (SOUL.md)

**Antes:** solo el agente principal tenía `SOUL.md` (en el home). Los bots no tenían ninguno: su manual
(`BOT.md`) no importaba personalidad.

**Ahora:**

- **Dónde vive.** Cada bot tiene `SOUL.md` en su directorio (`~/.alfred-bots/<id>/SOUL.md`), importado por
  su manual (`@SOUL.md`; Codex recibe «Read SOUL.md…»). El manual dice que la personalidad marca el tono,
  nunca lo que el bot puede hacer, y que nada en `SOUL.md` concede, levanta ni pide saltarse un permiso.
- **De dónde sale.** Del texto escrito en el asistente de creación o, si no hay, de la **personalidad
  base**: `state/soul-base.md` si se ha editado, si no la plantilla `alfred/templates/SOUL.md` (tono,
  estilo y actitud genéricos, sin nombre). Un bot creado antes de esto recibe la base en su siguiente
  mensaje. Nunca se sobrescribe un `SOUL.md` que ya existe.
- **Cómo se edita.**
  - Menú → Mis bots → un bot → **🎭 Personality**: ver, **Replace** (el texto nuevo llega en el siguiente
    mensaje; nada cambia hasta entonces), **Describe it** (§5.1) y **Reset to the base**.
  - Atajo de texto: `/bot soul <bot> show|set <texto>|reset`.
  - La base: menú → inicio → **🎭 Base personality** (ver, reemplazar, volver a la plantilla), o
    `/bot base-soul show|set <texto>|reset`. Cambiar la base no toca los bots que ya existen.
  - Asistente de creación: un paso 3 de 7, opcional, «personality»: usar la base (por defecto), escribir
    una propia o describirla para que Claude escriba un borrador. El resumen dice cuál lleva.
- **Cuándo se aplica.** La huella de `SOUL.md` forma parte de los ajustes del proceso vivo
  (`#soul=` en `ClaudeBackend.live_settings`). Al cambiarla, el siguiente mensaje reabre el proceso con
  `--resume`: misma conversación, como con un cambio de modelo o de manual.
- **Validación.** No vacía, como mucho 4.000 caracteres y sin nada que parezca un secreto (los mismos
  patrones que extensiones y memoria compartida).
- **Permiso `soul`**, nuevo, desactivado en todos los presets, bajo Personalizar como «🎭 Edit its own
  personality», con el aviso «it can rewrite its own SOUL.md, and with it how it behaves…».
  - Sin él: `Edit` del `SOUL.md` propio denegado, `denyWrite` del sandbox y, con shell abierto,
    `Bash(*SOUL.md*)` denegado.
  - Con él: solo se abre el `SOUL.md` propio. El de otro bot está siempre cerrado (todo su directorio lo
    está para `Edit`).
  - **Ajuste respecto a lo pedido:** se pidió exigir además que el preset permitiera escribir en su
    carpeta. En el código actual todo bot puede escribir siempre en su propia carpeta, en todos los
    presets (`Edit(<su directorio>/**)` está siempre permitido). Por eso la condición efectiva es solo el
    permiso `soul`. En modo `plan` no escribe nada de todos modos.
- **Propietario.** Ver, cambiar, restablecer y generar solo sobre bots propios (`find`/`get_owned` con
  el propietario, en comandos y botones).

### 5.1 Borradores con Claude (`alfred/soul_writer.py`)

A partir de una explicación corta (máximo 1.000 caracteres), el proceso del panel (no un agente) hace
**una** llamada:

```text
claude -p --output-format json --tools "" --no-session-persistence --setting-sources project,local
       --strict-mcp-config --disable-slash-commands --no-chrome --model haiku --effort low
       --system-prompt <fijo>
```

- Se ejecuta en una carpeta temporal vacía, así que no hay `CLAUDE.md` del proyecto ni ajustes de
  proyecto. Las fuentes `project,local` dejan fuera `~/.claude/settings.json` y el `CLAUDE.md` de usuario.
  No se usa `--bare` porque exige `ANTHROPIC_API_KEY` y no sirve con la sesión de suscripción.
- El entorno no lleva las variables de una sesión padre de Claude Code, ni `TELEGRAM_BOT_TOKEN`, ni
  ninguna `ALFRED_*`.
- El prompt de sistema es fijo. Pide solo personalidad y tono, con la estructura de la plantilla y como
  mucho unos 1.500 caracteres. Dice que permisos y seguridad se aplican fuera y que un SOUL no puede
  concederlos ni pedir saltárselos. Y deja claro que la descripción son datos, no instrucciones.
- El texto del usuario va en el mensaje (por stdin), entre `<description>` y `</description>`, con `&`,
  `<` y `>` escapados, para que no pueda cerrar el delimitador.
- Lo que vuelve se limpia (sin bloques de código, recortado a un salto de línea si se pasa del límite) y se
  valida como cualquier personalidad.
- **Flujo:** vista previa con **Accept**, **Regenerate** (otra llamada con la misma descripción), **Edit
  by hand** (enseña el borrador para copiarlo; se guarda lo que el usuario envíe) y **Cancel**. Nada se
  guarda sin Accept o sin el texto editado.
- **Coste:**
  - Una llamada por intento, cuente o falle.
  - Tope de `ALFRED_SOUL_ATTEMPTS` por usuario y hora (5 por defecto). Se guarda en memoria, así que un
    reinicio pone la cuenta a cero.
  - Si el monitor de uso está activo y su última lectura de sesión o semanal es de al menos el 90 %, avisa
    («Little plan usage left…»).
  - Modelo y esfuerzo configurables: `ALFRED_SOUL_MODEL` (por defecto `haiku`) y `ALFRED_SOUL_EFFORT`
    (por defecto `low`). Tiempo máximo: `ALFRED_SOUL_TIMEOUT`, 120 s por defecto.
- **Errores** con mensaje claro: límite alcanzado, sin red, Claude Code sin sesión iniciada, binario no
  disponible, tiempo agotado y borrador inválido. Tras un error se vuelve a pedir la descripción.
- **Log:** solo metadatos (usuario, bot, resultado, duración, caracteres de entrada y de salida); nunca el
  texto.
- **Sin probar contra la CLI real:** los tests usan un Claude simulado. Conviene una prueba manual con la
  CLI instalada antes de desplegar, sobre todo de `--tools ""` con `--setting-sources project,local`.

## 6. Cómo recrear el asistente personal como un bot más (decisión abierta)

No se ha creado ningún bot ni se ha movido nada. Si el usuario quiere recuperar a «Alfred», sería un bot
más de los Consiglieri:

1. **Crearlo** desde el menú: nombre (por ejemplo «Alfred»), tarea («asistente personal general: agenda,
   recordatorios, recados, búsquedas…»), personalidad (pegar el `SOUL.md` antiguo, o describirla) y
   modelo.
2. **Preset.** El agente principal corría con `bypassPermissions`, sin sandbox y con acceso total. Como
   bot conviene empezar por **Assistant** o **Power**, y abrir permisos sueltos según haga falta:
   `browser`, `contact` si debe escribir a otros chats, `usage` si debe leer el uso del plan, `bots` si
   debe pedir bots nuevos. **Full access** solo si se quiere exactamente lo de antes, con sus riesgos.
3. **Acceso entre agentes:** no por defecto. Si este bot debe consultar lo que saben los demás bots del
   usuario, se concede desde Permisos, con aviso y auditoría. Aun así no abre el home antiguo.
4. **Memoria a mover**, a mano y con el bot en pausa:
   - `~/.alfred/memory/` (el índice y las carpetas de temas) a `~/.alfred-bots/<id>/memory/`. Conviene
     revisar el índice: lo general para todos los bots encaja mejor en la memoria compartida (`/shared`).
   - La memoria automática de Claude Code en `~/.claude/projects/-Users-…--alfred/memory/` a la del
     proyecto del bot. Es opcional y se puede leer y copiar a mano.
   - Las transcripciones `*.jsonl` no se pueden reanudar en otra carpeta: el bot empieza conversación nueva.
5. **Extensiones:** copiar las útiles de `~/.alfred/extensions/` a `extensions/` del bot y pasarle
   `alfred ext check`. Hooks, monitores y LSP no se cargan en un bot, y los servidores MCP solo con el
   permiso `mcp`.
6. **Recordatorios:** los del bot 0 siguen entregándose desde el panel (§3). Si deben pasar a ser del bot
   nuevo, se cancelan en Old reminders y se crean de nuevo hablando con el bot. Se podría añadir un
   comando para «adoptar» recordatorios antiguos en un bot si se quiere.
7. **Después:** decidir si se archiva o borra lo que queda del antiguo agente en `~/.alfred` (sus ficheros
   y su `.git`). Nada lo necesita ya.

## 7. Gestión desde el menú

Todo lo que se gestionaba con comandos de texto o a mano también está en el menú:

| Qué | Menú | Atajo de texto |
|---|---|---|
| Crear | Create bot (nombre, tarea, personalidad, modelo, esfuerzo, permisos) | `/bot new` |
| Configurar | Mis bots → un bot: Model, Effort, Task; Folders and rules: carpeta de trabajo, carpetas extra, reglas allow/deny | `/bot set`, `/bot perm` |
| Permisos y presets | Permissions: presets, Customize (interruptores, shell, sandbox, modo) | `/bot perm <bot> <preset>` |
| Acceso entre agentes | Permissions → Cross-agent access, con aviso | `/bot perm <bot> cross on\|off` |
| Auditoría | Inicio → Audit | `/bot audit` |
| Pausar, reanudar, destruir | Pause / Resume / Destroy (destruir pide confirmación) | `/bot pause\|resume\|destroy` |
| Memoria compartida | Inicio → Shared memory: temas, añadir, borrar un hecho, borrar un tema | `/shared` |
| Permiso del monitor de uso | Permissions → Customize → Plan usage and alerts | `/bot perm <bot> usage on` |
| Alertas de uso | Mis bots → un bot → Usage alerts: lista, estado del monitor, borrar | (las crea el bot con `alfred usage-alert`) |
| Recordatorios de cada bot | Mis bots → un bot → Reminders: lista en todos sus chats, cancelar | (los crea el bot con `alfred schedule`) |
| Recordatorios antiguos del bot principal | Inicio → Old reminders | — |
| Personalidad | Mis bots → un bot → Personality; Inicio → Base personality | `/bot soul`, `/bot base-soul` |

Quedan solo como texto, a propósito:

- Los modos `plan` y `default`: con nadie que responda, `plan` nunca actúa y `default` lo deniega todo.
- Generar la personalidad, que se hace solo desde el menú porque necesita la vista previa.

## 8. Varios usuarios: cada uno sus bots

- `BotRegistry.list(owner_id=…)`, `find(…, owner_id=…)` y `get_owned(…)` filtran por propietario.
  `/bots`, `/status`, cada `/bot <acción> <bot>`, la lista del menú y cada acción del menú
  (`Menus._record`) solo llegan a bots propios. El bot de otro usuario responde «Which bot?», así que ni
  se confirma que exista.
- Un botón del menú lleva solo el id del bot. Se comprueba el propietario al pulsarlo, sin fiarse del
  botón. Una sesión de menú es del usuario que la abrió. Un botón de confirmación (destruir, conceder acceso
  entre agentes) solo vale para quien lo pidió y además se vuelve a comprobar el propietario.
- Un bot solo atiende a su propietario, también en `/start`, que además no registra el chat de otro.
- El acceso entre agentes solo abre los demás bots del mismo propietario. Los de otro usuario y los
  directorios sin dueño siguen cerrados.
- Un bot con el permiso `bots` solo lista y cambia bots de su propietario (`alfred bots list|set`).
- La auditoría guarda el propietario del bot en cada entrada y cada usuario ve solo la de sus bots. Las
  entradas antiguas sin propietario se atribuyen al dueño actual del bot.
- `tests/test_owners.py` lo prueba con dos usuarios permitidos: comandos, menú con botones falsificados,
  confirmaciones, chat de un bot ajeno, acceso entre agentes, auditoría y `alfred bots`.

## 9. Decisiones abiertas para el usuario

1. **Memoria compartida con varios usuarios.** Hoy hay una sola: todo usuario permitido la lee y la
   escribe desde el panel, y todos los bots de todos los usuarios la leen. ¿Una global, una por usuario
   (por ejemplo `~/.alfred-shared/<user id>/memory`) o una global más una por usuario?
2. **Panel de auditoría con varios usuarios.** Ahora cada usuario ve solo la de sus bots. ¿Debe haber un
   administrador (por ejemplo el primero de `ALFRED_ALLOWED_USERS`) que vea la auditoría global?
3. **Personalidad base con varios usuarios.** Hoy es una sola y cualquier usuario permitido puede
   cambiarla, lo que afecta a los bots nuevos de los demás. ¿Global, por usuario o solo un administrador?
4. **Monitor de uso.** Mide una sola cuenta de Claude, la de la máquina, compartida por todos los bots de
   todos los usuarios. Sus avisos van a `ALFRED_USAGE_CHAT_ID`. ¿Basta así?
5. **Recrear a Alfred como bot** (§6): si se hace, con qué preset, qué memoria se mueve y qué pasa
   después con lo que queda del antiguo agente en `~/.alfred`.
6. **Tope de borradores en memoria:** ¿persistirlo en `state/` para que un reinicio no lo ponga a cero?
7. **«Adoptar» recordatorios antiguos** en un bot con un botón, en lugar de cancelarlos y crearlos de
   nuevo.

## Pruebas

`PYTHONPATH=$PWD:$PWD/tests python -m unittest discover -s tests`, con el paquete importado desde este
worktree. Tests nuevos o rehechos:

- `test_panel`: ninguna sesión de Claude para el chat del panel. Mensajes, comandos de bot y tareas
  antiguas del bot 0 no lanzan ningún proceso; `Agent` y `TelegramBot` no se pueden construir para el
  panel.
- `test_agent`, `test_telegram`, `test_cli`, `test_extensions`: ahora con un bot registrado
  (`Sandbox.add_bot`).
- `test_home`: el home solo tiene `bin/` y `state/`, y un home antiguo queda intacto.
- `test_owners`: dos usuarios.
- `test_menus`: recordatorios, recordatorios antiguos, alertas, carpetas y reglas, auditoría, borrar un
  hecho compartido.
- `test_soul`: dónde vive, base, permiso, reapertura con la misma conversación, menú, atajos y
  propietario.
- `test_soul_writer`: banderas de la llamada, prompt delimitado y escapado, carpeta vacía y entorno
  limpio, tope por hora, errores, log sin texto, aviso de cuota, y en el menú el flujo completo,
  Regenerar, Cancelar, Editar a mano y propietario.
