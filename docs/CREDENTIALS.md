# Credenciales para los agentes

Estado: implementado en la rama `credentials`, sin fusionar ni publicar. Fecha: 25 de noviembre de 2026. Sustituye
al primer diseño de esta rama (secretos en el Llavero de macOS y ayudantes propios `alfred creds fill|totp|http`), que
se descartó y queda en la rama `credentials-keychain-old`. Probado de punta a punta con un home temporal, dos bots,
KeePassXC 2.8.0, KeePassXC-Browser 1.10.4.1, el Chromium de Playwright 1.60 y una web de inicio de sesión local con
TOTP (§9). Sin probar con una web real (§10).

## 1. El problema

Un bot entra en una web solo con la sesión que el usuario abrió a mano en su navegador. Cuando caduca (Moodle y
TUMonline, con SSO en `login.tum.de` y a veces 2FA), el bot se para hasta que el usuario vuelve a entrar. Se quiere que
un bot pueda iniciar sesión solo, con credenciales que el usuario concede bot a bot, sin ver nunca el secreto.

Condición del usuario: **no depender del harness, del modelo ni del entorno** donde corre Consiglieri. Nada de
Claude in Chrome, de reglas de Claude Code como mecanismo de seguridad, del Llavero de macOS ni de una integración
propia de un proveedor.

## 2. La solución, en una frase

Las credenciales viven en **KeePassXC**, y su extensión **KeePassXC-Browser**, cargada en un **navegador propio de
cada bot**, rellena el formulario sola, solo en los sitios de la credencial. Qué bot recibe qué credencial lo decide
el mecanismo propio de KeePassXC: una base de datos por credencial, que responde solo a las identidades de los bots a
los que se concedió.

| Pieza | Qué es | Por qué no depende de nada |
|---|---|---|
| Almacén | KeePassXC (libre, GPL, macOS/Linux/Windows), una base `.kdbx` por credencial, abiertas con un fichero llave | Formato KDBX estándar; se maneja con `keepassxc-cli` |
| Relleno | KeePassXC-Browser, la extensión oficial, en versión fijada y comprobada por su hash | Funciona en cualquier Chromium; el modelo no interviene |
| Navegador | Un Chromium por bot (el de Playwright, que aún carga extensiones desempaquetadas), con su perfil | Cualquier agente lo maneja con órdenes de shell (`alfred browser ...`) por CDP |
| Permisos | Las identidades de KeePassXC, más el permiso `creds` de cada bot | La separación la hace KeePassXC, no una regla de texto |

Alternativas estudiadas y descartadas (precios a noviembre de 2026): 1Password for Claude (atado a Claude y a Mac,
y pide Touch ID en cada tarea), Bitwarden (TOTP solo con Premium, 19,80 $/año) o Vaultwarden (gratis, pero un
servidor que mantener), Proton Pass, Playwright MCP `--secrets` (sin límite por dominio) y browser-use (su propio
bucle de agente). Para tokens de API, un proxy que inyecta el token (Infisical Agent Vault, Fly.io tokenizer) sigue
siendo la vía; no está en esta rama (§10).

## 3. Cómo funciona

```
<home>/state/credentials/         0700, dentro de state/: cerrado a todos los bots
    master.key                    0400, abre todas las bases (sin contraseña: los bots trabajan desatendidos)
    hub.kdbx                      vacía; la identidad de cada bot con navegador. Siempre la base ACTIVA
    tum.kdbx, mail.kdbx, ...      una credencial cada una (usuario, contraseña, URLs, semilla TOTP)
                                  + las identidades de los bots a los que se concedió
    identities.json               0600, {bot_id: {"id": "alfred-<id>", "key": <32 bytes aleatorios>}}
    keepassxc.ini                 los ajustes de esta instancia de KeePassXC (nunca los del usuario)
<home>/state/browsers/<bot_id>/   0700: el perfil de Chromium del bot, su run.json y su registro
<home>/state/browser-extensions/keepassxc-browser-1.10.4.1/   la extensión, desempaquetada
```

