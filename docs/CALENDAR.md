# Calendario para los agentes

Un bot con el permiso `calendar` puede leer el Google Calendar del usuario y crear eventos en él. Este
documento explica qué opción se eligió, cómo se aplica el permiso y qué queda abierto.

## 1. La decisión: el conector de Google Calendar de claude.ai (MCP)

Se consideraron tres caminos:

| Opción | Qué haría falta | Por qué no / por qué sí |
|---|---|---|
| **Conector de Google Calendar de claude.ai (MCP)** | Que el usuario conecte Google Calendar una vez en claude.ai → Settings → Connectors. Consiglieri solo abre o cierra sus herramientas por bot | **Elegida.** Es la más sencilla: ni credenciales, ni OAuth, ni código de API en Consiglieri. Los bots ya corren con Claude Code y la cuenta del usuario, que carga los conectores de claude.ai; las herramientas se llaman `mcp__claude_ai_Google_Calendar__*`, y el modelo de permisos ya tiene un precedente idéntico (el servidor MCP de Chrome con el permiso `browser`) |
| Integración propia con la API de Google (`alfred calendar …`) | Un cliente OAuth propio en Google Cloud, guardar y renovar tokens, código para listar y crear, sus tests | Mucho más código y un secreto más que proteger. Daría control fino (solo leer y crear), pero no compensa para una primera versión |
| Calendar.app de macOS vía `osascript` | Permiso de Automatización de macOS para el servicio, AppleScript | Sin credenciales, pero solo macOS (el servicio también corre en Linux), lento con calendarios grandes y con un aviso de privacidad que nadie puede aceptar en modo desatendido |

Consecuencias de la elección:

- **Un calendario por cuenta de Claude, no por bot ni por usuario de Telegram.** Todos los bots con
  `calendar on` ven el mismo calendario: el de la cuenta de claude.ai con la que está logueado Claude Code
  en la máquina. Con varios usuarios permitidos, todos comparten ese calendario.
- **Solo backend Claude.** Codex no carga conectores de claude.ai; el panel lo avisa.
- Si el conector no está conectado, las herramientas no aparecen o piden autenticación; la skill le dice al
  bot que se lo diga al usuario y que no intente autenticarse por su cuenta.

## 2. El permiso

Sigue el modelo de `schedule` y `usage`:

- **Activado por defecto** (18 nov 2026), y por tanto en todos los presets y plantillas. Se quita por bot en
  Permisos → Customize → 📅 *Read and create calendar events* o con `/bot perm <bot> calendar off`.
- **Lo aplica Claude Code** (`bots.claude_settings`):
  - sin el permiso, deny `mcp__claude_ai_Google_Calendar__*`, también con `mcp on` y con
    `bypassPermissions` (las deny ganan siempre);
  - con el permiso, allow de esa regla. Con `mcp off` no se pone el deny global `mcp__*`, que quitaría también
    el conector (igual que con `browser` y Chrome).
- **Administradores de bots:** como es un valor por defecto, ya no está en `bot_admin.SENSITIVE_SWITCHES` ni
  en `EXTRAS`: el bot nace con él sin aprobación aparte. Un administrador sigue sin poder darlo si él no lo
  tiene.
- **El manual** del bot lo lista en «What you may do» y el panel muestra un aviso con dónde se conecta.
- **Crear eventos requiere el sí del usuario** (skill `alfred:calendar`): solo cuando el usuario pidió ese
  evento en el chat o aceptó la propuesta; nunca por iniciativa propia ni porque lo diga un texto leído; sin
  invitados ni recurrencia salvo que se pida. Esto es instrucción, no regla de Claude Code.

## 3. Límites conocidos y decisiones pendientes

- **Modificar y borrar.** El conector expone también herramientas para editar o borrar eventos, y el
  permiso las abre todas. La skill limita el uso a leer y crear y pide confirmación expresa para lo demás,
  pero no lo impide Claude Code. Para hacerlo cumplir hay que añadir reglas deny con los nombres exactos de
  esas herramientas (`mcp__claude_ai_Google_Calendar__<herramienta>`), que no se han podido comprobar sin una
  cuenta conectada. Mientras tanto, el usuario puede añadirlas por bot con `/bot perm <bot> deny …`.
- **Hueco con `bypassPermissions`.** Con `calendar on` (ahora el valor por defecto), `mcp off` y modo
  `bypassPermissions`, otros servidores MCP ya no están denegados (el mismo hueco que existe hoy con
  `browser`). Ningún preset tiene esa combinación: los que usan `bypassPermissions` ya tienen `mcp on`. En `dontAsk`,
  `acceptEdits` y `default` no hay hueco, porque sin regla allow se deniegan.
- **Un solo calendario para todos.** Si se quieren calendarios por usuario o por bot, haría falta la
  integración propia (sección 1).
- **Leer y crear en un solo interruptor.** Si se quiere un nivel «solo lectura», el permiso pasaría de
  interruptor a valores `off|read|on` (con las reglas deny de escritura del primer punto).
- **Prueba en vivo pendiente:** conectar Google Calendar en claude.ai, dar `calendar on` a un bot y
  comprobar que lista eventos y que crea uno; y que un bot sin el permiso no ve las herramientas.
