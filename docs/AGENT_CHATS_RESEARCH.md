# Agent chats: crear bots y chats nuevos desde Alfred

Estado: investigación (12 de noviembre de 2026). No cambia código. Fuente principal: la documentación
oficial de Telegram, consultada en esa fecha (Bot API 10.4, del 9 de noviembre de 2026). Los enlaces
están en cada sección y reunidos al final.

## Conclusión

Desde la Bot API 9.6 (3 de abril de 2026) existe una vía oficial para esto: los **bots gestionados**
(*Managed Bots*). Alfred puede activarse como "bot gestor" una sola vez en el Mini App de @BotFather.
Desde entonces puede ofrecer al usuario un botón o un enlace `t.me/newbot/...` con el nombre y el
username ya rellenos. El usuario confirma con un toque, Alfred recibe un update `managed_bot` y obtiene
el token del bot nuevo con `getManagedBotToken`, sin pasar por BotFather ni por una cuenta de usuario.

Lo que no se puede automatizar del todo, por diseño:

- **La creación siempre la confirma una persona.** Solo un usuario puede invocar `bots.createBot`. Un
  bot solo puede *pedir* la creación.
- **El primer mensaje lo envía el usuario.** Un bot no puede iniciar una conversación, así que el chat
  privado con el bot nuevo queda vinculado cuando el usuario pulsa *Start*. Alfred le pasa el enlace.
- **Un bot no puede crear grupos.** Para grupos o foros, el usuario crea el grupo y añade los bots.