- **Identidad.** La extensión se conecta a KeePassXC con un nombre y una clave, y KeePassXC guarda cada identidad
  aceptada en los metadatos de la base (`KPXC_BROWSER_<nombre>`). Consiglieri genera la de cada bot y la siembra a la
  vez en su extensión (por CDP, en su proceso de fondo) y en las bases; así no hay diálogo «Conectar» que nadie pulse.
- **La base central.** La extensión comprueba primero la base activa de KeePassXC; si no la conoce, no pide nada. Por
  eso `hub.kdbx` está vacía, conoce a todos los bots y se abre la última, para quedar activa. Con
  `SearchInAllDatabases`, KeePassXC busca después en las demás bases abiertas, **y cada una solo responde a las
  identidades que contiene**.
- **Conceder** es añadir la identidad del bot a la base de la credencial; **retirar**, quitarla. `keepassxc-cli` no
  sabe hacerlo (tampoco poner el TOTP ni URLs extra), así que la base se reescribe entera: exportada a XML en memoria,
  cambiada e importada de nuevo por una tubería con nombre, sin que el XML en claro toque el disco. KeePassXC, abierto,
  ve el cambio y recarga.
- **KeePassXC** se abre con `--pw-stdin` y el fichero llave, sin bloqueos por inactividad, con la integración del
  navegador activa, sin preguntar al acceder, ofreciendo solo la mejor coincidencia y negando la petición que lista
  todos los apuntes de una base. Lo arranca la primera orden de navegador de un bot con `creds`, y se reinicia al
  añadir o borrar una credencial (solo abre bases al arrancar).
- **El navegador** de un bot arranca sin ventana en su primera orden. Con `creds` carga la extensión, con un manifiesto
  de *native messaging* en su propio perfil que la deja hablar con KeePassXC; sin `creds`, no carga ninguna extensión.
  Cambiar el permiso reinicia el navegador en la orden siguiente.

## 4. Uso

En la máquina, en una terminal:

```bash
brew install --cask keepassxc
venv/bin/pip install -e '.[browser]' && venv/bin/alfred browser install
echo ALFRED_BROWSER=bot >> ~/.alfred/.env      # todos los bots; o solo uno: /bot set <bot> browser bot
venv/bin/alfred creds setup
venv/bin/alfred creds add tum --url moodle.tum.de --url login.tum.de --username ge12abc --totp
venv/bin/alfred creds grant tum @uni_bot            # y /bot perm uni_bot creds on
venv/bin/alfred creds list | revoke | remove | status | start | stop
```

- `add` pide la contraseña dos veces, y con `--totp` la clave TOTP (el enlace `otpauth://` del QR o su base32), con
  `getpass`. Se niega sin terminal: el secreto nunca entra por un argumento ni por una tubería. `--replace` lo cambia
  y mantiene las concesiones.
- Para un SSO, todas las URLs del recorrido: el sitio y el proveedor de identidad.
- `grant` y `revoke` dejan una línea en la auditoría (`/bot audit`) y un aviso en el chat del bot, y dicen qué le falta
  al bot (el permiso `creds`, el de navegador o `ALFRED_BROWSER=bot`).

El bot: `alfred creds list` (sus credenciales: nombre, usuario, sitios, si hay TOTP; nunca el secreto) y su navegador.
La skill `web-logins` le dice qué hacer: cargar la página de inicio de sesión, comprobar en la instantánea que el
usuario está y la contraseña sale como `[hidden]`, pulsar «Entrar»; lo mismo con el código; un solo intento, y si
algo falla, pasárselo al usuario (con `alfred browser show` para que tenga ventana).

## 5. Lo que el bot ve

- **Instantáneas, texto, títulos y URLs** pasan por `scrub`: se sustituyen por `[hidden]` el valor de cada campo de
  contraseña y cada contraseña y código TOTP vigente (anterior, actual y siguiente) de las credenciales del bot. Hace
  falta: el árbol de accesibilidad de Playwright **muestra en claro el valor de un campo de contraseña**.
