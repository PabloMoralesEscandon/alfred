# Agent chats: crear bots y chats nuevos desde Consiglieri

Estado: investigado e implementado (12 de noviembre de 2026). Las secciones 0 a 5 son la investigación
y el diseño propuesto. La [sección 6](#6-implementación) cuenta lo que se construyó en esta rama, en qué
se apartó del diseño y por qué, qué se hace cumplir de verdad y qué falta probar en vivo. Fuente
principal: la documentación oficial de Telegram, consultada en esa fecha (Bot API 10.4, del 9 de
noviembre de 2026), y la de Claude Code para los permisos. Los enlaces están en cada sección y
reunidos al final.

## Conclusión

Desde la Bot API 9.6 (3 de abril de 2026) existe una vía oficial para esto: los **bots gestionados**
(*Managed Bots*). Consiglieri puede activarse como "bot gestor" una sola vez en el Mini App de @BotFather.
Desde entonces puede ofrecer al usuario un botón o un enlace `t.me/newbot/...` con el nombre y el
username ya rellenos. El usuario confirma con un toque, Consiglieri recibe un update `managed_bot` y obtiene
el token del bot nuevo con `getManagedBotToken`, sin pasar por BotFather ni por una cuenta de usuario.

Lo que no se puede automatizar del todo, por diseño:

- **La creación siempre la confirma una persona.** Solo un usuario puede invocar `bots.createBot`. Un
  bot solo puede *pedir* la creación.
- **El primer mensaje lo envía el usuario.** Un bot no puede iniciar una conversación, así que el chat
  privado con el bot nuevo queda vinculado cuando el usuario pulsa *Start*. Consiglieri le pasa el enlace.
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
sea cual sea el bot.** Si Consiglieri atiende varios bots, el chat del usuario con cada uno tiene el mismo
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
si el usuario regenera el token en BotFather, Consiglieri se entera y puede pedir el nuevo.

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
  muestra 20 y 40. Cuentan todos los bots del usuario, incluido Consiglieri.
- **Username.** Entre 5 y 32 caracteres, solo letras latinas, dígitos y `_`, y terminado en `bot`. Con
  Premium puede llevar un username secundario acabado en `agent`, `ai`, `app`, `game` o `gay`, en
  hasta 2 bots (Bot API 10.4). Desde la Bot API no se puede comprobar si está libre:
  `bots.checkUsername` solo lo puede llamar un usuario. El cliente del usuario lo valida en la ventana
  de confirmación, y el usuario puede cambiarlo ahí.
- **Borrado.** No hay método para borrar un bot gestionado. El propietario lo borra en BotFather
  (`/deletebot`, o `https://t.me/BotFather?start=deletebot`).
- **Privacidad.** El propietario sigue siendo el usuario. Puede revocar el token, transferir el bot o
  borrarlo desde BotFather sin que Consiglieri lo impida.

### Funciones relacionadas

- **Comunicación entre bots** (Bot API 10.0,
  [features](https://core.telegram.org/bots/features#bot-to-bot-communication)). Si ambos bots tienen
  activado el *Bot-to-Bot Communication Mode* en BotFather, un bot puede escribir a otro por privado
  con `sendMessage` y su `@username`. En grupos funciona mediante menciones o respuestas. Telegram
  **exige** salvaguardas contra bucles: deduplicar, limitar la tasa y fijar una profundidad máxima.
  Avisa de que no hacerlo "may lead to degraded performance or platform restrictions". Para Consiglieri no
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
`?start=<payload>`) y pulsa *Start*. Consiglieri lo sabe porque recibe ese `/start` por el token del bot
nuevo. Como `ManagedBotUpdated.user` dice quién creó el bot, Consiglieri ya sabe a quién esperar.

La documentación no dice si el creador de un bot gestionado cuenta como alguien que ya "inició" el
chat. Hay que suponer que no hasta verificarlo en vivo (sección 5.5).

### 3.2 Temas en el chat privado del bot actual (sin bots nuevos)

- **Bot API 9.3:** temas en chats privados, con `message_thread_id` en los mensajes y en los métodos
  de envío, y `User.has_topics_enabled` en `getMe`.
- **Bot API 9.4:** [`createForumTopic`](https://core.telegram.org/bots/api#createforumtopic) funciona
  también en chats privados, y existe `allows_users_to_create_topics`.
- **Bot API 10.4:** enlaces `https://t.me/<bot_username>/<message_thread_id>` que abren el tema solo
  para ese usuario.

Es la forma más sencilla de tener "varios chats dentro de Consiglieri". El bot crea el tema con una
llamada, sin confirmación ni token nuevo, y responde en él con `message_thread_id`.

| Ventajas | Inconvenientes |
| --- | --- |
| Se automatiza del todo. Sin tokens nuevos ni límite de 20 bots. | Todos los agentes comparten el nombre, la foto y las notificaciones de Consiglieri. |
| Un solo proceso de polling. | Requiere activar los temas en BotFather para el bot base. |
| | Los términos para desarrolladores ([§6.2.6](https://telegram.org/tos/bot-developers#6-2-6-enabling-topics-in-private-chats)) aplican una comisión del 15 % a las compras con Stars mientras los temas estén activos. A Consiglieri no le afecta porque no cobra. |

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

- La sesión de usuario es una credencial mucho más peligrosa que un token de bot. Consiglieri corre un
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
  attempt to circumvent bans". Consiglieri solo debe aceptar bots de su propietario, para su propio uso.
- **§5.2(b).** No enviar mensajes no solicitados. Un agente nuevo no debe escribir a nadie más que a su
  dueño.
- **§4.3.** Prohíbe recoger datos para entrenar modelos. Consiglieri no lo hace y no debe hacerlo.

Lo que **sí** sería un problema: automatizar BotFather o crear bots desde una cuenta de usuario con un
cliente no oficial (sección 3.4).

### Límites de tasa

De la [FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this):

- como mucho un mensaje por segundo en un chat (se toleran ráfagas cortas, luego llega 429);
- 20 por minuto en un grupo;
- unos 30 por segundo de difusión.

Son por bot, así que varios bots no comparten el cupo. La documentación pide no depender de valores
fijos y reintentar con `retry_after`
([test environment](https://core.telegram.org/bots/features#dedicated-test-environment)). Hoy Consiglieri no
gestiona los 429: `_send` envía los trozos seguidos. Con varios agentes activos merece la pena usar
`AIORateLimiter` de python-telegram-bot, o como mínimo reintentar ante `RetryAfter`.

Otros límites operativos:

- **Un consumidor por token.** Dos procesos haciendo polling del mismo token producen un conflicto 409.
  Ya está documentado en el README.
- **Alertas de BotFather.** BotFather avisa cuando un bot popular responde poco
  ([status alerts](https://core.telegram.org/bots/features#status-alerts)). No afecta a bots privados.

### Seguridad

1. **Cualquiera puede abrir el enlace de creación.** Con el modo gestor activo, cualquier usuario de
   Telegram que conozca el username de Consiglieri puede abrir `t.me/newbot/<Consiglieri>/...` y crear un bot
   gestionado por Consiglieri. Consiglieri recibiría su update y podría pedir su token. **Si Consiglieri aceptara
   cualquier bot, un desconocido tendría un agente con acceso a la máquina.** El gestor debe:
   - comprobar `ManagedBotUpdated.user` con la lista de usuarios permitidos (`Config.is_allowed`)
     antes de pedir el token;
   - ignorar y registrar en el log cualquier otro bot;
   - además, exigir que la creación responda a una petición pendiente hecha por Consiglieri (el
     `request_id` del botón, o un username sugerido reciente) y caducarla en minutos.
2. **Restringir el bot nuevo.** Tras crearlo, llamar a `setManagedBotAccessSettings(is_access_restricted=True)`
   para que solo el dueño pueda usarlo. Mantener además la lista de permitidos en el propio Consiglieri,
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
   - correr Consiglieri con un usuario propio si importa.
   Ningún diseño elimina el riesgo mientras el agente tenga acceso total.
5. **Logs.** `httpx` registra las URLs a nivel INFO, y en la Bot API el token va en la URL
   (`/bot<token>/...`). `telegram_bot.run` ya pone `httpx` en WARNING. Hay que mantenerlo y no
   registrar nunca el token ni el `ManagedBotUpdated` completo.
6. **Revocación.** Ante un 401 (token revocado) o un `managed_bot` que anuncie un cambio de token,
   Consiglieri debe parar ese bot, pedir el token nuevo con `getManagedBotToken` y arrancarlo otra vez. Si
   cambia el propietario a alguien no permitido, debe parar el bot y borrar su token.

## 5. Recomendación y diseño

> Este es el diseño propuesto antes de implementar. Lo construido está en la [sección 6](#6-implementación),
> que lo sustituye donde difieren: el bot base es ahora un panel sin agente, la memoria, el inbox y las
> extensiones son de cada bot, cada bot tiene permisos propios, los directorios de los bots están fuera
> del home y no hizo falta subir de versión python-telegram-bot.

### 5.1 Recomendación

1. **Mecanismo para bots nuevos:** bots gestionados, con Consiglieri como gestor. Un toque de confirmación
   y un *Start* por agente. El token se obtiene y se rota por la API.
2. **Chat de cada bot:** su chat privado con el dueño, vinculado al primer `/start`.
3. **Varios bots en un solo proceso** de `alfred run`: una `Application` de python-telegram-bot por
   token, todas en el mismo bucle de eventos y con el planificador compartido.
4. **Diseñar la clave de conversación pensando en temas,** para añadir después agentes ligeros como
   temas del chat privado de Consiglieri sin rehacer nada.
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
usuario ──/agent new Research──► Consiglieri (bot base)
Consiglieri: guarda la petición pendiente (request_id, "alfred_research_bot", caduca en 10 min)
Consiglieri ──mensaje con botón request_managed_bot (o enlace t.me/newbot/<Consiglieri>/alfred_research_bot?name=Research)──► usuario
usuario: toca, revisa nombre y username, confirma            (único paso manual 1)
Telegram ──update managed_bot {user, bot}──► Consiglieri
Consiglieri: ¿user permitido? ¿hay una petición pendiente que encaje? Si no, se ignora y se registra.
Consiglieri ──getManagedBotToken(bot.id)──► token ─► TokenStore (0600)
Consiglieri ──setManagedBotAccessSettings(bot.id, is_access_restricted=True)
Consiglieri, con el token nuevo ──setMyCommands / setMyDescription / setMyShortDescription
Consiglieri: AgentRegistry.add(...); BotPool.start(bot_id)
Consiglieri ──"Listo. Abre t.me/alfred_research_bot?start=hola"──► usuario
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
   BotFather para el bot de Consiglieri. Al arrancar, Consiglieri comprueba `getMe().can_manage_bots` y lo
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
identidad de Consiglieri.

## 6. Implementación

Implementado en la rama `PabloMoralesEscandon/agent-chats` en commits pequeños. No se creó ningún bot
real, no se usó ninguna credencial y no se tocó `~/.alfred` ni el servicio en marcha. Todo lo de abajo
está probado con un Telegram y un Claude Code simulados (sección 6.6). Lo que falta probar con Telegram
de verdad está en la sección 6.7.

### 6.1 Qué cambió respecto al diseño, y por qué

| Diseño (sección 5) | Implementado | Por qué |
| --- | --- | --- |
| El bot actual sigue siendo Consiglieri y además gestiona bots | El bot base es un **panel de control sin agente** (`ControlPanel`): crea, lista, configura y destruye bots, y es el gestor de sus cuentas de Telegram. Los agentes viven solo en los bots creados | Petición del usuario |
| Memoria y skills compartidas por defecto | **Todo es de cada bot**: memoria, inbox, outbox, skills, extensiones, sesiones y tareas programadas. Lo común a todos es solo la base: el comando `alfred`, el plugin `alfred/plugin` y sus skills `alfred:*` | Petición del usuario |
| Directorio del agente en `~/.alfred/agents/<nombre>/` | **`~/.alfred-bots/<bot_id>/`**, al lado del home y no dentro (`ALFRED_BOTS_HOME`) | Claude Code carga todos los `CLAUDE.md` desde el cwd hasta la raíz ([memory](https://code.claude.com/docs/en/memory)). Dentro de `~/.alfred`, cada bot leería también el manual de Consiglieri y su índice de memoria |
| Una clave `bot/chat/tema` en un único `sessions.json` | Un **`Agent` por bot**, con su propio `state/bots/<id>/sessions.json`, y dentro una clave `Chat` (chat + tema) | Encaja mejor con el código: cada bot tiene su configuración, su backend y sus procesos. `Chat` es un `int` que lleva el tema, así que el código y los 125 tests que pasan `chat_id` enteros siguen funcionando |
| `--add-dir` para el directorio de trabajo | Igual, pero el **cwd es el directorio propio del bot** y el de trabajo va con `--add-dir` y `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` | Así el manual y la memoria del bot se cargan siempre de forma nativa. El `CLAUDE.md` del proyecto se carga también, gracias a esa variable |
| Subir a python-telegram-bot 22.8 | **Sin cambio de versión**: las llamadas de gestor van por `Bot.do_api_request` y el update `managed_bot` se lee de `api_kwargs` | La 22.8 exige Python 3.10 y el servicio corre en Python 3.9.6. La 22.5 (Bot API 9.2) sirve igual. Si se sube de versión, el código lee también los atributos nativos |
| Aceptar solo creaciones que respondan a una petición pendiente | Se aceptan los bots de un creador de la lista de permitidos. Si hay petición, el bot recibe su tarea y sus permisos. Si no, queda con los permisos mínimos y se avisa al dueño | La frontera de seguridad es la lista de permitidos. Rechazar el bot de tu propio dueño no protege de nada y deja un bot huérfano |
| Borrar el bot gestionado | **No se puede por la API.** `destroy` revoca el token con `replaceManagedBotToken` (y descarta el nuevo), borra todo lo local y envía el enlace `https://t.me/BotFather?start=deletebot` | No hay ningún método de borrado ni en la Bot API ni en MTProto para el gestor (sección 1, *Límites*) |
| Permisos: no estaban en el diseño | **Permisos por bot**, elegidos al crearlo y cambiables desde el panel (sección 6.4) | Petición del usuario |

### 6.2 Cómo funciona

```text
Telegram ─► ControlPanel (TELEGRAM_BOT_TOKEN)  /bots /bot …   sin agente
   │          └─ managed_bot ─► BotPool.on_managed_bot ─► getManagedBotToken ─► state/bots/<id>/token
   │                                                    └─► BotPool.launch(record): conecta sin reiniciar
   ├─► TelegramBot @research_bot ─► Agent(config del bot) ─► claude -p … cwd=~/.alfred-bots/<id>
   │                                                            --settings state/bots/<id>/claude-settings.json
   │                                                            --setting-sources project,local --add-dir <workdir>
   └─► TelegramBot @chef_bot     ─► Agent(config del bot) ─► claude …  (otro proceso, otra sesión)
                    ▲
   scheduler (uno) ─┘ cada elemento lleva bot_id, chat_id y thread_id, y solo se entrega a ese bot
```

- **`alfred/bots.py`**:
  - `BotRegistry`: los ficheros de cada bot, su token, las peticiones pendientes y `config_for(bot_id)`, que da a cada bot su propio `Config`;
  - la validación de ajustes y permisos;
  - `claude_settings()`, que traduce los permisos a ajustes de Claude Code;
  - `prepare_workspace()`, que reescribe el manual y los permisos antes de cada mensaje;
  - las tres llamadas de gestor a la Bot API.
- **`alfred/config.py`**: `Config` acepta `bot_id`, `workspace_dir` y unos `overrides` que se leen en cada consulta. Un cambio desde el panel se aplica en el siguiente mensaje sin reiniciar, igual que ya pasaba con `.env`. La memoria, el inbox, las skills, las extensiones y las sesiones salen del directorio del bot.
- **`alfred/telegram_bot.py`**: `ControlPanel`; `TelegramBot`, la interfaz con agente de cada bot creado; y `BotPool`, que corre todas las `Application` en un solo bucle y añade o quita bots en caliente.
- **`alfred/cli.py`**: dentro del turno de un bot (`ALFRED_BOT_ID`), `alfred` actúa como ese bot: su token, su chat, su tema, su configuración y sus permisos. Añade `alfred bots list|set|new` y `alfred chat --bot`.
- **`alfred/scheduler.py`**: columnas `bot_id` y `thread_id` con migración; listado y cancelación por bot; `Postpone` para los bots en pausa.
- **`alfred/templates/BOT.md`**: el manual de cada bot, con su tarea, su directorio de trabajo y un resumen de lo que puede hacer.

### 6.3 Menús y comandos

**El menú (`/bot` sin argumentos).** Es un teclado en línea que edita siempre el mismo mensaje
(`alfred/menus.py`, sin dependencias de Telegram):

- **Crear bot** es un asistente de 6 pasos, cada uno con *Atrás* y *Cancelar*:
  1. nombre, como texto;
  2. tarea, como texto o *Saltar*;
  3. modelo: Claude o Codex; para Claude, Default, Fable, Opus, Sonnet o Haiku; o escribir un nombre;
  4. esfuerzo, con las opciones de cada backend;
  5. permisos, con interruptores y los preajustes Mínimo, Estándar y Amplio. Las opciones peligrosas
     llevan avisos: shell completa, sin sandbox, navegador, otros chats, `bypassPermissions`, `auto`;
  6. resumen con el username propuesto y *Crear*.

  *Crear* envía el botón de teclado y el enlace, igual que `/bot new`.
- **Mis bots** abre cada bot: modelo, esfuerzo, tarea, permisos (cada toque se aplica), pausar o
  reanudar, y destruir con una pantalla de confirmación.
- **Ayuda**.

Detalles de los menús:

- **Estado:** en memoria, una sesión por usuario, que caduca a los 30 minutos sin uso. Un `/bot` nuevo
  sustituye al anterior, y un reinicio las borra todas. Un botón de una sesión que ya no existe contesta
  "This menu has expired… Send /bot" y no hace nada, así que no queda un asistente a medias que actúe
  con elecciones viejas.
- **Botones:** el `callback_data` es `m:<token>:<acción>`, siempre por debajo de 64 bytes, y un test lo
  comprueba en todas las pantallas.
- **Quién puede pulsar:** solo los usuarios permitidos, y solo quien abrió el menú.
- **Texto libre:** el panel lo interpreta como respuesta solo mientras el menú espera un texto.
- **Modos en el selector:** recorre `dontAsk`, `acceptEdits`, `auto` y `bypassPermissions`. `plan` y
  `default` quedan para `/bot perm`, porque sin nadie que conteste no sirven.

**Username propuesto.** Extiende el del panel: `manolito_bot` + "Gestor calendarios" →
`manolito_gestor_calendarios_bot`.

- El slug sale del nombre o, si este no tiene letras útiles, de la tarea. Se pasa a ASCII (acentos,
  emojis, mayúsculas, espacios), solo con `[a-z0-9_]`.
- Para no pasar de 32 caracteres se recorta el slug, nunca el prefijo, sin dejar `__` ni `_` colgando.
- Si coincide con un bot propio, una petición pendiente o el propio panel, lleva un número
  (`manolito_research_2_bot`).
- La Bot API no permite comprobar si otra persona ya tiene ese username (`bots.checkUsername` es solo
  para usuarios). En ese caso Telegram lo rechaza en su ventana, donde el usuario puede cambiar nombre
  y username.
- Caso límite: si el username del panel es tan largo que ni `prefijo_bot` cabe, se recorta también el
  prefijo.

**Comandos de texto (atajos)**, en el panel:

- `/bot new <nombre> [opciones] [tarea]`: las opciones van antes de la tarea, por ejemplo `+web`, `+shell`, `-schedule` o `model=sonnet`.
  - **Vía principal:** un botón de teclado `KeyboardButton.request_managed_bot` con `request_id`, `suggested_name` y `suggested_username`. La 22.5 no tiene la clase, así que el botón lleva el JSON de la Bot API en `api_kwargs`. No depende de que la app abra un deep link.
  - **Segunda opción:** el enlace `t.me/newbot/<panel>/<username>`.
  - El teclado tiene también una tecla "Cancel", que descarta la petición. Se quita con `ReplyKeyboardRemove` cuando el bot ya existe o al cancelar.
  - El mensaje de servicio `managed_bot_created`, que llega al chat del panel, se ignora: el update `managed_bot` hace el trabajo.
- `/bots` y `/bot show <bot>`: listar y ver ajustes y permisos.
- `/bot set <bot> <ajuste> <valor>`: `task`, `model`, `effort`, `backend` o `workdir`.
- `/bot perm <bot> <permiso> <valor>`.
- `/bot perm <bot> cross on|off`: el acceso entre agentes (sección 6.4, *Memoria y herramientas de otros
  agentes*). `on` muestra una advertencia y pide un botón de confirmación; `off` se aplica sin preguntar.
- `/bot audit`: las últimas concesiones y retiradas del acceso entre agentes.
- `/bot pause|resume <bot>`.
- `/bot destroy <bot>`: pide confirmación con botones, que solo puede pulsar quien lo pidió y que caducan en 5 minutos.
- `/status`, `/usage`, `/help`.

En cada bot, los de siempre: `/help /new /stop /usage /status /memory /skills /schedule /backend`.

### 6.4 Permisos: qué se hace cumplir y qué no

Cada bot corre con su propio fichero de ajustes, `state/bots/<id>/claude-settings.json`, pasado con
`--settings` ([settings](https://code.claude.com/docs/en/settings)), y con `--setting-sources project,local`.
Esto último deja fuera `~/.claude/settings.json`, para que tus reglas *allow* globales no amplíen las del
bot. Las reglas *allow* del `.claude/settings.json` de un proyecto no se aplican en `claude -p` sin
confianza en el espacio de trabajo ([permissions](https://code.claude.com/docs/en/permissions#project-allow-rules-and-workspace-trust)),
así que el fichero `--settings` es el sitio correcto. Las reglas *deny* se aplican en todos los modos,
también en `bypassPermissions` ([permission modes](https://code.claude.com/docs/en/permission-modes)).

Permisos por defecto de un bot nuevo:

- modo `dontAsk`: todo lo que pediría confirmación se deniega;
- solo el comando `alfred` en la shell;
- recordatorios activados;
- nada fuera de su directorio: sin web, sin navegador, sin MCP;
- no puede escribir a terceros ni gestionar bots.

Siempre:

- `blockReadsOutsideWorkingDirectories` y `blockWritesOutsideWorkingDirectories`;
- `denyInsteadOfPrompting`;
- deny para leer el home de Consiglieri (`.env`, tokens) y el `.env` del código;
- deny para editar su propio `CLAUDE.md`, `AGENTS.md` y `.claude/settings*.json`;
- deny para `alfred run|service|init|chat`.

| Permiso | Mecanismo | ¿Se hace cumplir? |
| --- | --- | --- |
| `mode` | `--permission-mode` y `defaultMode` | **Sí, Claude Code.** |
| `shell` = `alfred` | allow `Bash(alfred *)`; en `dontAsk` cualquier otro comando se deniega | **Sí, Claude Code.** Siguen permitidos los comandos de solo lectura que Claude Code considera seguros (`ls`, `cat`…), dentro de las reglas de lectura. |
| `shell` = `full` | allow `Bash` | Se permite cualquier comando. Las reglas de Bash y de Read/Edit miran el texto del comando, así que un script que abre ficheros por su cuenta se las salta ([límites de las reglas Bash](https://code.claude.com/docs/en/permissions#what-a-bash-rule-doesnt-match)). Por eso existe `sandbox`. |
| `sandbox` (con shell abierta: `shell full`, o los modos `bypassPermissions` y `auto`, que ejecutan Bash sin regla allow) | `sandbox.enabled`, `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed: false` (el sandbox nunca amplía lo que puede ejecutarse), `failIfUnavailable: true`, `denyRead` y `denyWrite` del home de Consiglieri, del `.env`, de los demás bots y de sus carpetas de Claude Code; escritura solo en sus directorios (lo que el sandbox permite por defecto) | **Sí, el sistema operativo** (Seatbelt en macOS) para la shell y sus procesos hijos ([sandboxing](https://code.claude.com/docs/en/sandboxing)). Algunos subcomandos de `alfred` (`send`, `schedule`, `event`, `skills` y `bots`, este solo con el permiso `bots`) corren fuera del sandbox porque necesitan el estado y Telegram; comprueban ellos mismos los permisos. Si el sandbox no puede arrancar, Claude Code no arranca. |
| `edit`, `folders`, `workdir` | `additionalDirectories` y `--add-dir`; allow `Edit(//dir/**)` solo con `edit on`; los dos `block…OutsideWorkingDirectories` | **Sí, para las herramientas de ficheros de Claude Code.** Para la shell, solo con `sandbox`. |
| `web` | allow o deny `WebFetch` y `WebSearch`; `WebFetch(domain:*)` abre también la red del sandbox | **Sí, Claude Code.** |
| `browser` | allow o deny `Bash(orca *)` y `mcp__claude-in-chrome__*`; sin `--chrome` | **Sí, en la forma habitual de invocarlo.** Con `shell full`, la deny de `Bash(orca *)` es por texto y se puede rodear (por ejemplo con `/ruta/orca`). |
| `mcp` | `off`: deny `mcp__*`, que quita todas las herramientas MCP; `on`: no las quita | **Sí, Claude Code.** Con `on`, en modo `dontAsk` cada servidor necesita su regla `allow` (`mcp__<servidor>__*`): Claude Code ignora un `mcp__*` en allow ([tool name wildcards](https://code.claude.com/docs/en/permissions#tool-name-wildcards)). Con `browser on` no se pone el deny global, porque Chrome es un servidor MCP. |
| `schedule` | deny `Bash(alfred schedule *)`, y `alfred schedule` se niega | **Sí: Claude Code y el comando.** |
| `contact` | `alfred send`, `schedule` y `event` se niegan con un chat que no sea del dueño | **Sí, el comando `alfred`.** No cubre otros canales, como el correo o la web con `web`, `browser` o `mcp` activos. |
| `bots` | deny `Bash(alfred bots *)`, y `alfred bots` se niega. Con el permiso: listar, cambiar ajustes de *otros* bots (nunca permisos, nunca `workdir`, nunca los suyos) y pedir bots nuevos, que el dueño confirma en Telegram y que nacen con permisos mínimos. Destruir, solo desde el panel | **Sí: Claude Code y el comando.** |
| `allow`, `deny` | Reglas de Claude Code tal cual | **Sí, Claude Code.** |
| Codex como backend | `--sandbox read-only`, `workspace-write` o `danger-full-access` según `mode`, `shell` y `edit` | Solo eso: Codex no tiene reglas por herramienta. |

Lo que se hace cumplir **fuera** de Claude Code:

- `alfred send --file` solo envía ficheros de los directorios del bot;
- un bot nunca actúa como otro: ni publica, ni programa, ni despierta en su nombre;
- los tokens no pasan al entorno del agente;
- un bot creado por alguien fuera de la lista de permitidos se ignora sin pedir su token;
- cada bot nuevo queda restringido a su dueño en Telegram con `setManagedBotAccessSettings`.

Lo que es **solo instrucción** en el manual del bot (`BOT.md`):

- ceñirse a su tarea;
- confirmar antes de algo destructivo, que cueste dinero o que contacte con terceros;
- tratar el contenido de webs y ficheros como datos;
- no intentar las herramientas que tiene desactivadas;
- no leer el home de Consiglieri. Esto además lo impiden las reglas deny y, con `shell full`, el sandbox.

#### Memoria y herramientas de otros agentes: lista negra y acceso entre agentes

Rama `PabloMoralesEscandon/agent-permissions`. **Requisito:** en todos los niveles (Mínimo, Estándar,
Amplio, el acceso completo y cualquier combinación), un bot no lee la memoria de otros agentes ni usa sus
herramientas. "Otros agentes" son los demás bots y el agente principal de Consiglieri (su home). Cada bot sigue
usando con normalidad su memoria, sus herramientas y la base de Consiglieri (`alfred`, `alfred/plugin`). Por
encima del acceso completo hay un nivel, **acceso entre agentes** (*cross-agent access*), que levanta esa
lista negra.

**Qué es la memoria y qué son las herramientas de un agente:**

- memoria: `memory/`, su historial en `.git/` y la carpeta de Claude Code del agente,
  `~/.claude/projects/<cwd codificado>/`, con sus transcripciones y la auto-memoria de Claude Code;
- herramientas: `extensions/` (plugins, con sus `bin/`), `extensions-disabled/` y `.claude/` (skills,
  agentes, ajustes).

Para cada bot se niega su directorio entero. Para el home de Consiglieri se niega todo el home, como antes.

**La lista negra, en cada nivel por debajo del acceso entre agentes** (`bots.blacklist()`):

| Mecanismo | Qué cubre | Quién lo hace cumplir |
| --- | --- | --- |
| deny `Read(//<dir>/**)` y `Edit(//<dir>/**)` para el directorio de cada otro bot, el home de Consiglieri y la carpeta de Claude Code de cada uno | Read, Edit y Write. Las reglas `Read` también se aplican a Grep y Glob, a los comandos de fichero que Claude Code reconoce en Bash (`cat`, `head`, `tail`, `sed`, `tee`) y a las redirecciones `<` y `>` | Claude Code, en todos los modos, también `bypassPermissions`. Las deny ganan a las allow, así que las reglas extra `allow` del bot no las abren |
| `blockReadsOutsideWorkingDirectories` y `blockWritesOutsideWorkingDirectories`, ya presentes. `folders` y `workdir` siguen rechazando cualquier ruta dentro del home o de `~/.alfred-bots` | Las herramientas de ficheros y los comandos de solo lectura de Bash fuera de los directorios de trabajo | Claude Code |
| deny `Bash(*<ruta>/*)`, `Bash(*<ruta>)` y `Bash(*<ruta> *)` para la memoria y las herramientas de cada agente, en forma absoluta, con `~/` y relativa (`../<id>/memory`) | Que un comando las nombre, por ejemplo para ejecutar `…/extensions/x/bin/y` de otro bot | Claude Code, pero solo por el texto del comando |
| sandbox con `denyRead` y `denyWrite` de todo lo anterior y `denyRead` de todo `~/.alfred-bots` salvo el directorio propio (`allowRead`), siempre que la shell esté abierta y `sandbox` esté activo (el valor por defecto) | Cualquier proceso que lance la shell: scripts, intérpretes, binarios | **El sistema operativo** (Seatbelt en macOS) |
| `disableAllHooks: true` en los ajustes de cada bot. Además, Consiglieri no carga las extensiones de un bot que tengan hooks, monitores o servidores LSP, ni las que tengan servidores MCP sin el permiso `mcp` (`extensions.held_back`) | Un hook o un servidor que el propio bot haya escrito correría como el usuario, fuera de las reglas y del sandbox. Sin esto, cualquier bot podía saltarse la lista negra (y todos sus permisos) escribiéndose una extensión con un hook | Claude Code y Consiglieri, que decide qué `--plugin-dir` pasa |

No hay reglas `Grep(ruta)` ni `Glob(ruta)`: Claude Code las acepta pero no las aplica
([permissions](https://code.claude.com/docs/en/permissions#writepath-globpath-and-greppath-rules-dont-match)).
Su documentación dice que hay que usar `Read(ruta)`, y eso es lo que se hace.

**Por qué no se puede quitar con los interruptores:** el nivel no es un permiso. Es un campo aparte del
registro, `cross_agent`, fuera de `permissions`. Ningún preajuste, interruptor, regla extra, valor de
`/bot perm`, opción de `/bot new` ni comando `alfred` llega a él:

- `clean_permission` y `parse_options` rechazan `cross`, `cross-agent`, `+cross` y `cross=on`;
- `alfred bots set` solo cambia ajustes;
- las peticiones de bots nuevos (`Pending`) no tienen ese campo;
- en `bot.json` solo cuenta un `true` JSON en su propio campo: ni un texto `"true"` ni un
  `cross_agent` dentro de `permissions`. Quien pueda escribir ese fichero sí puede concederlo; ningún
  bot puede, salvo con shell sin sandbox (ver los rodeos).

**El acceso entre agentes:**

- **Quién lo concede:** solo el panel, el usuario permitido que lo pide y solo después de una
  advertencia explícita. Por texto, `/bot perm <bot> cross on` envía un botón que caduca a los 5
  minutos. Un botón de *destroy* no sirve para concederlo, ni al revés. En el menú está en *Mis bots →
  Permisos*, en su propia pantalla de confirmación.
- **Al crear un bot:** nunca se concede. Se da a un bot que ya existe.
- **Dónde se ve:** en `/bots`, `/bot show`, la lista y la ficha del menú (⚠️ CROSS-AGENT ACCESS) y en el
  resumen de permisos.
- **Auditoría:** cada concesión o retirada deja una línea JSON en `<home>/state/audit.log` (0600), con
  hora, evento, bot, usuario y vía. Se registran también las retiradas al destruir el bot y al pasar su
  propiedad a alguien fuera de la lista de permitidos, que además lo retira. `/bot audit` muestra las
  últimas.
- **Qué abre:**
  - lectura de la memoria, `extensions/` y `.claude/skills/` de cada otro agente, y de la auto-memoria de
    Claude Code de cada uno. Se añaden como `additionalDirectories` y, en el sandbox, como `allowRead`
    (la ruta más estrecha gana);
  - ejecutar sus ejecutables, con allow `Bash(<dir>/extensions/*/bin/*)`, también con shell `alfred`;
  - el manual del bot pasa a listar esas rutas.
- **Qué sigue cerrado:**
  - escribir nada suyo (deny `Edit` y `denyWrite`);
  - `state/` y `.env` del home;
  - los `inbox/`, `outbox/` y `.git/` de los demás;
  - sus transcripciones (`*.jsonl`).

  Sus extensiones no se cargan como plugins en el bot: sus hooks y servidores no corren en él.

**Qué queda garantizado:**

- Con shell `alfred` en `dontAsk` o `acceptEdits`, que son los valores por defecto y los de Mínimo,
  Estándar y Amplio sin shell, Claude Code impide leer y ejecutar lo de los demás. Las herramientas de
  ficheros y los comandos que reconoce quedan cubiertos por las reglas deny y por el confinamiento a los
  directorios de trabajo. Cualquier otro comando se deniega por no tener regla allow.
- Con shell abierta y sandbox, que es Amplio, el sistema operativo lo impide para todo lo que lance la
  shell.
- Ningún bot se concede el nivel a sí mismo ni a otro por las vías de Consiglieri.

**Qué rodeos siguen posibles, y lo que avisa de ellos:**

- **Shell abierta sin sandbox:**
  - qué abre: `shell full` con `sandbox off`, o `bypassPermissions` o `auto` con `sandbox off`;
  - el rodeo: el bot puede hacer lo mismo que tu usuario, leer cualquier ruta y ejecutar cualquier
    script. Las reglas Bash solo miran el texto, así que una variable, un glob, un `cd` previo o un
    script que abre el fichero por su cuenta las pasan. Las reglas `Read` siguen cubriendo las
    herramientas de ficheros;
  - quién avisa: el menú (pantalla de permisos y resumen) y el manual del bot.
- **Codex como backend:** su sandbox `read-only` lee todo el disco y no tiene reglas por ruta. La lista
  negra no se hace cumplir; solo el manual la pide. El menú lo avisa.
- **Comandos `alfred` fuera del sandbox:** `send`, `schedule`, `event`, `skills` y, con el permiso
  `bots`, `bots`. Comprueban ellos mismos qué bot los llama, por `ALFRED_BOT_ID` (ver abajo). Ninguno
  lee memoria. `alfred skills` lista nombres y rutas de skills del bot que diga esa variable.
- **Un bot con shell completa sin sandbox:** puede editar su propio `bot.json` (y con ello darse el
  acceso entre agentes) o el registro de auditoría. Solo un usuario del sistema propio o una VM lo
  impide.
- **El acceso entre agentes en sí:** lo que el bot lee de otro agente es texto que puede llevar
  instrucciones. La advertencia y el manual lo dicen, pero es una instrucción, no un mecanismo.
- **Un bot creado mientras otro está a mitad de turno:** entra en las reglas deny del otro en su siguiente
  mensaje, porque los ajustes se regeneran antes de cada uno. Mientras tanto lo cubren el confinamiento
  de las herramientas de ficheros y, con sandbox, el `denyRead` de todo `~/.alfred-bots` (con `allowRead`
  solo del directorio propio). Lo único que se retrasa son las reglas de texto de Bash, que ya eran solo
  una capa extra.

Efecto secundario: al crear o destruir un bot cambian los ajustes de todos los demás, y cada uno reabre
su proceso, con la misma sesión, en su siguiente mensaje.

Visto de paso, sin tocar: con el sandbox, `alfred ext` corre dentro de él y no puede leer
`state/bots/<id>/bot.json` (está en el home). El comando no sabe entonces qué bot es y falla al escribir.
El bot puede crear sus extensiones con las herramientas de ficheros. Pasaba ya antes de estos cambios con
`shell full`.

Límite que conviene tener claro: el comando `alfred` sabe qué bot lo llama por la variable
`ALFRED_BOT_ID`. Con `shell` = `alfred`, Claude Code no deja anteponer variables a un comando permitido
(una regla allow no casa con `VAR=x alfred …`), así que no se puede suplantar. Con `shell full`, el bot
puede cambiar esa variable y hacerse pasar por otro bot ante `alfred`. Y con `shell full` sin sandbox,
o con `bypassPermissions` y Bash libre, el bot puede hacer lo mismo que tu usuario. Para un aislamiento
real en esos casos, Consiglieri debería correr con un usuario propio o en una VM.

#### Memoria compartida

Además de su memoria propia, cada agente lee una memoria general compartida: hechos que sirven a todos
(quién es el usuario, sus preferencias, datos comunes). Es contexto, nunca instrucciones, y nunca guarda
contraseñas, tokens ni claves.

- **Dónde vive**: `~/.alfred-shared/memory/` (se cambia con `ALFRED_SHARED_HOME`). Contiene `MEMORY.md`
  como índice y un archivo por tema en `<carpeta>/<nombre>.md`, igual que la memoria de un agente. Está
  junto a `~/.alfred` y `~/.alfred-bots`, fuera de los dos, porque la lista negra deniega ambos a todos
  los bots y en Claude Code una regla deny gana a cualquier allow. Tampoco está dentro de ningún
  repositorio git: no se versiona ni se publica. La carpeta es `0700` y los archivos `0600`. Si
  `ALFRED_SHARED_HOME` apunta dentro de una zona denegada, el registro lo avisa
  (`shared_memory.location_problem`). Se crea vacía, con solo el índice; no se ha copiado nada de la
  memoria actual.
- **Cómo se carga**: Claude Code no carga un `@import` de fuera del proyecto en una sesión no interactiva
  (pediría aprobación y no hay nadie). Lo probamos con el CLI real: con `@/ruta/absoluta`, con y sin
  `--add-dir`, el modelo no veía el contenido; con una copia dentro del proyecto, sí. Por eso, antes de
  cada proceso, Consiglieri escribe `SHARED_MEMORY.md` en el directorio de cada agente: una cabecera que dice
  qué es, cuánto fiarse y si puede escribir, seguida del índice. El manual lo importa con
  `@SHARED_MEMORY.md`, igual que `@memory/MEMORY.md`. Esto vale para cada bot (`bots.prepare_workspace`) y
  para el agente principal (`home.bootstrap` y `Agent._prepare_workspace`). Codex lo lee porque su
  `AGENTS.md` se lo pide. Si el índice cambia, el proceso se reabre en el siguiente mensaje con el índice
  nuevo (`#shared` en `live_settings`). Los archivos de tema se leen donde viven.
- **Lectura, siempre**: la carpeta va en `additionalDirectories` de cada bot y como `--add-dir` de cada
  proceso de Claude Code. Con sandbox, va también en su `allowRead`. Ninguna regla deny `Read` ni ningún
  `denyRead` la cubre, en ningún preset, con o sin acceso entre agentes. La prueba
  `test_the_shared_memory_is_readable_at_every_level_and_written_only_with_the_permission` lo comprueba
  con todos los presets y con todas las combinaciones de interruptores, modos y shells que ya usa la
  prueba de la lista negra.
- **Escritura, solo con permiso**: cada bot tiene `Edit(//<memoria compartida>/**)` en sus reglas deny,
  que también cubren Write, y la carpeta en el `denyWrite` del sandbox. Esto cambia solo con el permiso
  `shared` (Personalizar → «🧠 Write the shared memory»), que está desactivado en todos los presets,
  Full access incluido. Con él, la regla pasa a allow y la carpeta a `allowWrite`. El aviso dice que los
  demás agentes leerán lo que escriba y que ese texto puede llevar instrucciones. Su copia del índice
  también está en las reglas deny `Edit`, como su `CLAUDE.md`.
- **Quién escribe**: el usuario, desde el panel, con `/shared list|show|add|remove` o con el botón
  «🧠 Shared memory» de `/bot`. El panel escribe los archivos él mismo, sin agente, y rechaza lo que
  parezca un secreto (los mismos patrones que `alfred ext check`) y los nombres de tema que se salgan de
  la carpeta. También escribe el agente principal de Consiglieri, al que su manual pide hechos generales,
  nada leído en webs o mensajes, y nunca secretos.

Rodeos que siguen siendo posibles:

- **Un bot con shell abierta y sin sandbox** (Full access, o `bypassPermissions` con `shell alfred`)
  puede escribir en la carpeta con comandos (`echo >> …`). La regla deny `Edit` solo frena las
  herramientas de archivos y los comandos que Claude Code reconoce como edición. No hay reglas de shell
  sobre esta ruta porque también bloquearían su lectura. Lo que se escriba así lo leerán los demás
  agentes en su siguiente proceso.
- **Codex**: su sandbox lee todo, y con `danger-full-access` también escribe todo. Solo el manual le pide
  no escribir.
- **El agente principal** corre con tus permisos y escribe ahí por diseño. Si una web le cuela un texto
  y él lo guarda, ese texto llega a todos los bots. Su manual y la cabecera de la copia piden tratar lo
  que hay como contexto y no como órdenes, pero eso es una instrucción, no una barrera.
- **La copia** se rehace antes de cada proceso. Un proceso que ya está abierto sigue con el índice
  anterior hasta su siguiente mensaje, y un tema borrado puede seguir en su contexto hasta entonces.

### 6.5 Otras decisiones

- **Un bot en pausa** no recibe mensajes: lo que se le escriba mientras tanto se descarta al reanudarlo
  (`drop_pending_updates`). Sus tareas programadas se posponen 5 minutos cada vez hasta que vuelva, en
  vez de darse por fallidas.
- **Un cambio de token** (el dueño hace `/token` en BotFather) llega como `managed_bot`. El panel pide el
  token nuevo y reconecta el bot. Si el bot pasa a ser de alguien fuera de la lista de permitidos
  (transferencia en BotFather), el panel lo desconecta, borra su token y avisa al dueño anterior.
- **Al destruir un bot** se borran su directorio (memoria, ficheros, extensiones), su estado (ajustes,
  sesiones, token, permisos) y sus tareas programadas. Las de los demás bots no se tocan.
- **El home `~/.alfred`** se conserva: el agente antiguo sigue accesible con `alfred chat`. Los
  recordatorios del chat del panel se siguen entregando; sus tareas se anuncian en vez de ejecutarse.
- **Los temas** funcionan en el chat de cada bot (`message_thread_id`), cada uno con su sesión, y
  `alfred send` y `schedule` responden en el mismo tema. Crear temas desde el panel no está hecho (5.6).

### 6.6 Qué se probó

`PYTHONPATH=tests venv/bin/python -m unittest discover -s tests` pasa **198 tests**: los 125 que había,
61 de la rama `agent-chats` y 12 de la lista negra y el acceso entre agentes (rama `agent-permissions`). Se ejecutaron en un venv con Python 3.9.6 y python-telegram-bot 22.5, con stand-ins para
`claude`, `codex`, `orca` y Telegram.

Uno de los 125 (`test_a_turn_with_no_sign_of_life_is_stopped`) ya fallaba antes de estos cambios en esta
máquina. macOS tarda medio segundo en ejecutar por primera vez un script recién creado, más que el
límite de 0,4 s del test. Se subió a 1,5 s en un commit aparte.

- **`tests/test_bots.py` (27):**
  - usernames: prefijo del panel, acentos, espacios, mayúsculas, emojis, slug sacado de la tarea, recorte a 32 sin `__`, colisiones con número;
  - registro, token `0600` en un directorio `0700` y fuera del historial de git;
  - peticiones pendientes con caducidad;
  - cada bot con su directorio fuera del home y sus propias extensiones;
  - validación de ajustes y permisos;
  - traducción de los permisos a ajustes de Claude Code (por defecto y ampliados, con sandbox);
  - el manual;
  - aislamiento: el agente de un bot corre en su directorio con `--settings`, `--setting-sources`, `--add-dir`, `--permission-mode` y `--model`, sin `TELEGRAM_BOT_TOKEN` en el entorno;
  - dos bots con el mismo `chat_id` no comparten sesión ni memoria, y un tema es otra conversación.
- **`tests/test_cli_bots.py` (8):**
  - `schedule` guarda el bot y el tema, y cada bot ve solo lo suyo;
  - permiso `schedule`;
  - `send` publica con el token del bot y en su tema, y se niega a otros chats sin `contact`, a ficheros de fuera y a actuar como otro bot;
  - `event`;
  - `bots list|set|new` con sus límites;
  - extensiones por bot.
- **`tests/test_panel.py` (15):**
  - creación completa sin reiniciar: el botón de teclado con su JSON, el enlace como segunda opción, el teclado retirado al terminar, el mensaje de servicio ignorado; el bot nuevo contesta con su propio token y su modelo;
  - la tecla "Cancel" descarta la petición y quita el teclado;
  - un bot creado por un desconocido se ignora sin pedir su token;
  - un bot no solicitado queda con permisos mínimos;
  - reconexión por cambio de token;
  - un bot transferido a otra persona se desconecta y se olvida su token;
  - aviso cuando el modo gestor está apagado;
  - bots registrados que se conectan al arrancar;
  - el panel no tiene agente;
  - handlers del panel;
  - el scheduler entrega cada elemento a su bot y su tema, y pospone los de un bot en pausa;
  - una tarea programada corre en el agente de su bot;
  - ajustes y permisos desde el panel;
  - temas;
  - destrucción con confirmación: un desconocido no puede confirmar, un botón viejo caduca, *Cancel* no hace nada, y se borra solo lo del bot destruido.

- **`tests/test_menus.py` (11):**
  - el asistente completo hasta la petición, y el bot creado con lo elegido;
  - *Atrás* paso a paso conservando lo escrito;
  - *Cancelar* cierra el menú, y sus botones dejan de funcionar;
  - un reinicio o un `/bot` nuevo caducan los botones viejos sin enviar nada;
  - solo el dueño del menú, y solo un usuario permitido, puede pulsar;
  - entradas inválidas que se explican y se vuelven a pedir;
  - modelos y esfuerzos de Codex;
  - avisos de las opciones peligrosas y preajustes;
  - 64 bytes en todos los botones;
  - Mis bots: modelo, esfuerzo, tarea, permisos, pausar, reanudar y destruir con confirmación.

- **Lista negra y acceso entre agentes (12):**
  - la lista negra está en cada nivel: Mínimo, Estándar, Amplio, acceso completo con y sin sandbox,
    `auto`, `bypassPermissions` con shell `alfred`, reglas extra `allow` que nombran a otro bot, y cada
    combinación de modo, shell e interruptores (`test_bots.py`);
  - las shells abiertas llevan sandbox salvo con `sandbox off`;
  - ningún interruptor, valor, opción, preajuste ni edición a mano de `permissions` la quita;
  - el nivel la levanta: abre memoria, extensiones y skills para leer y ejecutar, y mantiene cerrados
    `state/`, `.env`, inbox, `.git` y transcripciones;
  - el manual y los resúmenes lo dicen;
  - el registro es privado y solo añade;
  - las extensiones de un bot con hooks o MCP (sin `mcp`) no se cargan;
  - un bot no se lo concede a sí mismo ni a otro con `alfred bots set` o `alfred bots new`, ni el
    operador desde el CLI (`test_cli_bots.py`);
  - panel por texto (`test_panel.py`): `/bot new +cross` rechazado; confirmación que un desconocido no
    puede pulsar y que caduca; un botón de *destroy* no concede y uno de concesión no destruye; las
    reglas del bot cambian en su siguiente mensaje y las del otro no; `/bots` y `/bot show` lo marcan;
    `off`; destrucción y transferencia lo retiran; cada paso en el registro y en `/bot audit`;
  - menús (`test_menus.py`): no aparece al crear; advertencia, *No, back*, conceder, preajustes que no
    lo tocan, marcas en la ficha y la lista, retirar, auditoría; botones de 64 bytes; un desconocido no
    concede, y la acción fuera de su pantalla solo la muestra.

### 6.7 Qué queda por probar en vivo

- Memoria compartida, con Claude Code real:
  1. Tras `/shared add user …`, un bot Basic responde con ese hecho sin abrir archivos (el índice está en
     contexto) y abre un archivo de tema cuando hace falta.
  2. Sin el permiso `shared`, el mismo bot no puede editar ni crear un archivo allí.
  3. Con Power, el sandbox de macOS deja leer la carpeta (`cat`) y no escribirla.
  4. Tras el siguiente arranque, el agente principal ve `~/.alfred/SHARED_MEMORY.md` y escribe un tema
     nuevo cuando se le pide.

Nada de lo que sigue se ha probado con Telegram ni con Claude Code reales. Los pasos asumen un bot de
pruebas y un home aparte, para no tocar `~/.alfred` ni el servicio en marcha:

1. En @BotFather, crea un bot de pruebas para el panel (`/newbot`). Abre el Mini App (botón *Open*),
   elige ese bot y activa **Bot Management Mode**.
2. Crea `~/.alfred-test.env` con `TELEGRAM_BOT_TOKEN=<token del bot de pruebas>` y
   `ALFRED_ALLOWED_USERS=<tu id de Telegram>`.
3. Arranca desde este worktree con su propio código. Un `venv/bin/alfred` instalado en modo editable desde
   otra copia (por ejemplo el worktree `refactor`) ejecuta el código de esa copia. Para saber a cuál
   apunta: `venv/bin/python -c "import alfred; print(alfred.__file__)"`. Hay dos formas:
   - sin instalar nada, con el código de esta carpeta delante:
     `ALFRED_HOME=~/.alfred-test ALFRED_ENV_FILE=~/.alfred-test.env PYTHONPATH=$PWD venv/bin/python -m alfred run`
   - o reinstalando aquí en modo editable y usando `venv/bin/alfred`:
     `venv/bin/pip install --upgrade pip setuptools && venv/bin/pip install -e .`, y después
     `ALFRED_HOME=~/.alfred-test ALFRED_ENV_FILE=~/.alfred-test.env venv/bin/alfred run`.
     Hace falta actualizar pip antes: el pip 21.2 que trae el venv de Python 3.9 rechaza la instalación
     editable con `pyproject.toml` ("editable mode currently requires a setuptools-based build").

   Comprobado el 12 de noviembre de 2026: tras reinstalar, `venv/bin/alfred bots list` ejecutado desde
   `/` sin `PYTHONPATH` carga `refactor-2/alfred/telegram_bot.py`, con `ControlPanel`. Los bots irán a
   `~/.alfred-test-bots`.
4. En el chat del panel, `/status`. Debe decir "bot management on". Si dice "unknown", el `getMe` real
   no trae `can_manage_bots` como se espera.
5. `/bot new Prueba Responde siempre en español y en una frase.` El panel envía dos mensajes. El primero
   trae un teclado con "Create @prueba_bot" (`KeyboardButtonRequestManagedBot`) y "Cancel". Pulsa
   "Create", confirma en la ventana que abre Telegram y espera el mensaje "@… is ready", que también
   quita el teclado. El segundo mensaje trae el enlace `t.me/newbot/…` como alternativa. Comprueba que el update `managed_bot` llegó y que el token se guardó:
   `ls -l ~/.alfred-test/state/bots/*/` debe mostrar `token` con `-rw-------`.
   Prueba también el menú: `/bot`, *Crear bot*, recorre los pasos con *Atrás*, y comprueba en el
   resumen que el username extiende el del panel. Después, en *Mis bots*, cambia el modelo y un permiso.
6. Abre el bot nuevo, pulsa *Start* y escríbele. Debe contestar él, no el panel. En su `/status` debe
   aparecer su directorio en `~/.alfred-test-bots/<id>`.
7. Permisos con un Claude Code real. Revisa el log por avisos de Claude Code sobre los ajustes; `claude
   doctor` desde el directorio del bot lista las entradas rechazadas. Después prueba:
   - pídele que busque algo en la web: debe decir que no puede;
   - `/bot perm Prueba web on` y repite: debe poder;
   - pídele `ls ~` (debe negarse: shell `alfred`) y que lea `~/.alfred-test/.env` (debe negarse).
8. Pídele "recuérdame en 2 minutos X". El recordatorio debe llegar en el chat de ese bot, no en el panel.
9. Crea un segundo bot y confirma que no ve la memoria ni los recordatorios del primero.
10. Con `/bot perm Prueba shell full`, comprueba que el sandbox arranca en macOS: un comando `touch ~/x`
    debe fallar con "Operation not permitted" y `alfred send` debe seguir funcionando.
11. En BotFather, regenera el token del bot gestionado. El panel debe decir que se reconectó. Esto
    comprueba el formato real del update en un cambio de token.
12. `/bot pause Prueba`, escribe al bot (no debe contestar) y `/bot resume Prueba`.
13. `/bot destroy Prueba` y confirma. Comprueba que `~/.alfred-test/state/bots/<id>` y
    `~/.alfred-test-bots/<id>` ya no existen. Luego borra el bot en BotFather con `/deletebot`.
14. Opcional, temas: activa los temas en chats privados para un bot (BotFather) y escribe dentro de un
    tema. La respuesta debe llegar en el mismo tema.

15. Lista negra con un Claude Code real, con dos bots de pruebas A y B. Guarda algo en la memoria de B y en
    la de Consiglieri (`~/.alfred-test/memory/`), y crea en B una extensión con un `bin/` que imprima algo.
    - Con A en Mínimo, pídele que lea la memoria de B, que la busque con Grep, que haga `cat` de su
      ruta, `ls ../<id de B>` y que ejecute el script de B por su ruta. Todo debe fallar. `claude doctor`
      desde el directorio de A no debe listar reglas rechazadas.
    - Con A en Amplio (sandbox), pide `python3 -c "print(open('<ruta de B>/memory/MEMORY.md').read())"`.
      Debe fallar con "Operation not permitted".
    - Con `shell full` y `sandbox off`, el mismo comando funciona. Es el rodeo documentado, y el menú
      debe mostrar el aviso.
    - Pide a A que se escriba una extensión con un hook `SessionStart` que lea la memoria de B. En el
      siguiente mensaje el hook no debe correr, y `alfred ext check` debe listarlo en
      `not_loaded_for_this_bot`.
16. `/bot perm A cross on`: lee la advertencia, confirma y repite el paso 15 con A en Mínimo. Ahora A
    debe poder leer la memoria de B y de Consiglieri y ejecutar el script de B. No debe poder editarlos, ni
    leer `~/.alfred-test/.env`, `state/` o el `inbox/` de B. Comprueba `/bots`, `/bot audit` y
    `~/.alfred-test/state/audit.log`. Luego `/bot perm A cross off`, y en el siguiente mensaje todo debe
    volver a fallar. Prueba lo mismo desde *Mis bots → Permisos*.
17. Con A en Amplio, comprueba que su shell sigue leyendo su propio directorio (`allowRead` dentro del
    `denyRead` de `~/.alfred-bots`). Con `cross on`, comprueba que el sandbox deja leer la memoria de B (`allowRead` dentro de
    un `denyRead`, la regla más estrecha gana) y no deja escribirla (`denyWrite` sobre un directorio que
    también es `additionalDirectories`).

Puntos que la documentación no aclara y que esos pasos resuelven:

- si `getMe` devuelve `can_manage_bots` (paso 4);
- qué apps de Telegram abren la ventana de creación con el enlace `t.me/newbot`. En la primera prueba en
  vivo, el enlace abrió una página web y no la ventana; por eso el botón de teclado es ahora la vía
  principal (paso 5);
- el contenido de `managed_bot` en un cambio de token (paso 11);
- si `denyWrite` gana a la escritura que el sandbox concede por defecto a los `additionalDirectories`, y si
  `disableAllHooks` en `--settings` apaga también los hooks de plugins (pasos 15 a 17);
- si el nombre de la carpeta de Claude Code de cada agente coincide con el que calcula
  `bots.claude_project_dir` (cada carácter que no es letra ni número pasa a `-`; las rutas muy largas
  pueden acortarse con un hash, y entonces la regla no casaría). Compruébalo con `ls ~/.claude/projects`
  tras el paso 6;
- si este Claude Code (2.1.322) acepta todas las claves del fichero `--settings` y aplica
  `--setting-sources` y `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` como dice su documentación
  (pasos 6, 7 y 10).

Para pasar después al servicio real: `venv/bin/alfred service install` desde este worktree, una vez
desinstalado el actual (que apunta al worktree `refactor`). Hay que activar antes Bot Management Mode en
el bot de verdad. Al hacerlo, ese bot pasa a ser el panel y deja de contestar como Consiglieri (sección 6.5).

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

Claude Code (permisos y carga de instrucciones), consultada el 12 de noviembre de 2026:

- Permisos (reglas, rutas, directorios de trabajo, precedencia): https://code.claude.com/docs/en/permissions
- Modos de permisos (`dontAsk`, `bypassPermissions`, deny en todos los modos): https://code.claude.com/docs/en/permission-modes
- Ajustes (`--settings`, precedencia, `blockReads…`, `denyInsteadOfPrompting`): https://code.claude.com/docs/en/settings
- Sandbox de Bash: https://code.claude.com/docs/en/sandboxing
- CLI (`--setting-sources`, `--add-dir`, `--allowedTools`): https://code.claude.com/docs/en/cli-reference
- Carga de `CLAUDE.md` y `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`: https://code.claude.com/docs/en/memory

Fuera de Telegram:

- python-telegram-bot, versiones y fechas (22.8, del 12 de junio de 2026, añade los métodos de bots
  gestionados): https://pypi.org/project/python-telegram-bot/