Recomendación: usar bots gestionados para los agentes que necesiten identidad propia, cada uno en su
chat privado con el usuario. El token se guarda en `~/.alfred/state/` con permisos `0600`, nunca en
git. Para agentes ligeros que no necesitan un bot aparte, los **temas en chats privados** (Bot API
9.3/9.4) dan varios chats dentro del bot actual sin ningún token nuevo, y caben en el mismo diseño. No
recomiendo automatizar BotFather ni usar una cuenta de usuario (MTProto). El diseño y los pasos están
en la [sección 5](#5-recomendación-y-diseño).

## 0. Punto de partida en el código

Lo que condiciona el diseño (rama `PabloMoralesEscandon/agent-chats`):

- **Un token, un proceso.** `Config.telegram_token` lee `TELEGRAM_BOT_TOKEN` (`alfred/config.py`), y
  `telegram_bot.run` construye una sola `Application` y llama a `run_polling()`
  (`alfred/telegram_bot.py:386`).
- **Todo se indexa por `chat_id`:**
  - las sesiones (`SessionStore`, `state/sessions.json`, clave `str(chat_id)`, `alfred/agent.py:32`);
  - las colas y bloqueos del `Agent`;
  - el planificador (`scheduled_messages.chat_id INTEGER`, `alfred/scheduler.py:86`);
  - la carpeta del inbox;
  - la variable `ALFRED_CHAT_ID` que reciben el agente y `bin/alfred`.
- **`alfred send` usa siempre el token base** (`alfred/cli.py:133`).
- **El home es un repositorio git que se commitea tras cada turno** (`home.autosave`). Su `.gitignore`
  sembrado ignora `state/` y `.env` (`alfred/templates/gitignore`).
- **El agente hereda el entorno del proceso** (`Agent._environment` copia `os.environ`). Corre con
  `bypassPermissions` y puede leer cualquier fichero del usuario.
- **Dependencia:** `python-telegram-bot>=21,<23`. El soporte nativo de bots gestionados llegó en la
  22.8 (12 de junio de 2026), con `get_managed_bot_token`, `replace_managed_bot_token`,
  `set_managed_bot_access_settings` y `Update.managed_bot`.

Una consecuencia que no se ve a simple vista: **el `chat_id` de un chat privado es el id del usuario,
sea cual sea el bot.** Si Alfred atiende varios bots, el chat del usuario con cada uno tiene el mismo
`chat_id`. Con el código actual las sesiones, los recordatorios y el inbox de todos los bots se
mezclarían. La clave de conversación tiene que incluir el bot (y el tema, si se usan temas). Es el
cambio de fondo del diseño.

## 1. Bot API: bots gestionados

### Qué es

> "Telegram allows bots to create and manage other bots on behalf of their owners."
> ([Bot Features: Managed Bots](https://core.telegram.org/bots/features#managed-bots))

Un bot gestionado es un bot normal cuyo **propietario es el usuario que lo crea**. Además, otro bot (el
gestor) puede obtener su token y controlarlo. Según la
[página MTProto](https://core.telegram.org/api/bots/managed-bots): "A managed bot is a bot that is
fully owned by the user that created it [...] It can be managed by the user using @BotFather as for
all owned bots, but it can also be managed by the manager bot".

### Flujo

1. **Activar el modo gestor (una vez, a mano).** En el Mini App de @BotFather, en los ajustes del bot,
   se activa *Bot Management Mode*. `getMe` lo refleja en
   [`User.can_manage_bots`](https://core.telegram.org/bots/api#user) ("True, if other bots can be
   created to be controlled by the bot").
2. **Pedir la creación.** Hay tres formas:
   - Un botón de teclado
     [`KeyboardButton.request_managed_bot`](https://core.telegram.org/bots/api#keyboardbutton) con un
     [`KeyboardButtonRequestManagedBot`](https://core.telegram.org/bots/api#keyboardbuttonrequestmanagedbot)
     (`request_id`, `suggested_name`, `suggested_username`). Solo funciona en chats privados y solo para
     bots con el modo gestor activo.
   - Un enlace `https://t.me/newbot/{manager_bot_username}/{suggested_bot_username}[?name={suggested_bot_name}]`
     ([changelog 9.6](https://core.telegram.org/bots/api-changelog#april-3-2026),
     [deep links](https://core.telegram.org/api/links#managed-bot-creation-request-links)).
   - Desde un Mini App, con
     [`savePreparedKeyboardButton`](https://core.telegram.org/bots/api#savepreparedkeyboardbutton).
3. **El usuario confirma.** Ve una ventana con el nombre y el username prerrellenados y editables.
4. **El gestor recibe el resultado.** Le llega el update
   [`managed_bot`](https://core.telegram.org/bots/api#update), un
   [`ManagedBotUpdated`](https://core.telegram.org/bots/api#managedbotupdated) con `user` (el creador) y
   `bot`. Si se usó el botón, también llega un mensaje de servicio
   [`managed_bot_created`](https://core.telegram.org/bots/api#managedbotcreated).
5. **El gestor obtiene el token** con
   [`getManagedBotToken(user_id)`](https://core.telegram.org/bots/api#getmanagedbottoken). Con
   [`replaceManagedBotToken`](https://core.telegram.org/bots/api#replacemanagedbottoken) lo revoca y
   obtiene uno nuevo.
6. **El gestor restringe el acceso**, si quiere, con
   [`setManagedBotAccessSettings`](https://core.telegram.org/bots/api#setmanagedbotaccesssettings)
   (`is_access_restricted`, `added_user_ids` hasta 10). Lo lee con
   [`getManagedBotAccessSettings`](https://core.telegram.org/bots/api#getmanagedbotaccesssettings). El
   propietario siempre tiene acceso.
7. **Con el token del bot nuevo**, el gestor lo controla como cualquier bot: `getUpdates`,
   `sendMessage`, `setMyName`, `setMyDescription`, `setMyCommands`, `setMyProfilePhoto`...

`ManagedBotUpdated` llega también cuando **cambia el token o el propietario** del bot gestionado ("A
new bot was created to be managed by the bot, or token or owner of a managed bot was changed"). Así,
si el usuario regenera el token en BotFather, Alfred se entera y puede pedir el nuevo.

### Requisitos

| Requisito | Detalle |
| --- | --- |
| Modo gestor | Se activa a mano en el Mini App de BotFather. No hay método de la API para hacerlo. |
| Versión | Bot API 9.6 o posterior. En Python, python-telegram-bot 22.8 o posterior (sin ella valdría `Bot.do_api_request`). |
| Updates | `managed_bot` no es uno de los tipos excluidos por defecto en `allowed_updates` (solo lo son `chat_member`, `message_reaction` y `message_reaction_count`). Aun así conviene pasar la lista explícita, porque `getUpdates` reutiliza el último valor enviado ([getUpdates](https://core.telegram.org/bots/api#getupdates)). |
| Confirmación humana | "Only users can invoke `bots.checkUsername` and `bots.createBot`: bots and Mini Apps can request the creation of a managed bot" ([MTProto](https://core.telegram.org/api/bots/managed-bots)). |

### Límites

- **Bots por usuario.** Si el usuario ya tiene el máximo de bots propios, `bots.createBot` devuelve
  `BOT_CREATE_LIMIT_EXCEEDED`. El límite está en las claves `bots_create_limit_default` y
  `bots_create_limit_premium`. El ejemplo de la [documentación de config](https://core.telegram.org/api/config)
  muestra 20 y 40. Cuentan todos los bots del usuario, incluido Alfred.
- **Username.** Entre 5 y 32 caracteres, solo letras latinas, dígitos y `_`, y terminado en `bot`. Con
  Premium puede llevar un username secundario acabado en `agent`, `ai`, `app`, `game` o `gay`, en
  hasta 2 bots (Bot API 10.4). Desde la Bot API no se puede comprobar si está libre:
  `bots.checkUsername` solo lo puede llamar un usuario. El cliente del usuario lo valida en la ventana
  de confirmación, y el usuario puede cambiarlo ahí.
- **Borrado.** No hay método para borrar un bot gestionado. El propietario lo borra en BotFather
  (`/deletebot`, o `https://t.me/BotFather?start=deletebot`).
- **Privacidad.** El propietario sigue siendo el usuario. Puede revocar el token, transferir el bot o
  borrarlo desde BotFather sin que Alfred lo impida.

### Funciones relacionadas

- **Comunicación entre bots** (Bot API 10.0,
  [features](https://core.telegram.org/bots/features#bot-to-bot-communication)). Si ambos bots tienen
  activado el *Bot-to-Bot Communication Mode* en BotFather, un bot puede escribir a otro por privado
  con `sendMessage` y su `@username`. En grupos funciona mediante menciones o respuestas. Telegram
  **exige** salvaguardas contra bucles: deduplicar, limitar la tasa y fijar una profundidad máxima.
  Avisa de que no hacerlo "may lead to degraded performance or platform restrictions". Para Alfred no
  hace falta, porque los agentes comparten proceso y pueden hablarse por dentro.
- **Business bots** ([features](https://core.telegram.org/bots/features#business-bots)). Sirven
  para que un bot actúe en los chats de una cuenta Business con permisos explícitos. No crean bots ni
  chats nuevos, así que no resuelven este problema.

## 2. BotFather

### Qué se hace con él

Todo lo de [BotFather](https://core.telegram.org/bots/features#botfather):

- crear bots (`/newbot`) y obtener o regenerar su token (`/token`);
- editar el nombre, la descripción, el texto "about", la foto y los comandos;
- ajustes: modo inline, unirse a grupos, privacidad, transferir y borrar;
- en el **Mini App**, los interruptores nuevos: *Bot Management Mode*, *Bot-to-Bot Communication
  Mode*, temas en chats privados, *Guest Mode*, etc.

### ¿Se puede automatizar?

**No de forma directa.** BotFather es un bot con el que solo habla un usuario. No hay una API de
BotFather. Automatizarlo exigiría controlar una cuenta de usuario por MTProto y "conversar" con
BotFather analizando sus textos, que no tienen formato estable ("new commands [...] may be added at
any time"). No es una interfaz soportada. Además arrastra los riesgos de la sección 3.4.

**La vía soportada es la de los bots gestionados.** La propia documentación la presenta como la forma
de crear bots "without interacting with @BotFather". Cubre lo único que exigía BotFather en cada
creación, que es el token. El resto de lo que hacía BotFather (nombre, descripción, comandos, foto) lo
hace el propio bot con su token: `setMyName`, `setMyDescription`, `setMyShortDescription`,
`setMyCommands` y `setMyProfilePhoto`.

| Paso | ¿Automatizable? | Cómo |
| --- | --- | --- |
| Activar el modo gestor en el bot base | No, una vez a mano | Mini App de BotFather |
| Crear el bot | Un toque del usuario | Botón `request_managed_bot` o enlace `t.me/newbot/...` |
| Obtener el token | Sí | `getManagedBotToken` |
| Rotar el token | Sí | `replaceManagedBotToken` |
| Restringir quién lo usa | Sí | `setManagedBotAccessSettings` |
| Nombre, descripción, comandos, foto | Sí | `setMy*` con el token del bot nuevo |
| Activar temas, bot-to-bot y otros interruptores del Mini App en el bot nuevo | No (a mano, si hace falta) | Mini App de BotFather |
| Borrar el bot | No | El propietario, en BotFather |

## 3. Crear y vincular el chat de cada bot

### 3.1 Chat privado con cada bot (encaja con bots gestionados)

"Bots can't start conversations with users. A user must either add them to a group or send them a
message first" ([Bots: how are bots different](https://core.telegram.org/bots#how-are-bots-different-from-users)).
El chat queda vinculado cuando el usuario abre `https://t.me/<username_nuevo>` (o
`?start=<payload>`) y pulsa *Start*. Alfred lo sabe porque recibe ese `/start` por el token del bot
nuevo. Como `ManagedBotUpdated.user` dice quién creó el bot, Alfred ya sabe a quién esperar.

La documentación no dice si el creador de un bot gestionado cuenta como alguien que ya "inició" el
chat. Hay que suponer que no hasta verificarlo en vivo (sección 5.5).

### 3.2 Temas en el chat privado del bot actual (sin bots nuevos)

- **Bot API 9.3:** temas en chats privados, con `message_thread_id` en los mensajes y en los métodos
  de envío, y `User.has_topics_enabled` en `getMe`.
- **Bot API 9.4:** [`createForumTopic`](https://core.telegram.org/bots/api#createforumtopic) funciona
  también en chats privados, y existe `allows_users_to_create_topics`.
- **Bot API 10.4:** enlaces `https://t.me/<bot_username>/<message_thread_id>` que abren el tema solo
  para ese usuario.

Es la forma más sencilla de tener "varios chats dentro de Alfred". El bot crea el tema con una
llamada, sin confirmación ni token nuevo, y responde en él con `message_thread_id`.

| Ventajas | Inconvenientes |
| --- | --- |
| Se automatiza del todo. Sin tokens nuevos ni límite de 20 bots. | Todos los agentes comparten el nombre, la foto y las notificaciones de Alfred. |
| Un solo proceso de polling. | Requiere activar los temas en BotFather para el bot base. |
| | Los términos para desarrolladores ([§6.2.6](https://telegram.org/tos/bot-developers#6-2-6-enabling-topics-in-private-chats)) aplican una comisión del 15 % a las compras con Stars mientras los temas estén activos. A Alfred no le afecta porque no cobra. |

### 3.3 Grupos y foros

La Bot API **no tiene ningún método para crear grupos**. Para un grupo hace falta que el usuario lo
cree, active los temas y añada los bots. Para crear temas, el bot debe ser administrador con
`can_manage_topics`. Desde ahí se automatiza todo: `createForumTopic` y la invitación de más miembros
con `createChatInviteLink`. Límites a tener en cuenta:

- en grupos, un bot no envía más de 20 mensajes por minuto ([FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this));
- por defecto está activo el modo privacidad ([privacy mode](https://core.telegram.org/bots/features#privacy-mode));
- un bot solo ve los mensajes de otros bots con bot-to-bot activo.

Merece la pena si se quiere ver a varios agentes en una misma sala. Para "un chat por agente" no
aporta nada frente al chat privado.

### 3.4 Bot frente a cuenta de usuario (MTProto)

| | Bot (Bot API) | Cuenta de usuario (MTProto) |
| --- | --- | --- |
| Crear bots | Solo pedirlo (botón o enlace). El usuario confirma. | Sí, `bots.createBot` sin intervención |
| Crear grupos o supergrupos | No | Sí (`messages.createChat`, `channels.createChannel`) |
| Iniciar un chat | No, el usuario escribe primero | Sí |
| Credencial | Token del bot: controla solo ese bot | `api_id`/`api_hash` y una sesión que da acceso a **toda la cuenta**: chats, contactos, ajustes |
| Supervisión | La normal de los bots | "all accounts that log in using unofficial Telegram API clients are automatically put under observation" ([obtaining api_id](https://core.telegram.org/api/obtaining_api_id)) |
| Términos | [Bot Platform Developer Terms](https://telegram.org/tos/bot-developers) | [API Terms of Service](https://core.telegram.org/api/terms). Prohíben, por ejemplo, actuar en nombre del usuario sin que lo sepa y consienta (§1.4). |

Una cuenta de usuario lo automatizaría todo, y `bots.createBot` es un método documentado. Aun así lo
descarto:

- La sesión de usuario es una credencial mucho más peligrosa que un token de bot. Alfred corre un
  agente con `bypassPermissions` que puede leer ficheros.
- La cuenta personal del usuario quedaría bajo observación y expuesta a bloqueos.
- Esta tarea excluye usar la cuenta del usuario.
- Lo único que se gana es ahorrar un toque de confirmación y el primer *Start*.

## 4. Riesgos

### Términos de servicio

Los bots gestionados son la vía oficial, así que crearlos así no infringe nada. De los
[Bot Platform Developer Terms](https://telegram.org/tos/bot-developers) aplican:

- **§4.5 Credenciales.** Los tokens son confidenciales y está "explicitly prohibited" publicarlos. Lo
  que haga otro con tu token se considera hecho por ti.
- **§4.4 Seguridad.** Los datos de usuario deben cifrarse en reposo y guardarse separados de la clave.
  También piden un entorno razonablemente seguro.
- **§5.2(f).** Prohíbe operar "by proxy (i.e., using Bot API credentials supplied by other users) in an
  attempt to circumvent bans". Alfred solo debe aceptar bots de su propietario, para su propio uso.
- **§5.2(b).** No enviar mensajes no solicitados. Un agente nuevo no debe escribir a nadie más que a su
  dueño.
- **§4.3.** Prohíbe recoger datos para entrenar modelos. Alfred no lo hace y no debe hacerlo.

Lo que **sí** sería un problema: automatizar BotFather o crear bots desde una cuenta de usuario con un
cliente no oficial (sección 3.4).

### Límites de tasa

De la [FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this):

- como mucho un mensaje por segundo en un chat (se toleran ráfagas cortas, luego llega 429);
- 20 por minuto en un grupo;
- unos 30 por segundo de difusión.

Son por bot, así que varios bots no comparten el cupo. La documentación pide no depender de valores
fijos y reintentar con `retry_after`
([test environment](https://core.telegram.org/bots/features#dedicated-test-environment)). Hoy Alfred no
gestiona los 429: `_send` envía los trozos seguidos. Con varios agentes activos merece la pena usar
`AIORateLimiter` de python-telegram-bot, o como mínimo reintentar ante `RetryAfter`.

Otros límites operativos:

- **Un consumidor por token.** Dos procesos haciendo polling del mismo token producen un conflicto 409.
  Ya está documentado en el README.
- **Alertas de BotFather.** BotFather avisa cuando un bot popular responde poco
  ([status alerts](https://core.telegram.org/bots/features#status-alerts)). No afecta a bots privados.

### Seguridad

1. **Cualquiera puede abrir el enlace de creación.** Con el modo gestor activo, cualquier usuario de
   Telegram que conozca el username de Alfred puede abrir `t.me/newbot/<Alfred>/...` y crear un bot
   gestionado por Alfred. Alfred recibiría su update y podría pedir su token. **Si Alfred aceptara
   cualquier bot, un desconocido tendría un agente con acceso a la máquina.** El gestor debe:
   - comprobar `ManagedBotUpdated.user` con la lista de usuarios permitidos (`Config.is_allowed`)
     antes de pedir el token;
   - ignorar y registrar en el log cualquier otro bot;
   - además, exigir que la creación responda a una petición pendiente hecha por Alfred (el
     `request_id` del botón, o un username sugerido reciente) y caducarla en minutos.
2. **Restringir el bot nuevo.** Tras crearlo, llamar a `setManagedBotAccessSettings(is_access_restricted=True)`
   para que solo el dueño pueda usarlo. Mantener además la lista de permitidos en el propio Alfred,
   como hoy.
3. **Dónde se guarda el token:**
   - Nunca en el repositorio de código, en `docs/` ni en el git del home.
   - Fichero `~/.alfred/state/bots/<bot_id>.token`, o un único `state/bots.env`. Directorio en modo
     `0700` y fichero en `0600`, escritura atómica (temporal y luego `os.replace`). `state/` ya está en
     el `.gitignore` que se siembra en el home.
   - El `.gitignore` del home se copia una sola vez y luego es del usuario o del agente. Como red de
     seguridad, `bootstrap` debería comprobar que `state/` sigue ignorado, y `autosave` debería negarse
     a commitear si algún fichero en stage contiene algo con forma de token (`\d{6,12}:[A-Za-z0-9_-]{30,}`).
   - Opcional en macOS: el Llavero, con `security add-generic-password`. Cumple mejor el "cifrado en
     reposo" del §4.4, pero añade una dependencia por plataforma. Lo dejaría como mejora posterior.
4. **El agente puede ver los tokens.** Claude Code corre con los permisos del usuario. Un modelo
   manipulado (por ejemplo, desde una web) podría leer `state/` o `.env`, y eso ya pasa hoy con el token
   base. Mitigaciones:
   - no pasar tokens en el entorno del agente: `alfred send` los busca por `ALFRED_BOT_ID`;
   - que el manual (`CLAUDE.md`) prohíba leer `state/` y `.env`;
   - correr Alfred con un usuario propio si importa.
   Ningún diseño elimina el riesgo mientras el agente tenga acceso total.
5. **Logs.** `httpx` registra las URLs a nivel INFO, y en la Bot API el token va en la URL
   (`/bot<token>/...`). `telegram_bot.run` ya pone `httpx` en WARNING. Hay que mantenerlo y no
   registrar nunca el token ni el `ManagedBotUpdated` completo.
6. **Revocación.** Ante un 401 (token revocado) o un `managed_bot` que anuncie un cambio de token,
   Alfred debe parar ese bot, pedir el token nuevo con `getManagedBotToken` y arrancarlo otra vez. Si
   cambia el propietario a alguien no permitido, debe parar el bot y borrar su token.

## 5. Recomendación y diseño

### 5.1 Recomendación

1. **Mecanismo para bots nuevos:** bots gestionados, con Alfred como gestor. Un toque de confirmación
   y un *Start* por agente. El token se obtiene y se rota por la API.
2. **Chat de cada bot:** su chat privado con el dueño, vinculado al primer `/start`.
3. **Varios bots en un solo proceso** de `alfred run`: una `Application` de python-telegram-bot por
   token, todas en el mismo bucle de eventos y con el planificador compartido.
4. **Diseñar la clave de conversación pensando en temas,** para añadir después agentes ligeros como
   temas del chat privado de Alfred sin rehacer nada.
5. **No automatizar BotFather ni usar MTProto con la cuenta del usuario.**

### 5.2 Modelo de datos

```text
~/.alfred/state/
  agents.json            registro de agentes (sin secretos)
  bots/                  0700
    <bot_id>.token       0600, un token por fichero
  pending-agents.json    peticiones de creación en curso (request_id, username sugerido, caducidad)
  sessions.json          clave nueva: ver abajo
```

`agents.json`:

```json
{
  "8123456789": {
    "username": "alfred_research_bot",
    "name": "Research",
    "owner_id": 123456789,
    "created_at": "2026-11-12T10:00:00Z",
    "enabled": true,
    "chat_id": 123456789,
    "instructions": "agents/research/AGENT.md"
  }
}
```

**Clave de conversación.** Se introduce un tipo pequeño en `alfred/agent.py`:

```python
class Conversation(NamedTuple):
    bot_id: int = 0                  # 0 = el bot base (TELEGRAM_BOT_TOKEN)
    chat_id: int = 0
    thread_id: Optional[int] = None  # tema de foro o de chat privado

    @property
    def key(self) -> str:
        # El bot base sin tema conserva la clave de hoy, así sessions.json no necesita migración.
        if self.bot_id == 0 and self.thread_id is None:
            return str(self.chat_id)
        return "%s/%s/%s" % (self.bot_id, self.chat_id, self.thread_id or 0)
```

`Agent`, `SessionStore`, el inbox y `Listener` pasan de `chat_id: int` a `Conversation`. La clave se usa
en los diccionarios y en `sessions.json`. El planificador añade dos columnas, `bot_id INTEGER NOT NULL
DEFAULT 0` y `thread_id INTEGER`, con una migración `ALTER TABLE` como la que ya hace con el formato
antiguo. El entorno del agente añade `ALFRED_BOT_ID` y `ALFRED_THREAD_ID`, y `bin/alfred send|event|schedule`
los respeta.

### 5.3 Componentes

| Módulo | Cambio |
| --- | --- |
| `alfred/config.py` | `bots_dir`, `agents_path`. `TELEGRAM_BOT_TOKEN` sigue siendo el bot base (gestor). Sin ajustes nuevos obligatorios. |
| `alfred/agent_bots.py` (nuevo) | `TokenStore`: leer, escribir con `0600` y borrar. `AgentRegistry`: `agents.json` y peticiones pendientes con caducidad. Sin dependencia de Telegram, fácil de probar. |
| `alfred/telegram_bot.py` | `TelegramBot` recibe `bot_id`, y sus handlers construyen `Conversation(bot_id, chat_id, message_thread_id)`. Un `BotPool` arranca una `Application` por token (`initialize`, `start` y `updater.start_polling(allowed_updates=[...])`) en el mismo bucle, y sustituye a `run_polling()`. El bot base añade un handler para `Update.managed_bot` y el comando `/agent`. |
| `alfred/agent.py` | `Conversation` como clave. `_environment` añade `ALFRED_BOT_ID`/`ALFRED_THREAD_ID` y quita del entorno cualquier `TELEGRAM_BOT_TOKEN`. Las instrucciones propias del agente (`agents/<nombre>/AGENT.md` en el home) van en la línea `<context .../>` o en el primer mensaje de la sesión, igual que el índice de skills con Codex. |
| `alfred/cli.py` | `alfred send` elige el token por `ALFRED_BOT_ID` (`TokenStore`, o el base si es 0) y pasa `message_thread_id`. Comandos nuevos: `alfred agent request <nombre>` (para que el modelo pueda proponer un agente) y `alfred agent list`. |
| `alfred/home.py` | `bootstrap` comprueba que `state/` está en el `.gitignore` del home. `autosave` rechaza commits con algo que parezca un token. |
| `alfred/plugin/skills/` | Una skill base `agents` que explica al modelo cuándo y cómo proponer un agente nuevo y que nunca lea `state/`. |
| `pyproject.toml` | `python-telegram-bot>=22.8,<23`. |

### 5.4 Flujo de creación

```text
usuario ──/agent new Research──► Alfred (bot base)
Alfred: guarda la petición pendiente (request_id, "alfred_research_bot", caduca en 10 min)
Alfred ──mensaje con botón request_managed_bot (o enlace t.me/newbot/<Alfred>/alfred_research_bot?name=Research)──► usuario
usuario: toca, revisa nombre y username, confirma            (único paso manual 1)
Telegram ──update managed_bot {user, bot}──► Alfred
Alfred: ¿user permitido? ¿hay una petición pendiente que encaje? Si no, se ignora y se registra.
Alfred ──getManagedBotToken(bot.id)──► token ─► TokenStore (0600)
Alfred ──setManagedBotAccessSettings(bot.id, is_access_restricted=True)
Alfred, con el token nuevo ──setMyCommands / setMyDescription / setMyShortDescription
Alfred: AgentRegistry.add(...); BotPool.start(bot_id)
Alfred ──"Listo. Abre t.me/alfred_research_bot?start=hola"──► usuario
usuario: abre y pulsa Start                                    (único paso manual 2)
bot nuevo ──/start──► registra chat_id; el agente saluda en su propio chat
```

Gestión posterior, desde el chat del bot base:

- `/agents`: lista los agentes y su estado.
- `/agent off <nombre>` y `/agent on <nombre>`: paran o arrancan el polling. El token se conserva.
- `/agent rotate <nombre>`: `replaceManagedBotToken`, guarda el token nuevo y reinicia.
- `/agent remove <nombre>`: para el bot, borra el token y el registro, y explica que para liberar el
  username hay que borrarlo en BotFather (`t.me/BotFather?start=deletebot`).
- Un `managed_bot` no solicitado con un bot que ya está en el registro significa que cambió el token o
  el propietario: se vuelve a pedir el token, o se desactiva si el dueño ya no está permitido.

Preguntas de diseño que dejo abiertas, con lo que propongo por defecto:

- **¿Memoria y skills compartidas o por agente?** Por defecto, compartidas: un solo home, como hoy
  entre backends. Cada agente tiene su `AGENT.md` con su papel. Separar memorias es la pregunta 3 de
  `docs/PLAN.md` y puede decidirse después.
- **¿Puede el modelo crear agentes por su cuenta?** Solo proponerlos: `alfred agent request` envía el
  botón y la persona decide. Encaja con la regla de confirmar antes de cualquier acción hacia fuera.

### 5.5 Pasos de implementación

Cada paso se puede probar por separado. El orden reduce riesgos: primero lo que no toca Telegram.

1. **Activación manual (la hace el usuario, una vez).** Activar *Bot Management Mode* en el Mini App de
   BotFather para el bot de Alfred. Al arrancar, Alfred comprueba `getMe().can_manage_bots` y lo
   muestra en `/status`. Si falta, `/agent new` explica cómo activarlo en lugar de fallar.
2. **Dependencia.** Subir a `python-telegram-bot>=22.8,<23` y pasar la suite.
3. **Clave de conversación.** Introducir `Conversation` y migrar `SessionStore`, el planificador
   (columnas nuevas con valor por defecto), el inbox, el entorno y `bin/alfred`. Sin cambio visible: el
   bot base sigue usando la clave antigua. Añadir tests de migración.
4. **Secretos.** `TokenStore` con permisos y escritura atómica, la comprobación del `.gitignore` en
   `bootstrap` y el bloqueo de tokens en `autosave`. Tests: los permisos son `0600` y un commit con un
   token falso se rechaza.
5. **Varios bots en un proceso.** `BotPool` con N `Application`, cada una con su `TelegramBot(bot_id)`
   y el planificador compartido, que entrega usando el `bot_id` del elemento. Probarlo con dos bots
   falsos en `tests/fakes.py`.
6. **Flujo de creación.** El handler `managed_bot`, `/agent new` con botón y enlace, peticiones
   pendientes con caducidad y comprobación del creador. Tests:
   - un creador no permitido se ignora y no se pide su token;
   - una creación sin petición pendiente se ignora;
   - el flujo feliz guarda el token, restringe el acceso y arranca el bot.
7. **Ciclo de vida.** `/agents`, `on`/`off`/`rotate`/`remove`, y la reacción a cambios de token o
   propietario y a los 401. `AIORateLimiter` o reintentos ante `RetryAfter`.
8. **Instrucciones del agente.** Skill base `agents`, `agents/<nombre>/AGENT.md` y un apartado en
   `CLAUDE.md` sobre no leer `state/` ni `.env`.
9. **Documentación.** README (comandos, el paso manual en BotFather, dónde viven los tokens),
   `.env.example` y `docs/PLAN.md`.
10. **Verificación en vivo** (fuera de esta investigación, con el permiso del usuario). Usar el
    [entorno de pruebas de Telegram](https://core.telegram.org/bots/features#dedicated-test-environment)
    con una cuenta de pruebas, o la cuenta real si el usuario lo decide. Puntos que la documentación no
    aclara:
    - si el creador necesita pulsar *Start* en el bot nuevo antes de que este pueda escribirle;
    - el texto exacto del update cuando el usuario regenera el token en BotFather;
    - si `managed_bot_created` llega también cuando se usa el enlace y no el botón.

### 5.6 Variante ligera: agentes como temas

Si más adelante se quieren agentes sin bot propio, con el diseño anterior solo falta esto:

- activar los temas en chats privados para el bot base (BotFather) y comprobar `getMe().has_topics_enabled`;
- `/agent new --topic <nombre>`: `createForumTopic(chat_id=<usuario>, name=<nombre>)` y guardar el
  `message_thread_id` en el registro;
- leer `message.message_thread_id` en los handlers y pasarlo en cada envío.

No hay tokens nuevos, confirmaciones ni límite de bots. A cambio, todos los agentes comparten la
identidad de Alfred.

## Fuentes

Documentación oficial de Telegram, consultada el 12 de noviembre de 2026:

- Bot API, referencia: https://core.telegram.org/bots/api
  - `User.can_manage_bots`: https://core.telegram.org/bots/api#user
  - `KeyboardButtonRequestManagedBot`: https://core.telegram.org/bots/api#keyboardbuttonrequestmanagedbot
  - `ManagedBotUpdated`: https://core.telegram.org/bots/api#managedbotupdated
  - `ManagedBotCreated`: https://core.telegram.org/bots/api#managedbotcreated
  - `getManagedBotToken`: https://core.telegram.org/bots/api#getmanagedbottoken
  - `replaceManagedBotToken`: https://core.telegram.org/bots/api#replacemanagedbottoken
  - `getManagedBotAccessSettings` / `setManagedBotAccessSettings`: https://core.telegram.org/bots/api#setmanagedbotaccesssettings
  - `BotAccessSettings`: https://core.telegram.org/bots/api#botaccesssettings
  - `savePreparedKeyboardButton`: https://core.telegram.org/bots/api#savepreparedkeyboardbutton
  - `createForumTopic`: https://core.telegram.org/bots/api#createforumtopic
  - `getUpdates` / `allowed_updates`: https://core.telegram.org/bots/api#getupdates
- Bot API, changelog (9.3, 9.4, 9.6, 10.0, 10.4): https://core.telegram.org/bots/api-changelog
- Bot Features (Managed Bots, Bot-to-Bot, Topics in Private Chats, BotFather, límites, entorno de
  pruebas): https://core.telegram.org/bots/features
- Introducción a bots (diferencias con usuarios, tokens): https://core.telegram.org/bots
- Bot FAQ (límites de tasa): https://core.telegram.org/bots/faq
- MTProto, bots gestionados (`bots.createBot`, `bots.exportBotToken`, límites): https://core.telegram.org/api/bots/managed-bots
- MTProto, trabajar con bots: https://core.telegram.org/api/bots
- MTProto, configuración del cliente (`bots_create_limit_*`): https://core.telegram.org/api/config
- MTProto, deep links de creación de bots gestionados: https://core.telegram.org/api/links#managed-bot-creation-request-links
- Obtener `api_id` (supervisión de clientes no oficiales): https://core.telegram.org/api/obtaining_api_id
- Telegram API Terms of Service: https://core.telegram.org/api/terms
- Bot Platform Developer Terms: https://telegram.org/tos/bot-developers

Fuera de Telegram:

- python-telegram-bot, versiones y fechas (22.8, del 12 de junio de 2026, añade los métodos de bots
  gestionados): https://pypi.org/project/python-telegram-bot/