- **Capturas**: se tapan los campos de contraseña y cualquier campo cuyo valor sea un secreto del bot (el código TOTP
  está en un campo de texto normal y salía en la captura).
- No hay orden para ejecutar JavaScript ni para leer el valor de un campo.

## 6. Permisos

- **`creds`**, apagado por defecto y en todos los presets y plantillas (Full access incluido), nunca lo da un
  administrador de bots. Sin él, Claude Code niega `alfred creds` y su navegador no carga la extensión.
- **Siempre negado a todo bot**: `alfred creds setup|add|remove|grant|revoke|start|stop`, `alfred browser install` y
  cualquier orden que nombre KeePassXC. Además esas órdenes se niegan dentro del turno de un bot (`ALFRED_BOT_ID`).
- `alfred browser` comprueba el bot con `ALFRED_BOT_KEY`: un bot no puede usar el navegador de otro cambiando
  `ALFRED_BOT_ID`. Con sandbox, `alfred browser *`, `alfred creds list` y `status` corren fuera de él (arrancan
  Chromium y leen el estado del panel) y comprueban los permisos en Python.
- Destruir un bot para su navegador, borra su perfil y lo saca de todas las bases.

## 7. Modelo de amenazas

| Amenaza | Qué pasa |
|---|---|
| Una página pide al bot que teclee la contraseña en otra web | El bot no la tiene. La extensión rellena según la URL de la página, y KeePassXC solo devuelve credenciales cuyo sitio coincide |
| Una redirección lleva la pestaña a otro dominio | La extensión vacía su caché de credenciales de la pestaña al cambiar la URL (probado: no rellena en el otro dominio) |
| El bot lee la contraseña del campo | No hay orden para ello y lo que lee pasa por `scrub`. Con shell abierta podría hablar por CDP con su navegador y leerla |
| Un bot usa las credenciales de otro | KeePassXC no le responde: su identidad no está en esas bases (probado). Su orden `alfred browser` no alcanza el navegador del otro |
| Usar mal la credencial en el sitio bueno | No lo impide nada, igual que con una sesión abierta. Mitigan: credenciales de mínimo alcance, la skill (solo para la tarea del usuario) y la auditoría |
| Retirar una credencial | Efecto en la siguiente carga de página. Una sesión ya abierta sigue hasta que caduque o se cierre |

**Lo que no protege**, con un bot de **shell abierta** (`shell full` o `bypassPermissions`), que corre como el usuario:

- El puerto CDP de cada navegador escucha en `127.0.0.1` sin autenticación: cualquier proceso del usuario puede manejar
  el navegador de cualquier bot y leer lo que rellena.
- `state/credentials/` (las bases y el fichero llave) está negado por reglas de texto y, con sandbox, por el sandbox;
  sin sandbox, se puede leer.
- Por eso: credenciales solo a bots Basic o Assistant (`shell alfred`). La separación fuerte de verdad es otro usuario
  del sistema (o un contenedor) para los bots, con KeePassXC y los navegadores en el del usuario: un cambio de
  arquitectura que no está en esta rama.

Otros:

- **Desatendido = el fichero llave está en el disco.** Quien lea `state/` tiene todas las credenciales, igual que tiene
  ya los tokens de los bots. Las bases solo protegen en reposo (copias, otro usuario).
- **TOTP junto a la contraseña** convierte el 2FA en un solo factor para quien tenga la máquina. Se decide credencial a
  credencial (`--totp` o no).
- **Una sola instancia de KeePassXC por usuario** sirve a los navegadores. Si el usuario usa KeePassXC para sí mismo en
  esa cuenta, choca: `alfred creds start` se niega mientras haya otro abierto.

## 8. Lo que se encontró al probarlo

Comportamientos de la extensión que el código tiene en cuenta (`BotBrowser._seed` y `wake_extension`):

- Al arrancar su proceso de fondo, la extensión reescribe sus ajustes con los valores por defecto: hay que esperar a que
  termine antes de sembrar los nuestros y comprobar que se quedaron. Fuerza `autoReconnect` a falso en cada arranque.
