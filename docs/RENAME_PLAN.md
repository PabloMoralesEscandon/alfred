# Plan de cambio de nombre: Alfred → Consiglieri

Estado (2026-11-27): **fases 0 a 3 hechas y el código de la fase 4 listo** en la rama `rename-consiglieri`
(desde `master`), sin fusionar ni publicar. Falta lo que solo puede hacer el usuario: fusionar, ejecutar la
migración en vivo ([pasos exactos](#migración-en-vivo-pasos-exactos)), retirar la compatibilidad (fase 4,
final) y la fase 5. `scripts/check_old_name.sh -v` sale limpio: lo que queda del nombre viejo está marcado
`rename-compat` o en `.rename-allow`, cada excepción con su motivo.

**Corrección posterior a la fase 1:** el usuario decidió que no haya agente principal. Alfred ya no es un
personaje dentro de Consiglieri: el bot base es solo el panel, y cada agente es un bot de un usuario.
Lo que la fase 1 dejó como «personaje Alfred» se ha corregido (detalles en
[PANEL_ONLY.md](PANEL_ONLY.md)). Esa parte de este documento se ha actualizado en consecuencia.
Fecha del análisis: 2026-11-12, sobre el commit `2fd0e0a`.

## Decisión

El nombre es **Consiglieri**, en plural y a propósito: es un conjunto de agentes, no uno solo.

| Elemento | Forma nueva | Forma vieja |
|---|---|---|
| Proyecto, repositorio | Consiglieri, `consiglieri` | Alfred, `alfred` |
| Paquete Python y distribución | `consiglieri` | `alfred` |
| Comando | `consiglieri` (con `alfred` como alias temporal) | `alfred` |
| Variables de entorno | `CONSIGLIERI_*` (se leen `ALFRED_*` si falta la nueva) | `ALFRED_*` |
| Home y rutas derivadas | `~/.consiglieri`, `~/.consiglieri-bots`, `~/.consiglieri-shared` | `~/.alfred*` |
| Servicio | `com.consiglieri.bot` / `consiglieri.service` | `com.alfred.bot` / `alfred.service` |
| Prefijo de las skills base | `consiglieri:<skill>` | `alfred:<skill>` |
| Usernames de bot de Telegram | p. ej. `consiglieri_panel_bot` (ver §2) | — |

**¿Reservar también `consigliere` (singular) como alias?** No. Cada alias del comando tiene que
repetirse en las reglas de permisos de cada bot (`Bash(<cmd> run)` denegado, `Bash(<cmd> *)`
permitido...), así que un alias más es un sitio más donde puede escaparse un permiso, y el nombre
en GitHub (`consigliere`) ya es de una organización ajena. Si algún día hace falta, se añade.

## Hallazgos que condicionan todo el plan

1. **El servicio corre desde el checkout principal.** (Corregido el 2026-11-27: antes salía del worktree
   `refactor`.) El LaunchAgent `com.alfred.bot` ejecuta `/Users/…/orca/alfred/venv/bin/python3 -m alfred run`
   con `WorkingDirectory` y `PYTHONPATH` = `/Users/…/orca/alfred` y `ALFRED_HOME=~/.alfred`. Esta rama no lo
   afecta hasta que se fusione en `master`, se haga `git pull` en ese checkout **y** el proceso se reinicie.
   Como `PYTHONPATH` apunta al checkout, tras el pull se importan `consiglieri` y el paquete de
   compatibilidad `alfred` sin reinstalar nada (comprobado en una simulación con la instalación editable
   vieja). Pero el proceso en marcha tiene el código viejo en memoria y algunos módulos se importan tarde:
   hay que reiniciarlo justo después del pull.
2. **El venv del servicio tiene una instalación editable que apunta al directorio `alfred/`**
   (`__editable___alfred_0_2_0_finder.py`). Si se renombra la carpeta sin dejar un paquete
   `alfred` de compatibilidad, el siguiente arranque falla y launchd entra en bucle de reinicios. El script
   `venv/bin/alfred` de ese venv hace `from alfred.cli import main` sin `PYTHONPATH`, así que tras el pull
   solo funciona después de `pip install -e .` (que instala la distribución `consiglieri`).
3. **`config.home` se resuelve con `Path.resolve()`** cuando viene de `ALFRED_HOME`, y de él se
   derivan `bots_home` (`<home>-bots`) y `shared_dir` (`<home>-shared`). Un enlace
   `~/.alfred → ~/.consiglieri` hace que las rutas derivadas pasen a ser `~/.consiglieri-bots` y
   `~/.consiglieri-shared`: las tres carpetas deben moverse juntas. Sin `ALFRED_HOME`,
   `DEFAULT_HOME` no se resuelve y el comportamiento es distinto: hay que unificarlo antes de la fase 4.
4. **Claude Code guarda las conversaciones por ruta**: `~/.claude/projects/-Users-…--alfred` (agente
   principal) y una carpeta por bot (`…--alfred-bots-<id>`). Al mover el home, `--resume` deja de
   encontrar las sesiones y se pierde la memoria automática de Claude Code de ese proyecto. El código
   lo tolera (`session_lost` abre una sesión nueva), pero se pierde contexto: la migración debe
   mover también esas carpetas.
5. **Las reglas de permisos de los bots llevan el nombre del comando y rutas absolutas**:
   `Bash(alfred *)` permitido, `Bash(alfred run)`, `Bash(alfred service *)`… denegados,
   `excludedCommands` del sandbox, y `Read(<home>/**)` denegado. Se regeneran en
   `claude-settings.json` en cada turno. Si el comando nuevo existe y las reglas solo nombran el
   viejo, un bot con `shell: full` podría ejecutar `consiglieri service …`. **Es el riesgo de
   seguridad principal** y por eso las reglas deben cubrir ambos nombres mientras exista el alias.
   Lo mismo con las rutas: mientras exista el enlace `~/.alfred`, la denegación debe cubrir la ruta
   real y la del enlace.
6. **Hay datos persistidos con el nombre**: el valor de permiso `shell: "alfred"` en cada
   `state/bots/<id>/bot.json`; las claves `ALFRED_*` de los ajustes por bot; `ALFRED_*` en
   `~/.alfred/.env`; la etiqueta del servicio en `~/Library/LaunchAgents`; el lanzador
   `~/.alfred/bin/alfred` (regenerado en cada arranque); el convenio de secretos del llavero
   `alfred-<extensión>-<clave>`; la identidad git `Alfred <alfred@localhost>` de los autoguardados
   del home.
7. **`SOUL.md` ya existe en el home y dice «Your name is Alfred.»** Era la personalidad del agente
   principal, que ya no existe. Nada la lee ni la reescribe. La plantilla `templates/SOUL.md` es ahora
   la personalidad base de los bots, sin nombre (ver [PANEL_ONLY.md](PANEL_ONLY.md) §5).
8. (Actualizado el 2026-11-27.) Ahora **sí existen** `~/.alfred-bots` (cuatro bots, con sus carpetas en
   `~/.claude/projects/-Users-…--alfred-bots-<id>`) y `~/.alfred-shared`, además de la instancia de pruebas
   (`~/.alfred-test`, `~/.alfred-test-bots`, `~/.alfred-test.env`, `~/.alfred-test.old-code`). Las
   credenciales viven en `~/.alfred/state/credentials` (bases `.kdbx`, `master.key`, `identities.json`, los
   `.ini` de KeePassXC) y los navegadores de los bots en `~/.alfred/state/browsers`. Las identidades de
   KeePassXC se llaman `alfred-<id>` dentro de las bases y en la extensión de cada navegador.

## 1. Inventario

Búsqueda sin distinguir mayúsculas de `alfred`, sin `.git`, `venv`, `*.egg-info` ni `__pycache__`:
**1.312 apariciones en 1.083 líneas de 70 ficheros**, más la carpeta del paquete `alfred/` y el
ejecutable `alfred/plugin/bin/alfred-wait-agent`.

### Por zona del repo

| Zona | `Alfred` (nombre) | `alfred` (código, comando, rutas) | `ALFRED` (variables) |
|---|---:|---:|---:|
| Código (`alfred/*.py`, `backends/`) | 115 | 110 | 98 |
| Plantillas (`alfred/templates/`) | 14 | 28 | 0 |
| Plugin base (`alfred/plugin/`) | 12 | 39 | 1 |
| Tests | 25 | 167 | 123 |
| `docs/` | 125 | 227 | 48 |
| `README.md` | 54 | 57 | 19 |
| `.env.example` | 9 | 8 | 26 |
| `pyproject.toml` | 0 | 6 | 0 |
| `.gitignore` | 0 | 0 | 1 |

### Por categoría, con riesgo para el servicio real

Riesgo: **alto** = puede tumbar el servicio, perder datos o abrir un permiso; **medio** = rompe una
función o algo fuera del repo que hay que rehacer; **bajo** = solo texto.

| # | Categoría | Dónde | Recuento | Riesgo |
|---|---|---|---:|---|
| A | Paquete Python | carpeta `alfred/`, 59 `import alfred…` en tests, 69 imports relativos en el paquete (no cambian), 16 cadenas `"alfred.x"` en `mock.patch`, `alfred.__file__` | 1 carpeta, ~80 refs | **Alto** (hallazgos 1 y 2) |
| B | Ejecución como módulo | `-m alfred` en `service.program()`, en el lanzador `home._write_launcher` y en el README | 3 | **Alto**: el plist y `~/.alfred/bin/alfred` llevan `-m alfred` |
| C | Comando y subcomandos | `prog="alfred"`, `[project.scripts] alfred`, errores `alfred: …`; subcomandos `run init service usage usage-alert browser-setup chat skills send event ext bots chrome screenshot schedule`; 226 menciones `alfred <subcomando>` | 226 | **Alto** en plantillas y reglas (los agentes lo ejecutan); bajo en docs |
| D | Reglas de permisos de bots | `bots.py`: `ALWAYS_DENIED`, `UNSANDBOXED_ALFRED`, `USAGE_RULES`, `USAGE_UNSANDBOXED`, `Bash(alfred *)`, `Bash(alfred schedule *)`, `Bash(alfred bots *)`; valor `shell: alfred` | 20 reglas + valor persistido | **Alto** (seguridad, hallazgo 5) |
| E | Variables de entorno | 43 nombres `ALFRED_*` distintos más dos prefijos dinámicos (`ALFRED_CLAUDE`, `ALFRED_CODEX` en `bots.py`; `ALFRED_%s_BIN` en `tests/fakes.py`) | 314 | **Alto**: `~/.alfred/.env`, el plist y los ajustes por bot usan los nombres viejos |
| F | Home y rutas | `~/.alfred` (40), `~/.alfred-bots` (13), `~/.alfred-shared` (5), `~/.alfred-test*` (14), derivación `"%s-bots" % home.name` | 72 | **Alto** (hallazgos 3, 4, 8) |
| G | Servicio | `LABEL = "com.alfred.bot"`, `SYSTEMD_UNIT = "alfred.service"`, `state/alfred.log`, `Description=Alfred Telegram agent` | 6 | **Alto**: dos servicios con el mismo token se pelean por `getUpdates` |
| H | Plugin y skills base | `plugin.json` `name: "alfred"` → skills `alfred:browser`, `alfred:extensions`…; `BASE_NAME = "alfred"`; `alfred-wait-agent`; secretos `alfred-<name>-<key>`; 31 menciones `alfred:` | 31 + 1 ejecutable | **Medio**: cambia el nombre con que el agente ve sus skills |
| I | Plantillas regeneradas | `CLAUDE.md` (15), `AGENTS.md` (2), `BOT.md` (13) se reescriben en cada arranque; `SOUL.md` (1) y `MEMORY.md` solo si faltan | 31 líneas | **Medio**: es lo que lee el agente en cada turno |
| J | Textos al usuario | `telegram_bot.py` (panel, `/start`, `/help`, avisos), `menus.py`, `bots.py` (resúmenes de permisos), `shared_memory.py`, `usage_monitor.py`, `orca.py`, `cli.py`, `presets.py` | ~90 | Bajo (algunos tests comparan texto) |
| K | Comentarios y docstrings | todo el paquete | ~60 | Bajo |
| L | Logs e identidad | `getLogger("alfred.*")` (6), git `user.name=Alfred`, `alfred@localhost`, `"alfred: bootstrap"` | 9 | Bajo (solo si alguien filtra logs por nombre) |
| M | Documentación | `README.md`, `docs/*.md`, `.env.example` | ~600 | Bajo |
| N | Tests | 23 ficheros | 315 | Bajo, pero cambian con A–G |
| O | Metadatos | `pyproject.toml` (`name`, `scripts`, `packages`, `package-data`), `__init__.py` | 6 | **Alto** en fase 2 (instalación editable) |
| P | Fuera del repo | repo `github.com/<usuario>/alfred`; carpeta de Orca `orca/workspaces/alfred/` (y el venv del servicio dentro, con rutas absolutas en los shebangs); workspace de Orca registrado con la ruta del home; perfil de Chrome `ALFRED_CHROME_PROFILE=Alfred`; nombre y username del bot de Telegram principal y del panel; scripts externos que llamen a `~/.alfred/bin/alfred event` | — | **Medio** |

El script del anexo (`scripts/check_old_name.sh`) reproduce estos recuentos y comprueba la máquina
en solo lectura con `--runtime`.

## 2. Disponibilidad de «consiglieri»

Consultas públicas de solo lectura del 2026-11-12. No se ha creado nada.

| Sitio | Resultado |
|---|---|
| GitHub, usuario `consiglieri` | **Ocupado** por una cuenta personal (`Consiglieri`, 4 repos, desde 2016). No afecta: el repo será `<usuario>/consiglieri`. |
| GitHub, repos | 22 repos con ese nombre, ninguno con estrellas ni relacionado. |
| PyPI `consiglieri` | **Libre** (404 en `/pypi/consiglieri/json` y en `/simple/consiglieri/`). PyPI puede rechazar nombres parecidos a otros, pero no hay uno cercano. |
| npm `consiglieri` | **Libre** (404). |
| Telegram `consiglieri_bot` | **Ocupado** (bot «Tom Hagen»). |
| Telegram `consiglieribot` | **Ocupado** (bot «Your assistant»). |
| Telegram `consiglieri` | Cuenta de usuario existente. |
| Telegram `consiglieri_panel_bot`, `consiglieri_hq_bot`, `consiglieri_agent_bot`, `consiglieri_ai_bot` | Sin página pública: **probablemente libres**. Solo BotFather lo confirma al crearlo. |

**Marcas**: Consiglieri Corp. tiene registrada en EE. UU. la marca denominativa CONSIGLIERI (2021,
consultoría de marketing) y otra solicitud de 2025 para software de IA de marketing. Para una
herramienta personal no comercial no choca; si algún día se publica o se vende, hay que revisarlo
(clases 9 y 42). Por cultura popular el nombre evoca *El padrino* (el consejero de la familia):
es el chiste buscado, pero conviene saberlo para un repo público.

Fuentes: [Consiglieri Corp. en Justia](https://trademark.justia.com/owners/consiglieri-corp-4858887),
[Consiglieri en Crunchbase](https://www.crunchbase.com/organization/consiglieri-e95e).

## 3. Plan de migración por fases

Reglas para todas las fases: cada fase en commits pequeños en esta rama; `PYTHONPATH=$PWD:$PWD/tests
python -m unittest discover -s tests` en verde comprobando que `alfred.__file__` (luego
`consiglieri.__file__`) apunta a este worktree; se fusiona en `refactor` solo cuando el usuario lo
confirme; el servicio se reinicia solo a propósito y se vigila `~/.alfred/state/alfred.log`.
Antes de las fases 2 a 4: copia de seguridad (`tar` de `~/.alfred` y de `~/.claude/projects/*alfred*`,
copia del plist).

### Fase 1: nombre visible, documentación y textos

- **Qué**: README, `docs/`, `.env.example` (comentarios), textos del panel y de los bots,
  comentarios y docstrings, `description` de `pyproject.toml`, `displayName` del plugin, plantillas
  del repo en lo que es nombre del proyecto. **Sin tocar** paquete, comando, variables, rutas,
  servicio, nombre del plugin (`alfred:*`), `SOUL.md` ni los ficheros generados del home.
- **Convención**: *Consiglieri* es el proyecto y el conjunto de agentes. Los textos que nombran el
  sistema («el panel de control de Alfred», «los bots que creó Alfred», «el home de Alfred», «el
  código de Alfred») pasan a Consiglieri. (Al principio la fase 1 dejó «Alfred» como nombre del agente
  principal; al eliminarse el agente principal, esos textos se cambiaron por «tus otros bots» y
  similares.)
- **Riesgo**: bajo. El código no cambia de comportamiento; los tests que comparan texto se ajustan.
- **Prueba**: suite completa; `grep -rn '\bAlfred\b'` revisado a mano (solo deben quedar el
  personaje y los nombres técnicos); `scripts/check_old_name.sh` para ver el recuento.
- **Reversión**: `git revert` de los commits de la fase. Al usuario le llega cuando se fusione en
  `refactor` y se reinicie el servicio.
- **Hecho** (seis commits, uno por tipo de cambio): textos al usuario (panel, `/help` de los bots,
  ayuda y errores del CLI, resúmenes de permisos, avisos de memoria compartida y del monitor de uso);
  plantillas del repo (`CLAUDE.md`, `AGENTS.md`, `BOT.md`) y plugin base (`displayName`,
  `description`, `author`, skill `extensions`); comentarios y docstrings; README, `.env.example` y
  `description` de `pyproject.toml`; documentos de diseño; notas de investigación (commit aparte,
  ver decisión abierta 4). La palabra `Alfred` pasa de 321 líneas a 34, y tras quitar el agente
  principal a menos: quedan el ejemplo de perfil de Chrome, la identidad git de los autoguardados, el
  nombre anterior del proyecto en el README y en las docs, y los fixtures `AlfredPanelBot`. `scripts/check_old_name.sh` baja de 1.083 a 836 líneas: lo que queda es comando,
  variables, rutas, paquete y skills, de las fases 2 a 4. Los 289 tests pasan, con el paquete
  importado desde este worktree.
- **Sin tocar, a propósito**: el perfil de Chrome de ejemplo, `user.name=Alfred` de los
  autoguardados, los fixtures `AlfredPanelBot` de los tests, y todo lo que es código: `alfred`,
  `ALFRED_*`, `~/.alfred*`, `$alfred_home`, `alfred:<skill>`, servicio. Ya no existen la plantilla
  `CLAUDE.md` del agente principal ni «I'm Alfred»; `SOUL.md` es otra cosa (ver hallazgo 7).

### Fase 2: paquete y comando, con `alfred` como alias

- **Qué**:
  - `git mv alfred consiglieri`; `pyproject.toml`: `name = "consiglieri"`, `packages`,
    `package-data`; `[project.scripts]` con `consiglieri` **y** `alfred` apuntando a
    `consiglieri.cli:main`.
  - Paquete de compatibilidad `alfred/` con `__init__.py` (reexporta) y `__main__.py` (llama a
    `consiglieri.cli.main`), para que `python -m alfred run` del plist actual y del lanzador viejo
    sigan funcionando. Se borra en la fase 4, después de reinstalar el servicio.
  - El lanzador del home escribe `bin/consiglieri` y `bin/alfred` (el mismo script, con `-m consiglieri`). (Hecho de otro modo: `bin/alfred` solo se reescribe mientras exista; ver decisión 11.)
  - `prog="consiglieri"` y mensajes `consiglieri: …`. (Ya no hay `--bot main|alfred`: no hay agente principal.)
  - **Reglas de permisos para los dos nombres**: `ALWAYS_DENIED`, `USAGE_RULES`, `excludedCommands`,
    `Bash(<cmd> *)`, `Bash(<cmd> schedule *)`, `Bash(<cmd> bots *)` generadas desde una lista
    `COMMAND_NAMES = ("consiglieri", "alfred")`. Test parametrizado: para cada nombre, `run`,
    `service`, `init`, `chat` y `browser-setup` están denegados con cualquier preset.
  - Valor de permiso `shell`: se acepta `alfred` y `consiglieri` al leer, se escribe `consiglieri`,
    y el panel muestra «only consiglieri».
  - Extensiones: la lista de comandos que una extensión no puede sobrescribir incluye ambos.
  - Plugin base: `name: "consiglieri"` → skills `consiglieri:<skill>`; `BASE_NAME`;
    `consiglieri-wait-agent` (más `alfred-wait-agent` como enlace); plantillas y skills con el
    comando nuevo. Los secretos del llavero se buscan como `consiglieri-…` y, si no están, `alfred-…`.
  - Loggers `consiglieri.*`.
- **Riesgo**: **alto**. Puntos de fallo: la instalación editable del venv del servicio (hallazgo 2),
  un permiso que solo cubre un nombre (hallazgo 5), una plantilla que menciona un comando que el
  bot no tiene permitido.
- **Prueba**: suite completa; en un venv nuevo `pip install -e .` y comprobar `consiglieri --version`,
  `alfred --version`, `python -m alfred --version`, `python -m consiglieri --version`; arrancar la
  instancia de pruebas (`ALFRED_HOME=~/.alfred-test`, su propio token) y pedir a un bot `alfred send`
  y `consiglieri send`; comprobar que `consiglieri service status` se deniega a un bot con
  `shell: full`. Al desplegar: `pip install -e .` en el venv de `refactor` **antes** de reiniciar.
- **Reversión**: `git revert`; en el venv, `pip install -e .` otra vez; reiniciar. Gracias al paquete
  de compatibilidad, el plist no se toca en esta fase.
- **Hecho** (rama `rename-consiglieri`), con estos cambios respecto a lo previsto:
  - `pyproject.toml` declara solo `consiglieri` (paquete, distribución y comando). **No hay alias `alfred`
    como script de consola**: no hace falta para el servicio, y el venv viejo conserva el suyo hasta
    `pip uninstall alfred`. El paquete de compatibilidad `alfred/` tiene solo `__init__`, `__main__` y `cli`
    (para `python -m alfred` del plist y del lanzador viejo, y para el script de consola viejo), y no se
    empaqueta: se importa por `PYTHONPATH` o por la instalación editable vieja.
  - Las constantes `COMMAND`, `LEGACY_COMMAND` y `COMMAND_NAMES` están en `config.py`; `bots.each_command`
    escribe cada regla para los dos nombres: permitidas, denegadas **y** `excludedCommands`, de modo que el
    alias se comporta exactamente igual que el nombre nuevo. Además se han cerrado formas que antes no
    estaban: `service` y `init` sin argumentos o con ellos, `browser-setup` sin argumentos y `migrate`.
    `tests/test_rename_compat.py` recorre todos los presets y plantillas y cada interruptor en los dos
    sentidos, y comprueba que toda regla con el comando existe con los dos nombres y que los subcomandos del
    usuario (`run`, `service`, `init`, `chat`, `browser-setup`, `migrate`, `browser install`,
    `creds setup|add|remove|grant|revoke|start|stop`) se deniegan con los dos. Quitar el nombre viejo de una
    sola lista hace fallar 78 casos (comprobado).
  - Las comprobaciones del propio comando que rechazan órdenes del usuario en el turno de un bot
    (`creds setup…`, `browser install`, `migrate`) ven un id o una clave de bot con **cualquiera** de los dos
    nombres de variable (`config.in_bot_turn`); un nombre nuevo vacío no oculta el viejo.
  - Las identidades de KeePassXC nuevas se llaman `consiglieri-<id>`; las ya creadas conservan su nombre
    (guardado en `identities.json`), porque está en cada base concedida y en la extensión del navegador:
    renombrarlas desharía todas las concesiones.
  - Secretos del llavero: las extensiones nuevas usan `consiglieri-<ext>-<clave>`; las viejas siguen
    leyendo los suyos (es código de cada extensión, Consiglieri no los busca).
  - Plugin `consiglieri` (skills `consiglieri:<skill>`), `consiglieri-wait-agent` (sin enlace con el
    nombre viejo: lo carga el plugin del proceso nuevo), loggers `consiglieri.*`, identidad git de los
    autoguardados `Consiglieri <consiglieri@localhost>` (decisión abierta 5, aplicada).

### Fase 3: variables de entorno con compatibilidad hacia atrás

- **Qué**: `Config.get("CONSIGLIERI_X")` busca, en este orden, ajustes del bot, entorno y `.env`,
  primero con el nombre nuevo y después con `ALFRED_X`. Un solo sitio (`config.py`) traduce los
  nombres; el resto del código usa solo los nuevos. `Config.load` hace lo mismo con `HOME` y
  `ENV_FILE`. Si están las dos con valores distintos, gana la nueva y se avisa una vez en el log.
  - El entorno de los procesos del agente lleva las dos (`CONSIGLIERI_CHAT_ID` y `ALFRED_CHAT_ID`…)
    durante la transición, porque scripts de extensiones pueden leer las viejas.
  - Los ajustes por bot se escriben con claves nuevas y se leen ambas.
  - `consiglieri doctor` (o un aviso al arrancar) lista las `ALFRED_*` que siguen en `.env`.
  - `.env.example` y README con los nombres nuevos y una nota sobre los viejos.
- **Riesgo**: medio. El `.env` real no se toca y sigue funcionando; el riesgo es olvidar una
  variable leída fuera de `Config` (`cli.py` lee `ALFRED_CHAT_ID`, `ALFRED_BOT_ID`,
  `ALFRED_THREAD_ID`, `ALFRED_USER_ID`, `ALFRED_USERNAME` de `os.environ`; también
  `alfred-wait-agent` lee `ALFRED_ORCA_BIN`).
- **Prueba**: test parametrizado por variable: solo vieja, solo nueva, ambas (gana la nueva).
  `grep -rn 'environ.get("ALFRED_' consiglieri` debe quedar vacío fuera de `config.py`. Arranque de
  la instancia de pruebas con un `.env` que solo tiene `ALFRED_*`.
- **Reversión**: `git revert`; como el `.env` no cambió, no hay datos que deshacer.
- **Hecho.** `config._pick` busca en cada sitio el nombre nuevo y luego el viejo, también en
  `Config.load` (home y fichero `.env`) y en `config.env_get` para lo que el comando lee del entorno. El
  entorno del agente lleva los dos nombres (`with_legacy_names`; un `ALFRED_*` heredado del servicio nunca
  gana al nuevo). El generador de personalidad quita los dos prefijos. Los ajustes por bot nunca se
  guardaron como `ALFRED_*` (se calculan en `BotRegistry.overrides`), así que no hay nada que migrar ahí.
  No hay `consiglieri doctor`: `migrate` renombra las variables de los `.env`.

### Fase 4: rutas, home y servicio

- **Qué**:
  - Resolución del home: `CONSIGLIERI_HOME` / `ALFRED_HOME` si están; si no, `~/.consiglieri` si
    existe; si no, `~/.alfred` si existe (instalaciones viejas siguen igual); si no, `~/.consiglieri`.
    Siempre resuelto con `resolve()`, haya variable o no (hallazgo 3).
  - Comando explícito `consiglieri migrate-home [--dry-run]`, nunca automático al arrancar, que con
    el servicio parado: mueve `~/.alfred`, `~/.alfred-bots` y `~/.alfred-shared` a sus nombres nuevos;
    mueve las carpetas de `~/.claude/projects` del home y de cada bot a la clave de la ruta nueva
    (hallazgo 4); reescribe rutas absolutas en `bot.json` y ajustes por bot; deja
    `~/.alfred → ~/.consiglieri` como enlace para scripts externos; y vuelve a registrar el
    workspace de Orca (`browser-setup`).
  - Denegaciones de lectura para la ruta real y la del enlace mientras el enlace exista.
  - Servicio: `consiglieri service install` desinstala primero `com.alfred.bot` (nunca dos servicios
    con el mismo token), instala `com.consiglieri.bot` con `-m consiglieri` y `CONSIGLIERI_HOME`.
    Log en `state/consiglieri.log`.
  - Después, borrar el paquete de compatibilidad `alfred/` (el alias de consola `alfred` puede seguir).
  - La instancia de pruebas (`~/.alfred-test*`) se migra igual o se borra, a elección del usuario.
- **Riesgo**: **alto**. Es la única fase que toca datos reales: home, sesiones de Claude Code,
  tokens de bots, servicio.
- **Prueba**: primero con `~/.alfred-test` (copia, no el real): `--dry-run`, migración, arranque,
  comprobar que un chat existente sigue la conversación (resume), que un recordatorio programado se
  entrega, que `alfred event` vía el enlace funciona y que un bot no puede leer el home por ninguna
  de las dos rutas. Después, el real, con copia de seguridad hecha y el servicio parado.
- **Reversión**: parar el servicio nuevo (`consiglieri service uninstall`); mover a mano las tres carpetas
  y las de `~/.claude/projects` de vuelta; restaurar el plist copiado y `launchctl bootstrap gui/$(id -u)
  ~/Library/LaunchAgents/com.alfred.bot.plist`; en el peor caso, restaurar el `tar`. No hay `--reverse`.
- **Código hecho; la migración en vivo, no.** Cambios respecto a lo previsto:
  - El comando es `consiglieri migrate [--dry-run] [--from <home viejo>] [--to <home nuevo>]`
    (`consiglieri/migrate.py`, `tests/test_migrate.py`, siempre sobre una carpeta de usuario falsa).
  - **No deja enlace `~/.alfred → ~/.consiglieri`**: sería una segunda ruta a todo lo que los bots tienen
    denegado (hallazgo 5) y no se ha encontrado nada externo que la use (crontab vacío). Si hiciera falta,
    se crea a mano y entonces hay que añadir la ruta del enlace a las denegaciones.
  - No reinstala el servicio ni vuelve a registrar el workspace de Orca: se niega a empezar mientras haya
    un servicio instalado con cualquiera de los dos nombres, y al acabar dice qué ejecutar
    (`service install`, `browser-setup`).
  - Se niega también si `consiglieri|alfred run` sigue en marcha, si corre su KeePassXC o el navegador de un
    bot, si existe un destino (también una carpeta de `~/.claude/projects`), en el turno de un bot o sin
    home viejo; en todos esos casos no toca nada.
  - Reescribe las rutas viejas (como componente entero: `~/.alfred` nunca casa con `~/.alfred-test`) en los
    `.json` e `.ini` de `state/` (salvo `browsers/`, que se reescribe al abrir cada navegador) y en los dos
    `.env` (el del home y el del checkout o `CONSIGLIERI_ENV_FILE`), y en esos `.env` renombra `ALFRED_X=`
    a `CONSIGLIERI_X=` sin tocar valores, salvo si ya está el nombre nuevo. No toca `scheduler.sqlite3`, los
    `.kdbx` ni las transcripciones `.jsonl` (Claude Code las busca por carpeta, no por su campo `cwd`).
  - `service install` retira siempre antes el servicio viejo; `service status|restart|uninstall|logs`
    actúan sobre el viejo mientras sea el único instalado, y `status` lo avisa.
  - Resolución del home hecha: `CONSIGLIERI_HOME`/`ALFRED_HOME`; si no, `~/.consiglieri`, o `~/.alfred`
    mientras solo exista ese; siempre con `resolve()`.

### Fase 5: repo en GitHub y carpetas locales

- **Qué**: renombrar el repo en GitHub a `consiglieri` (GitHub redirige las URLs viejas, pero hay que
  actualizar `git remote set-url origin` en cada clon y worktree). Renombrar la carpeta de Orca
  `orca/workspaces/alfred/` es aparte y **solo después** de reinstalar el servicio desde la ruta
  nueva: el plist y los shebangs del venv llevan rutas absolutas, así que el venv se recrea.
- **Riesgo**: bajo en GitHub; medio en local por el venv y el plist.
- **Prueba**: `git fetch` desde cada worktree; `consiglieri service status` tras recrear el venv.
- **Reversión**: GitHub permite volver a renombrar; el nombre viejo queda libre y redirige mientras
  no se cree otro repo con él.

### Fuera de las fases (opcional)

- Bot de Telegram principal y panel: el username no se puede cambiar sin crear un bot nuevo (token
  nuevo, chats nuevos). El nombre visible sí, en BotFather (`/setname`). Recomendación: cambiar solo
  el nombre visible del panel a «Consiglieri» y dejar el username actual.
- Perfil de Chrome `Alfred`: es un valor del usuario en `.env`; puede quedarse.

## 4. Qué cambia con «consiglieri»

- **Como comando**: 11 letras frente a 6. Se escribe a mano pocas veces (lo usan sobre todo los
  agentes), pero en la terminal pesa; el autocompletado lo resuelve. La ortografía italiana
  (`-gli-`, `-ieri`) invita a erratas (`consigliere`, `consiglieiri`, `consigleri`); un error
  da «command not found», no hace nada raro.
- **Prefijo de skills**: `consiglieri:reminders`, `consiglieri:browser`. Más largo pero claro;
  Claude Code no tiene límite práctico ahí.
- **Variables**: `CONSIGLIERI_ALLOWED_USERS`, `CONSIGLIERI_USAGE_WAKE_INTERVAL`: 5 caracteres más
  cada una. Aceptable.
- **Telegram**: `consiglieri_bot` y `consiglieribot` están cogidos; hay que usar un sufijo
  (`consiglieri_panel_bot`, `consiglieri_hq_bot`).
- **Gramática**: el plural nombra bien al conjunto («los consiglieri del usuario») pero no a un
  agente suelto: un bot no puede decir «soy Consiglieri». Por eso cada bot tiene su propio nombre;
  *Consiglieri* es el sistema y el panel.
- **Pronunciación**: *con-si-LLIE-ri* (/konsiʎˈʎɛːri/). Para hispanohablantes es natural.
- **Qué se pierde**: la referencia al mayordomo (salvo que el usuario recree a Alfred como uno de sus
  bots, ver [PANEL_ONLY.md](PANEL_ONLY.md) §6) y la brevedad del comando.

## Migración en vivo: pasos exactos

Para el usuario, en `/Users/…/orca/alfred`, cuando la rama esté fusionada en `master`. Nadie lo ha
ejecutado. Unos minutos sin bots.

**0. Copia de seguridad** (con el servicio aún en marcha da igual; mejor justo después del paso 1):

```sh
cd ~ && tar czf ~/consiglieri-backup-$(date +%F).tgz .alfred .alfred-bots .alfred-shared \
  .claude/projects/-Users-pablomoralesescandon--alfred .claude/projects/-Users-pablomoralesescandon--alfred-bots-*
cp ~/Library/LaunchAgents/com.alfred.bot.plist ~/consiglieri-backup-com.alfred.bot.plist
```

**1. Parar lo que corre, con el código viejo** (antes del pull):

```sh
cd ~/orca/alfred
venv/bin/alfred service uninstall       # para y retira com.alfred.bot
venv/bin/alfred creds stop              # su KeePassXC, si está abierto
pgrep -fl 'alfred run|consiglieri run'  # no debe salir nada
```

**2. Código nuevo:**

```sh
git pull                                 # master con el renombrado
venv/bin/pip install -e '.[browser]'     # instala la distribución consiglieri y venv/bin/consiglieri
venv/bin/consiglieri --version
```

**3. Migrar:**

```sh
venv/bin/consiglieri migrate --dry-run   # revisar la lista: mueve, reescribe; nada cambia
venv/bin/consiglieri migrate
```

Si se niega, dice por qué (un navegador abierto: `venv/bin/consiglieri browser stop --bot <id>`; un destino
que ya existe; etc.) y no ha tocado nada.

**4. Servicio nuevo y comprobaciones:**

```sh
env | grep -E '^(ALFRED|CONSIGLIERI)_'       # el shell no debe fijar ALFRED_HOME al home viejo
venv/bin/consiglieri service install         # com.consiglieri.bot, con CONSIGLIERI_HOME=~/.consiglieri
venv/bin/consiglieri service status
venv/bin/consiglieri service logs            # ~/.consiglieri/state/consiglieri.log
venv/bin/consiglieri browser-setup           # vuelve a enlazar el home con su workspace de Orca
venv/bin/consiglieri creds status            # si se usan credenciales
scripts/check_old_name.sh --runtime          # en la máquina ya no deben salir ~/.alfred* ni com.alfred.bot
```

En Telegram: escribir a un bot que tuviera conversación (debe seguirla), y mirar sus recordatorios en el
menú. Para un bot con navegador y credenciales, que inicie sesión en un sitio concedido.

Si `migrate` lista extensiones o skills de un bot que aún llaman al comando viejo («note: … still calls the
old command»), cambiar en ellas `alfred` por `consiglieri`: tras migrar, el lanzador viejo ya no existe.

**5. Retirar el paquete viejo del venv** cuando todo funcione: `venv/bin/pip uninstall -y alfred`.

**6. Instancia de pruebas** (decisión 6: se migra, no se borra), con lo suyo parado:
`CONSIGLIERI_ENV_FILE=~/.alfred-test.env venv/bin/consiglieri migrate --from ~/.alfred-test --to ~/.consiglieri-test`
(primero con `--dry-run`). Así `migrate` reescribe también ese `.env` (nombres y la ruta del home); después,
si se quiere, renombrar el fichero a `~/.consiglieri-test.env` y `~/.alfred-test.old-code` a mano.

**Reversión:** ver la fase 4.

**Después (fase 4, final):** cuando el servicio nuevo lleve un tiempo sin incidencias (decisión abierta 3),
quitar la compatibilidad: todo lo marcado `rename-compat` y lo listado en `.rename-allow` (el paquete
`alfred/`, `LEGACY_*` en `config.py`, `service.py` y `keepass.py`, `migrate.py` y sus tests). Las
identidades `alfred-<id>` de KeePassXC necesitan su lectura mientras existan bots creados antes; para
quitarla habría que volver a conceder sus credenciales.

## Lo que no se puede cambiar desde aquí

- **Usernames de los bots ya creados en Telegram** (`alfred_rnfl_*_bot`): Telegram no permite cambiar el
  username de un bot; haría falta crear bots nuevos (token y chats nuevos). No aparecen en el repo, así que
  `check_old_name.sh` no los ve. El nombre visible sí se puede cambiar en BotFather (`/setname`) o desde el
  panel. Los bots nuevos ya no heredan el nombre viejo del username del panel: se les propone
  `consiglieri_…` (decisión 12).
- El repo en GitHub y la carpeta `orca/workspaces/alfred/` (fase 5).

## Decisiones abiertas

1. ~~¿El agente principal sigue llamándose Alfred?~~ **Resuelta:** no hay agente principal. Recrear el
   asistente personal como un bot más queda como decisión abierta en [PANEL_ONLY.md](PANEL_ONLY.md) §6.
2. ¿Nombre visible del bot del panel en Telegram: «Consiglieri»? ¿Username nuevo o el actual?
3. ~~¿Cuánto tiempo vive el alias?~~ **Resuelta (2026-11-27, a juicio del agente por indicación del
   usuario):** la compatibilidad se quita cuando el servicio real lleve una semana migrado sin incidencias.
4. Documentos históricos de investigación (`docs/*_RESEARCH.md`, `docs/PLAN.md`): en la fase 1 se
   actualiza el nombre del proyecto; si se prefiere dejarlos como registro histórico, se revierte
   ese commit y se añaden a `.rename-allow`.
5. ~~¿Identidad git de los autoguardados?~~ **Resuelta:** `Consiglieri <consiglieri@localhost>`.
6. ~~¿Instancia de pruebas: migrar o borrar?~~ **Resuelta:** se migra (paso 6 de la migración en vivo);
   borrar datos no se decide desde aquí.
7. Cómo se presenta un bot: la fase 1 usa el plural como nombre común en su manual («one of the
   user's Consiglieri: a Telegram bot created for one job») y «a Consiglieri bot» en su `/help`.
   ¿Se queda así o se prefiere otra fórmula?
8. ~~Los avisos de permisos nombraban a Alfred~~ **Resuelta:** ahora dicen «your other bots».
9. Las decisiones sobre varios usuarios (memoria compartida, auditoría, personalidad base) están en
   [PANEL_ONLY.md](PANEL_ONLY.md) §9.
10. ~~¿Enlace `~/.alfred → ~/.consiglieri`?~~ **Resuelta:** no. Sería una segunda ruta a todo lo que los bots
    tienen denegado.
11. ~~¿Lanzador `bin/alfred` tras migrar?~~ **Resuelta:** no. Solo se reescribe mientras exista (homes sin
    migrar); `migrate` lo borra y nunca se vuelve a crear, y lista lo que los bots escribieron que aún lo
    llama. Las reglas siguen cubriendo los dos nombres mientras exista `LEGACY_COMMAND`, como defensa.
12. ~~¿Usernames de bots?~~ **Resuelta:** los bots existentes conservan su username (cambiarlo exige un bot
    nuevo: token y chats nuevos, y se pierde su historia). Los bots nuevos de un panel cuyo username empieza
    por el nombre viejo reciben `consiglieri_…` (`bots.username_prefix`). Queda en manos del usuario cambiar el
    nombre visible del panel en BotFather (decisión 2).

## Anexo: `scripts/check_old_name.sh`

Busca restos del nombre viejo y los clasifica. Solo lee.

```sh
scripts/check_old_name.sh                 # recuentos por categoría, variables, rutas, comandos
scripts/check_old_name.sh -v              # además, cada línea
scripts/check_old_name.sh --runtime       # además, home, servicio y proyectos de Claude Code
scripts/check_old_name.sh -n consigliere  # comprobar otro nombre (p. ej. una errata)
```

Una línea con el marcador `rename-compat` (en un comentario) se da por intencionada: un alias, la
lectura de la variable vieja, la migración. Un fichero `.rename-allow` en la raíz excluye ficheros
enteros, un patrón por línea (ahora mismo solo este plan). Sale con 1 mientras quede algo, así que sirve como comprobación final
de la fase 4. Salida en la fase 0: 1.083 líneas (405 de documentación, 287 de código, 277 de tests,
42 de configuración, 41 del plugin, 31 de plantillas), 45 nombres `ALFRED_*`, y en la máquina
`~/.alfred`, el plist `com.alfred.bot` cargado y cuatro proyectos de Claude Code con la ruta vieja.