- Si arranca antes de que KeePassXC esté listo, su primer intento de conexión caduca y la marca como no disponible:
  antes de cada orden que carga o toca una página se comprueba la conexión **y** la asociación (`testAssociation`).
- Tras recargar KeePassXC una base (conceder, retirar), la extensión sigue pareciendo conectada pero ya no asociada.
- Guarda las credenciales de cada pestaña mientras la URL no cambie, y responde vacío a una petición repetida (misma
  página, misma pestaña): sin vaciar esa caché, retirar no surtía efecto al recargar y conceder tampoco.
- Solo rellena en la pestaña activa: cada orden trae su pestaña al frente.
- Las referencias del árbol (`e12`) no sobreviven a una conexión CDP nueva: cada orden vuelve a sacar el árbol antes de
  actuar (en una página que no cambió, salen iguales).

## 9. Pruebas

Unitarias (`tests/test_credentials.py`, 35; las del almacén usan el `keepassxc-cli` real y se saltan sin él): vectores
TOTP del RFC 6238, nombres y URLs, alta, concesión, retirada, sustitución con concesiones, olvido de un bot, que no
queda XML en claro, hash y rutas de la extensión, manifiesto solo con `creds`, órdenes negadas en el turno de un bot,
`list` de cada bot, reglas de Claude Code en cada caso, preset, administrador de bots y panel.

De punta a punta, en un home temporal con dos bots (A con la credencial, B con el permiso pero sin ella):

- Arranque en frío (sin KeePassXC ni navegador, perfil nuevo): 4 s hasta la página rellena.
- A: usuario y contraseña rellenos (`[hidden]` en la instantánea), «Entrar», código TOTP relleno y correcto, captura con
  los campos tapados.
- B: conectado a KeePassXC, 0 credenciales; no puede usar el navegador de A ni haciéndose pasar por él.
- Dos credenciales en dos hosts: cada una solo en el suyo, en 16 combinaciones de conceder y retirar, en pestañas viejas
  y nuevas, recargando la misma URL; redirección de la página a otro dominio, sin relleno.
- `creds` apagado: el navegador se reinicia sin extensión; encendido, vuelve a rellenar. Sin `browser` o con otro
  `ALFRED_BROWSER`, rechazo claro.
- Añadir una credencial reinicia KeePassXC y el navegador se reconecta solo.
- Destruir un bot: fuera de las bases, de la central y de `identities.json`; navegador parado y perfil borrado.

Una observación sin explicar: en una pasada, tras añadir y conceder una segunda credencial, la instantánea de
`127.0.0.1` mostró el usuario de `localhost`. No se repitió en ninguna de las pruebas siguientes (incluidas las de
redirección); lo más probable es que esa navegación fallara y la pestaña siguiera en `localhost`, porque la prueba
descartaba la salida de `goto`.

## 10. Lo que falta

- **Probar con webs reales**: el inicio de sesión de la TUM (`login.tum.de`, SSO, 2FA) y una web con el formulario en
  dos pasos o en un iframe.
- **`alfred browser show`** (ventana visible) no se probó en vivo, para no robar el foco del escritorio.
- **Linux sin escritorio**: KeePassXC es una app gráfica; en un servidor necesitaría un escritorio virtual. Windows no
  está soportado (tuberías con nombre).
- **Codex**: `alfred browser` es una orden de shell y debería funcionar igual, pero su sandbox puede impedir arrancar
  Chromium; sin probar.
- **Tokens de API**: fuera de esta rama; la vía es un proxy que inyecte el token (§2).
- **Aviso en cada uso**: la extensión rellena sin pasar por Consiglieri, así que solo se auditan concesiones y
  retiradas, no cada inicio de sesión.
- **El panel**: ver y retirar credenciales desde Telegram (`/creds`); hoy solo en la terminal.
- **Por defecto** sigue `ALFRED_BROWSER=orca`; los navegadores propios se activan a mano, para todos o bot a bot
  (`/bot set <bot> browser bot`).
