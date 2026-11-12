# Monitor de uso de la suscripción de Claude: investigación y diseño

Estado: monitor implementado en parte y **desactivado por defecto** (`ALFRED_USAGE_MONITOR=0`). Hay un
permiso `usage` por bot (sección 11), avisos para agentes con condiciones sobre el uso (sección 12) y una
estimación de cuánto del semanal consume una ventana de 5 h (sección 10). Los avisos al usuario
(R1-R6) siguen sin lógica. Estado detallado y pendientes: [sección 9](#9-estado-de-la-implementación).
Fecha: 2026-11-12. Claude Code probado: v2.1.322.

Objetivo: que Alfred (1) avise antes de que se agoten los límites del plan (ventana de 5 horas y
límites semanales) y (2) detecte cuota sin usar antes de que se reinicie, para avisar al usuario o
proponerle tareas pendientes que la aprovechen. Todo ello sin que el propio monitor gaste cuota.

## Resumen

- **La fuente correcta ya está a mano y no cuesta nada.** Pasar `/usage` a `claude -p` lo resuelve
  Claude Code en local, sin llamar al modelo: 0 tokens, `total_cost_usd: 0` y alrededor de 1 s.
  `alfred/usage.py` ya lo usa, pero lee el texto con una expresión regular. Si se pide con
  `--output-format stream-json --verbose`, la misma respuesta trae un objeto **`usage_report`
  estructurado y documentado**: cada medidor con `kind`, `percent`, `resets_at` en ISO 8601 UTC,
  `severity` y `scope`. Es mucho más fiable que analizar el texto.
- **Fuente pasiva y gratuita:** cada turno normal de Alfred ya emite un evento `rate_limit_event`
  con la utilización y el reinicio de las ventanas de 5 h y 7 días. Hoy `alfred/backends/live.py` lo
  descarta. Capturarlo da datos frescos sin ninguna consulta extra mientras Alfred trabaja.
- **El monitor no debe ser una tarea programada del agente.** Una entrada `alfred schedule --task`
  despierta al modelo y gasta cuota en cada comprobación. Tiene que ser un bucle en Python dentro del
  proceso del bot, junto al planificador, que solo mande mensajes fijos por Telegram. El agente solo
  se despierta cuando el usuario acepta aprovechar la cuota sobrante.
- **No usar** el endpoint no documentado `api.anthropic.com/api/oauth/usage` ni leer el token OAuth
  del llavero. Es frágil y choca con los términos sobre credenciales. Las herramientas que estiman a
  partir de los JSONL locales (ccusage y similares) no ven los límites reales del plan.
- **Recomendación:** pasar `usage.py` a `usage_report` (con el texto actual como respaldo),
  capturar `rate_limit_event`, y añadir un `UsageMonitor` con reglas deterministas para umbrales,
  proyección, cuota sobrante y reinicios. Por defecto solo avisa y propone, nunca ejecuta solo.

## 1. Cómo funciona hoy `alfred/usage.py`

`fetch()` lanza:

```
claude --print --output-format json --no-session-persistence     (stdin: "/usage")
```

con `cwd=config.home` y sin las variables de sesión heredadas (`INHERITED_SESSION_VARS`). Del
evento `result` toma `result` (texto) y `summarize()` se queda con las líneas que casan con:

```
^Current (?P<label>[^:]+):\s*(?P<percent>\d+)% used(?:\s*·\s*resets (?P<reset>.+?))?(?:\s*\([^)]*\))?\s*$
```

### Lo que se comprobó en esta máquina

Respuesta real (recortada) de `--output-format json`:

```json
{
  "type": "result", "subtype": "success", "is_error": false,
  "local_command": "usage", "num_turns": 0, "duration_ms": 266,
  "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0, "...": 0},
  "result": "You are currently using your subscription to power your Claude Code usage\n\nCurrent session: 33% used · resets Nov 12 at 12:40pm (Europe/Berlin)\nCurrent week (all models): 39% used · resets Nov 15 at 6am (Europe/Berlin)\nCurrent week (Fable): 59% used · resets Nov 15 at 6am (Europe/Berlin)\n\nWhat's contributing to your usage?\n..."
}
```

- **Coste: cero.** `local_command: "usage"`, `num_turns: 0`, todos los contadores de tokens a 0. La
  documentación lo confirma: `/usage` es de los comandos que Claude Code ejecuta sin pasar por el
  modelo ([commands](https://code.claude.com/docs/en/commands), fila `/usage`), y el informe de uso
  del plan viene del servidor de cuentas, no de una inferencia
  ([costs › Using the /usage command](https://code.claude.com/docs/en/costs#using-the-usage-command)).
- Lo único que sale del equipo es una petición HTTP al endpoint de uso de la cuenta. Ese endpoint
  **tiene límite de frecuencia propio**: según la documentación, cuando falla "most often because
  the usage endpoint is rate limited", `/usage` muestra los últimos datos de la última hora con la
  nota `Showing last-known usage`, o un mensaje `rate limited until 3:14am`
  ([costs › When the usage request fails](https://code.claude.com/docs/en/costs#when-the-usage-request-fails)).

### Fiabilidad y estabilidad del método actual

| Aspecto | Valoración |
| --- | --- |
| Exactitud de los porcentajes | Alta: son los del servidor, los mismos que muestran Claude Code y claude.ai. Incluyen el uso desde claude.ai y desde otros equipos, porque los límites son compartidos ([soporte: Pro/Max](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)). |
| Coste | Ninguno en cuota. Arranca un proceso de Node (~1 s). |
| Formato del texto | **Frágil.** Es texto para personas en inglés. El reinicio viene en formatos distintos (`7:09pm`, `Nov 15 at 6am`), sin año y con la zona entre paréntesis, que la regex descarta. Sirve para mostrarlo, no para calcular "faltan 40 minutos". Cualquier cambio de redacción (`Current week (all models)`, `% used`) rompe el análisis. El respaldo actual muestra el primer párrafo, así que el fallo no es grave, pero el monitor se quedaría ciego. |
| Datos que faltan | Si la ventana de 5 h no ha empezado (nada usado desde el último reinicio), la fila puede no tener reinicio o no aparecer. La regex acepta `resets` opcional. |
| Cuenta con API key | No hay límites del plan. `summarize()` muestra el bloque de coste, y es correcto. |

## 2. Fuentes de datos disponibles

### 2.1 `usage_report` estructurado (oficial, recomendada)

La referencia del Agent SDK documenta que cuando se envía `/usage` como prompt, el mensaje
`assistant` que lo entrega lleva `usage_report`, una copia estructurada del informe de tipo
`SDKUsageReport` ([TypeScript SDK › SDKAssistantMessage](https://code.claude.com/docs/en/agent-sdk/typescript#sdkassistantmessage),
[› SDKUsageReport](https://code.claude.com/docs/en/agent-sdk/typescript#sdkusagereport)). La CLI lo
emite igual con `--output-format stream-json --verbose`, porque el SDK no es más que esa CLI.
Comprobado aquí:

```
echo /usage | claude --print --output-format stream-json --verbose --no-session-persistence \
                     --setting-sources project --strict-mcp-config
```

Eventos: `system/init`, luego `assistant` con `usage_report`, luego `result` con coste 0, en 1,1 s.
El contenido:

```json
"rate_limits": {
  "limits": [
    {"kind": "session",       "group": "session", "percent": 33, "resets_at": "2026-11-12T11:40:00.124709+00:00", "scope": null, "severity": "normal", "is_active": false},
    {"kind": "weekly_all",    "group": "weekly",  "percent": 39, "resets_at": "2026-11-15T05:00:00.124730+00:00", "scope": null, "severity": "normal", "is_active": false},
    {"kind": "weekly_scoped", "group": "weekly",  "percent": 59, "resets_at": "2026-11-15T05:00:00.124992+00:00",
     "scope": {"model": {"display_name": "Fable"}, "surface": null}, "severity": "normal", "is_active": true}
  ],
  "extra_usage": {"is_enabled": false, "monthly_limit": null, "used_credits": 0, "utilization": null, "currency": "EUR"}
}
```

Lo que dice la documentación y conviene respetar:

- Hay que clasificar por `kind` ("Classify a row on this, never on a label"). Los valores
  conocidos son `session`, `weekly_all` y `weekly_scoped`, pero el servidor decide qué filas manda.
  Hay que aceptar `kind` desconocidos y tratarlos como un medidor genérico.
- `severity` (`normal`, `warning`, `critical`) es la lectura del propio servidor: es el mejor
  disparador de avisos porque sigue sus umbrales aunque cambien.
- `is_active` marca la fila que el servidor elegiría para un indicador único: suele ser la más
  restrictiva.
- `rate_limits` es `null` si Claude Code no consiguió el uso del plan, por ejemplo si el token no
  tiene el scope `user:profile`. Un token de `claude setup-token` pasado como
  `CLAUDE_CODE_OAUTH_TOKEN` **no** lo trae, porque solo tiene `user:inference`. Alfred usa el
  login normal de Claude Code, así que sí aplica.
- **El tipo está marcado como experimental: "its shape may change".** Requiere Agent SDK v0.3.273
  o posterior, así que conviene mantener el análisis del texto como respaldo.
- `--setting-sources project` con un `cwd` sin `.claude/` evita los hooks de usuario (en el modo
  por defecto se vio un `SessionStart` del usuario ejecutándose en cada consulta) y
  `--strict-mcp-config` evita conectar servidores MCP. No se puede usar `--bare`: en bare mode
  Claude Code no lee las credenciales OAuth ([headless › bare mode](https://code.claude.com/docs/en/headless#start-faster-with-bare-mode)).

### 2.2 `rate_limit_event` en el stream de cada turno (oficial, pasiva)

En `stream-json`, Claude Code emite `rate_limit_event` durante los turnos normales
([TypeScript › SDKRateLimitEvent](https://code.claude.com/docs/en/agent-sdk/typescript#sdkratelimitevent),
[Python › RateLimitInfo](https://code.claude.com/docs/en/agent-sdk/python#ratelimitinfo)). Hubo que
comprobarlo con una llamada mínima a Haiku: 4 tokens de salida y 0,002 USD estimados, la única
prueba de esta investigación que gastó cuota.

```json
{"type": "rate_limit_event",
 "rate_limit_info": {
   "status": "allowed", "resetsAt": 1794483600, "rateLimitType": "five_hour",
   "overageStatus": "rejected", "overageDisabledReason": "out_of_credits", "isUsingOverage": false,
   "unifiedWindows": {
     "five_hour": {"utilization": 0.39, "resetsAt": 1794483600, "status": "allowed"},
     "seven_day": {"utilization": 0.39, "resetsAt": 1794718800, "status": "allowed"}}}}
```

- Lo documentado es `status` (`allowed` / `allowed_warning` / `rejected`), `resetsAt`,
  `rateLimitType` y `utilization` (fracción de 0 a 1). **`unifiedWindows` aparece en la CLI pero no
  en los tipos documentados.** El SDK de Python lo dejaría en `raw`. Hay que tratarlo como un
  extra opcional.
- No incluye los semanales por modelo (`weekly_scoped`, p. ej. Fable). Para esos solo sirve
  `/usage`.
- Solo llega cuando hay tráfico al modelo. Sirve para reaccionar al instante mientras Alfred
  trabaja (`allowed_warning`, `rejected`), no para saber qué pasa cuando está parado.
- Hoy `LiveSession._handle()` en `alfred/backends/live.py` y `_run_once()` en
  `alfred/backends/claude.py` ignoran cualquier `type` que no reconocen, así que el evento llega y
  se descarta.

### 2.3 Campos `rate_limits` de la status line (oficial, solo interactivo)

El JSON que recibe un script de status line trae `rate_limits.five_hour` y
`rate_limits.seven_day` con `used_percentage` (0-100) y `resets_at` en epoch
([statusline › Available data](https://code.claude.com/docs/en/statusline#available-data),
[› Rate limit usage](https://code.claude.com/docs/en/statusline#rate-limit-usage)). Solo aparece
para suscriptores Pro/Max/Ultra, después de la primera respuesta, y cada ventana puede faltar.
**No sirve para Alfred:** la status line no se ejecuta en modo `-p` y engancharla obligaría a tocar
el `~/.claude/settings.json` del usuario. Solo sería útil como fuente extra si el usuario ya tiene
una status line que escribe esos datos en un archivo.

### 2.4 Mensajes de aviso y de límite (oficial)

Claude Code ya avisa con `You've used 85% of your session limit · resets 3:45pm` y bloquea con
`You've hit your session limit · resets 3:45pm` / `weekly` / `Opus` / `Sonnet`
([errors › Usage limits](https://code.claude.com/docs/en/errors#usage-limits)). En sesiones
interactivas puede esperar al reinicio y continuar solo (`autoContinueAtUsageLimit`,
[interactive-mode › Wait for a usage limit to reset](https://code.claude.com/docs/en/interactive-mode#wait-for-a-usage-limit-to-reset)).
Para Alfred lo útil es el equivalente estructurado: `status: "allowed_warning"` y `"rejected"` del
punto 2.2. Leer ese texto es la peor opción.

### 2.5 Logs locales de sesión (`~/.claude/projects/**/*.jsonl`)

Cada mensaje `assistant` guarda `message.usage` (tokens de entrada, salida y caché) y el modelo.
Cuando `/usage` se ejecuta en una sesión que se guarda en disco, el registro `system/local_command`
también incluye `usageReport` con el mismo contenido que en 2.1. Limitaciones:

- El formato de los transcripts no es una interfaz documentada y cambia entre versiones.
- Solo cubre **este equipo**. El uso desde claude.ai, el móvil u otras máquinas cuenta para el
  mismo límite y no aparece aquí. La propia documentación dice que el desglose de `/usage` es
  "approximate and computed from local session history on this machine"
  ([costs › Plan usage breakdown](https://code.claude.com/docs/en/costs#plan-usage-breakdown)).
- Los tokens no se traducen en porcentaje del plan: Anthropic no publica cuánto vale cada ventana
  en tokens, y el peso varía con el modelo y la caché.
- Se borran tras `cleanupPeriodDays`, 30 días por defecto.

Sirve para saber **qué** consumió la cuota (qué tarea, qué chat), no **cuánta queda**.

### 2.6 OpenTelemetry, Analytics API y Console

OpenTelemetry exporta tokens, coste estimado y actividad
([monitoring-usage](https://code.claude.com/docs/en/monitoring-usage)), pero no el porcentaje de
los límites del plan. La Claude Code Analytics API y la Enterprise Analytics API son para
organizaciones con Console o Enterprise ([costs › Manage costs for your organization](https://code.claude.com/docs/en/costs#manage-costs-for-your-organization)).
En un plan individual Pro/Max/Ultra no aportan nada a este caso.

### 2.7 Endpoint no documentado `GET https://api.anthropic.com/api/oauth/usage`

Muchas herramientas de la comunidad lo llaman con el token OAuth de Claude Code y la cabecera
`anthropic-beta: oauth-2025-04-20`, y obtienen `five_hour` y `seven_day` con `utilization` y
`resets_at` (p. ej. los crates [cship](https://docs.rs/crate/cship/1.7.0/source/src/usage_limits.rs)
y [clauth](https://docs.rs/crate/clauth/0.5.5/source/src/usage/fetch.rs)). **Se descarta:**

- No está documentado. Hay versiones con otras rutas y planes que no devuelven datos (issue
  [anthropics/claude-code#34348](https://github.com/anthropics/claude-code/issues/34348)).
- Hay que sacar el token del llavero o de `~/.claude` y usarlo fuera de Claude Code. La página
  legal dice que la autenticación OAuth sirve para "ordinary use of Claude Code and other native
  Anthropic applications" y que no se deben "collect, store, or intermediate Claude.ai credentials
  or session tokens" ([legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance)).
- No aporta nada que `usage_report` no dé ya por un camino soportado.

### 2.8 Herramientas de la comunidad

| Herramienta | Cómo obtiene los datos | Limitación para este caso |
| --- | --- | --- |
| [ccusage](https://github.com/ryoppippi/ccusage) (`ccusage blocks --live`) | Lee los JSONL locales y agrupa en "bloques" de 5 h que empiezan con el primer mensaje | Estima tokens y coste, no el % del plan real. Solo cubre este equipo. Los bloques son una heurística. No ve los semanales por modelo. |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) | JSONL locales más límites de tokens por plan supuestos, con predicción del agotamiento | Mismos problemas, más unos topes que no publica Anthropic y que se quedan viejos con cada cambio de límites. |
| cship, clauth, claude-usage y otros | Endpoint no documentado de 2.7 | Ver 2.7. |

La idea útil de estas herramientas es la **proyección por ritmo de consumo** ("a este ritmo llegas
al límite a las 15:10"). Alfred puede calcularla con los porcentajes reales de varias consultas
seguidas, sin contar tokens.

### 2.9 Comparativa

| Fuente | Oficial | Coste en cuota | 5 h | Semanal | Semanal por modelo | Cuándo hay datos | Estabilidad |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `usage_report` (2.1) | Sí, "experimental" | 0 | Sí | Sí | Sí | Cuando se consulta | Media-alta |
| Texto de `/usage` (hoy) | Sí, texto para personas | 0 | Sí | Sí | Sí | Cuando se consulta | Baja |
| `rate_limit_event` (2.2) | Sí (más `unifiedWindows` sin documentar) | 0 extra | Sí | Sí | No | Solo con tráfico | Media-alta |
| Status line (2.3) | Sí | 0 | Sí | Sí | No | Solo en interactivo | Alta, pero no aplica |
| JSONL locales (2.5) | No es una interfaz | 0 | Estimado | Estimado | No | Siempre | Baja |
| OTel (2.6) | Sí | 0 | No | No | No | Siempre | Alta, pero no aplica |
| `/api/oauth/usage` (2.7) | No | 0 | Sí | Sí | A veces | Cuando se consulta | Baja; riesgo con los términos |

## 3. Cómo se comportan los límites (lo que el monitor debe asumir)

- Pro y Max tienen un límite de sesión de 5 horas y otro semanal. Max y Ultra tienen además un
  semanal aparte para Fable. Las cuentas Ultra verificadas no tienen límite de 5 h. Claude
  (claude.ai) y Claude Code comparten los mismos límites
  ([soporte: Pro/Max](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan)).
  **El monitor tiene que funcionar aunque falte la fila `session`.**
- El uso cuenta para la ventana de sesión y la semanal **a la vez**. Una ráfaga fuerte puede agotar
  la semanal antes de que se reinicie la de 5 h ([errors › session limit](https://code.claude.com/docs/en/errors#youve-hit-your-session-limit)).
- La ventana de 5 h es móvil: empieza con el primer uso y se reinicia 5 h después. Lo confirman los
  datos: el reinicio de sesión cae a una hora suelta (11:40 UTC). La semanal se reinicia en un
  momento fijo de la cuenta (aquí, el sábado a las 05:00 UTC).
- Los límites de Opus y Sonnet solo bloquean esa familia de modelos. Cambiar de modelo permite
  seguir trabajando. Los de sesión y semanal-todos no se salvan cambiando de modelo.

**Consecuencia para "aprovechar la cuota sobrante":** lo que de verdad se pierde es el **semanal**
sin usar al llegar su reinicio. La ventana de 5 h es un limitador de ritmo: el margen que deja sin
usar solo es aprovechable si el semanal también tiene margen. Por eso la regla de cuota sobrante de
5 h tiene que mirar también el ritmo semanal.

## 4. Encaje con el planificador de Alfred

### Por qué no `alfred schedule`

`SchedulerStore` tiene tres tipos de entrada:

- `message`: texto fijo, no puede calcular nada.
- `task`: `TelegramBot._deliver` lo convierte en `agent.submit(...)`, es decir, un turno del modelo
  con todo el contexto de la conversación. Ese coste es justo lo que la documentación señala como
  causa de consumo en sesiones largas ([costs › Why usage climbs](https://code.claude.com/docs/en/costs#why-usage-climbs-in-a-long-session)).
  Una comprobación cada 15 minutos como tarea serían 96 turnos al día.
- `event`: igual que `task`.

Por tanto **el monitor no puede ser una tarea programada del agente**. Tiene que ser código del bot.

### Dónde va

`BotPool.start` (desde el merge de `refactor`; antes era `TelegramBot._post_init`) ya arranca
`run_scheduler(...)` como tarea de asyncio. El monitor se arranca al lado (`run_usage_monitor(...)`)
y se cancela en `BotPool.stop`, una vez por proceso y no una por bot (ver la sección 9). Cumple la regla 3 de
`docs/EXTENSIONS.md`: es código base, no de una extensión, y si falla no tumba el bot porque cada
iteración captura sus excepciones, igual que `deliver_one`.

```
             ┌──────────── proceso del bot (alfred run) ─────────────┐
 Telegram ◄──┤ TelegramBot._send ◄── UsageMonitor.evaluate() ◄─ snapshot
             │                               ▲                       │
             │     claude -p "/usage" (0 tokens, cada N min) ────────┤
             │     rate_limit_event de los turnos de LiveSession ────┘
             │                                                       │
             │ run_scheduler ── _deliver(task) ── consulta el tope ──┤ (pausa tareas)
             └───────────────────────────────────────────────────────┘
```

### Frecuencia de consulta

El endpoint de uso tiene límite de frecuencia (no se publica el valor) y Claude Code guarda los
últimos datos 60 minutos. Propuesta adaptativa:

| Situación | Intervalo |
| --- | --- |
| Por defecto | 15 min |
| Algún medidor ≥ 70 %, o un reinicio en menos de 1 h | 5 min (mínimo absoluto) |
| Horas de silencio y todo < 50 % | 60 min |
| Hay un `rate_limit_event` de menos de 5 min | No consultar 5 h ni 7 días: usar el evento (sí consultar si hace falta el semanal por modelo) |
| `/usage` respondió `rate limited until HH:MM` | No consultar antes de esa hora |
| Error o `rate_limits: null` | Reintento con espera exponencial: 5, 10, 20 y hasta 60 min |

A 15 minutos son unos 96 procesos de ~1 s al día, 0 tokens y 96 peticiones HTTP ligeras.

## 5. Umbrales y avisos propuestos

Todos los avisos son mensajes fijos enviados por el panel de control (antes, `TelegramBot._send`), sin pasar por el modelo. Cada
uno tiene una clave de deduplicación `(regla, kind, scope, resets_at, umbral)` guardada en disco,
para que no se repita ni tras reiniciar el bot.

| # | Regla | Disparo (por defecto) | Ejemplo de mensaje |
| --- | --- | --- | --- |
| R1 | Umbral | `percent` cruza 80 y 95, **o** `severity` pasa a `warning` o `critical`, o llega `status: allowed_warning` | "⚠️ Sesión al 82 %. Se reinicia a las 12:40 (en 1 h 10 min)." |
| R2 | Proyección | Con ≥ 2 lecturas de la misma ventana separadas ≥ 20 min: si al ritmo de los últimos 60 min se llega a 100 % antes del reinicio y en menos de 90 min | "A este ritmo llegas al límite semanal de Fable hacia las 15:10, 14 h antes del reinicio." |
| R3 | Límite alcanzado | `percent ≥ 100`, o `status: rejected` | "⛔ Límite de sesión agotado hasta las 12:40. Pauso las tareas programadas hasta entonces." |
| R4 | Reinicio | Ha pasado `resets_at` de un medidor que llegó a R1 crítico o R3 | "✅ Tu límite de sesión se ha reiniciado." (y se reanudan las tareas pausadas) |
| R5 | Sobrante de 5 h | 45 min antes del reinicio de `session`: `percent ≤ 50` **y** el semanal tiene margen (ver abajo) | "Quedan 45 min de esta ventana y has usado el 30 %. ¿Aprovecho para…? 1) … 2) …" |
| R6 | Sobrante semanal | 24 h y 6 h antes del reinicio semanal: queda ≥ 30 % (a 24 h) o ≥ 15 % (a 6 h), en `weekly_all` o en un `weekly_scoped` | "Tu semana se reinicia el sábado a las 06:00 y queda el 45 % (Fable: 40 %). Tareas pendientes que caben: …" |

**Margen semanal ("ritmo"):** `transcurrido = 1 - (resets_at - ahora) / 7 días`. Hay margen si
`percent ≤ transcurrido × 100 - 10`. Ejemplo: a mitad de semana (50 %) con un 39 % usado hay
margen; con un 59 % no.

**Horas de silencio** (`ALFRED_QUIET_HOURS`, por defecto 23-08 en hora local): se descartan R2,
R5 y R6; R1, R3 y R4 se acumulan y se envían al terminar el silencio, salvo los críticos. R6 a 6 h
se adelanta si cae dentro del silencio.

**Volumen:** como mucho 1 aviso por regla y ventana. R5 y R6 juntas, como mucho 1 propuesta cada
4 h.

### Proponer tareas pendientes

Hoy Alfred no tiene una lista de pendientes (memoria y skills sí, backlog no). Propuesta:

- Una tabla `backlog` en el mismo `scheduler.sqlite3` (`id, chat_id, text, size: s|m|l, model,
  created_at, done_at`) y una orden para el agente `alfred backlog add|list|done`, siguiendo el
  patrón de `alfred schedule`, con una skill base que explique cuándo usarla ("cuando sobre cuota,
  haz X"; "apúntalo para cuando haya margen").
- El monitor elige los elementos cuyo tamaño cabe en el margen (s ≤ 10 %, m ≤ 25 %, l ≤ 50 %;
  heurística ajustable) sin llamar al modelo, y los lista en el aviso.
- El usuario responde con `/spare 2` (comando nuevo en `COMMANDS`), que encola ese elemento como
  `KIND_TASK` para ese chat. Solo entonces se gasta cuota, y en algo que el usuario ha elegido.
- Más adelante: botones inline de Telegram en lugar de `/spare N` (encaja con "Inline approvals" de
  la fase 2 de `docs/PLAN.md`).
- `ALFRED_USAGE_AUTORUN` (por defecto desactivado) ejecutaría el primer elemento sin preguntar.
  No se recomienda activarlo por defecto (ver riesgos).

### Tope de gasto (fase 2 de `docs/PLAN.md`)

Con la misma instantánea, en `_deliver` antes de despertar al agente con un `KIND_TASK`: si algún
medidor que afecta al modelo de Alfred está a ≥ `ALFRED_USAGE_CAP` (95 %), o el último
`rate_limit_event` dice `rejected`, la tarea se aplaza a `resets_at + 2 min` y se avisa una vez. Hace
falta un `SchedulerStore.defer(item_id, due)` que cambie `due_at_utc` sin contar un intento. Los
recordatorios fijos (`KIND_MESSAGE`) no se aplazan nunca.

## 6. Riesgos

| Riesgo | Mitigación |
| --- | --- |
| **`usage_report` cambia** (el tipo está marcado experimental) | Análisis tolerante: campos opcionales, `kind` desconocido = medidor genérico. Respaldo con la regex actual: sin `resets_at` exacto no se ejecutan R2, R5 ni R6, pero R1 y R3 siguen funcionando. Un test con una fixture real por versión de Claude Code. Avisar una vez si se pierde el formato estructurado ("no puedo leer el uso; ¿actualizaste Claude Code?"). |
| **Los límites cambian sin aviso** (nuevos medidores, desaparece la ventana de 5 h, como en Ultra) | No guardar topes en tokens. Trabajar solo con `percent`, `resets_at` y `severity` del servidor. Recorrer `limits` en lugar de esperar tres filas fijas. |
| **El monitor gasta cuota** | Nunca usar `KIND_TASK` para monitorizar. `/usage` cuesta 0 tokens (verificado). Test que falle si el monitor arranca algo que no sea `/usage`. Registrar `total_cost_usd` de cada consulta y desactivar el monitor si alguna vez es > 0. |
| **Límite de frecuencia del endpoint de uso** | Intervalo adaptativo, respetar `rate limited until`, reutilizar `rate_limit_event`, mínimo de 5 min. |
| **Hooks o MCP del usuario en cada consulta** | `--setting-sources project`, `--strict-mcp-config` y `cwd` en `state/usage-probe/` sin `.claude/`. |
| **Avisos excesivos** | Deduplicación persistente, horas de silencio, máximo de propuestas, todo configurable y con un interruptor general. |
| **Términos de servicio** | Alfred usa el binario oficial de Claude Code con el login del propio usuario, y la página legal no lo impide ("Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their own Claude subscription"). Pero: (a) los términos de consumidor prohíben el acceso "through automated or non-human means, whether through a bot, script, or otherwise" salvo con API key o permiso explícito ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms)), y (b) "Advertised usage limits for Pro, Max, and Ultra plans assume ordinary, individual usage" ([legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance)). Consultar `/usage` es leer el estado de la propia cuenta, sin inferencia. **Llenar la cuota de forma automática y continua** se acerca a lo que esos textos quieren evitar. Por eso el diseño **propone** y el usuario decide. No se usa el endpoint no documentado ni se toca el token. Esto no es asesoramiento legal: si se activa `AUTORUN`, conviene revisar los términos vigentes. |
| **Datos de otros dispositivos** | No es un problema: `usage_report` viene del servidor e incluye el uso de claude.ai y de otros equipos. Pero R2 puede dispararse por uso que no es de Alfred, y el mensaje no debe culpar a Alfred. |
| **Zonas horarias** | `resets_at` es UTC. Mostrarlo en la hora local del equipo, la misma que usa `alfred schedule`. |

## 7. Diseño propuesto

### Módulos

**`alfred/usage.py`** (ampliar, sin romper `/usage`):

```python
@dataclass
class Limit:
    kind: str                 # "session", "weekly_all", "weekly_scoped", u otro
    label: str                # "Session", "Week (all models)", "Week (Fable)"
    percent: float
    resets_at: Optional[datetime]   # aware UTC
    severity: str = "normal"  # normal | warning | critical | unknown
    scope: Optional[str] = None     # "Fable", ...
    is_active: bool = False

@dataclass
class Snapshot:
    taken_at: datetime
    source: str               # "usage_report" | "text" | "stream"
    limits: List[Limit]
    extra_usage: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_after: Optional[datetime] = None   # de "rate limited until"

async def probe(config, environ) -> Snapshot      # stream-json + usage_report, con el texto como respaldo
def from_rate_limit_event(event, now) -> Snapshot # solo five_hour / seven_day
def summarize(snapshot) -> str                    # lo que hoy hace summarize(report)
```

- `probe()` usa `run_json_lines` con
  `[claude_bin, "--print", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
  "--setting-sources", "project", "--strict-mcp-config"]`, `stdin_text="/usage"` y
  `cwd=config.state_dir / "usage-probe"`. Del evento `assistant` toma `usage_report`; si no está,
  aplica `LIMIT_LINE` al texto de `message.content` o de `result`.
- `fetch()` queda como `summarize(await probe(...))`, y el comando `/usage` sigue igual. Puede añadir
  "hace 3 min" si usa una instantánea guardada.

**`alfred/usage_monitor.py`** (nuevo):

```python
class UsageStore:            # state/usage.json: última instantánea, historial corto, claves de avisos enviados
class Alert(NamedTuple): key: str; text: str; urgent: bool; offer_backlog: bool

def evaluate(now, snapshot, history, sent_keys, settings) -> List[Alert]   # función pura, sin E/S
def next_interval(now, snapshot, settings) -> float

async def run_usage_monitor(config, store, send, *, probe=usage.probe, clock=utc_now): ...
```

`evaluate` contiene las reglas R1 a R6 y es la parte que se prueba a fondo con horas fijas. El
bucle solo consulta, guarda, evalúa, envía y duerme.

**`alfred/backends/live.py` y `claude.py`:** en `_handle` / `on_event`, si
`kind == "rate_limit_event"`, llamar a un nuevo `Listener.rate_limit(info)` (no hace nada por
defecto). `TelegramBot` lo implementa guardando `from_rate_limit_event(...)` en `UsageStore`, y si
`status != "allowed"` despierta al monitor (`asyncio.Event`) para que evalúe ya.

**`alfred/telegram_bot.py`:** arranca y para el monitor en `BotPool.start` / `BotPool.stop` (antes, `_post_init` / `_post_shutdown`), añade
el comando `/spare N` y consulta el tope en `_deliver` (sección 5).

**`alfred/scheduler.py`:** `defer(item_id, due)`. Opcionalmente, la tabla `backlog`.

**`alfred/cli.py`:** `alfred backlog add|list|done` (JSON, como `schedule`) y
`alfred usage --json` con la instantánea, útil para depurar y para que el agente la consulte gratis
desde una skill.

**`alfred/config.py` y `.env.example`:**

```env
# Avisos de uso del plan de Claude. Default: 0 (desactivado) en la primera versión
# ALFRED_USAGE_MONITOR=1
# Chat que recibe los avisos. Default: el primer id numérico de ALFRED_ALLOWED_USERS
# ALFRED_USAGE_CHAT_ID=
# Cada cuánto consultar /usage (mínimo 5m). Default: 15m
# ALFRED_USAGE_POLL=15m
# Porcentajes que disparan aviso. Default: 80,95
# ALFRED_USAGE_WARN=80,95
# Proponer tareas cuando sobre cuota antes del reinicio. Default: 1
# ALFRED_USAGE_SPARE=1
# Pausar tareas programadas al llegar a este porcentaje. Vacío = nunca. Default: 95
# ALFRED_USAGE_CAP=95
# Sin avisos no urgentes en este rango de horas locales. Default: 23-8
# ALFRED_QUIET_HOURS=23-8
# Ejecutar sin preguntar la primera tarea pendiente cuando sobre cuota. Default: 0
# ALFRED_USAGE_AUTORUN=0
```

### Pasos de implementación

Cada paso se puede integrar por separado y deja las pruebas en verde.

1. **Instantánea estructurada.** `Limit`, `Snapshot`, `probe()` con `usage_report` y el texto como
   respaldo, y `summarize(snapshot)`. Actualizar la salida simulada de `claude` en los tests para
   emitir `stream-json` con `usage_report`, con una fixture copiada de la salida real de la
   sección 2.1. Tests: `usage_report` válido, sin `usage_report` (respaldo), `rate_limits: null`,
   `kind` desconocido, fila `session` ausente y "rate limited until". `/usage` en Telegram debe
   mostrar lo mismo que hoy.
2. **Captura pasiva.** `Listener.rate_limit`, el manejo de `rate_limit_event` en `live.py` y
   `claude.py`, y `from_rate_limit_event`. Tests con un evento como el de la sección 2.2, con y sin
   `unifiedWindows`.
3. **`UsageStore` y `alfred usage --json`.** Persistencia en `state/usage.json` (escritura atómica)
   con historial de las últimas 24 h, para que la proyección sobreviva a reinicios.
4. **`evaluate()` con R1, R3 y R4**, la deduplicación y las horas de silencio. Tests de tabla con
   relojes fijos.
5. **Bucle `run_usage_monitor`** en `_post_init`, con intervalo adaptativo y espera exponencial,
   detrás de `ALFRED_USAGE_MONITOR`. Test con un `probe` falso que verifique que nunca llama a
   `agent.submit`.
6. **R2 (proyección)**, R5 y R6 (cuota sobrante) con un mensaje simple, todavía sin backlog.
7. **Tope en `_deliver`** con `SchedulerStore.defer`, reanudando en R4. Cierra el "Spend cap" de
   `docs/PLAN.md`.
8. **Backlog**: tabla, `alfred backlog`, skill base `alfred:backlog`, `/spare N` y la selección por
   tamaño en R5 y R6.
9. **Documentación**: tabla de comandos del README (`/spare`), `.env.example`, `docs/PLAN.md` (marcar
   el tope de gasto y añadir el monitor) y una nota en `/help`.
10. **Prueba real**: dejar `alfred run` un día con `ALFRED_USAGE_POLL=5m`, comprobar que
    `total_cost_usd` de cada consulta es 0, que no aparece `rate limited until`, y contrastar los
    avisos con `/usage` en una sesión interactiva.

## 8. Preguntas abiertas

- Con qué frecuencia exacta empieza el endpoint de uso a limitar. No está publicado. El paso 10 lo
  acotará; si aparece `rate limited until`, subir el mínimo a 10 min.
- Qué devuelve `usage_report` cuando la ventana de 5 h no ha empezado (`resets_at: null`, la fila
  ausente o `percent: 0`). No se pudo observar porque la ventana estaba activa. El código debe
  aceptar los tres casos.
- Si `rate_limit_event` llega en cada petición o solo cuando cambia algo. Para el diseño da igual,
  porque se guarda el último.
- Si Anthropic documenta `unifiedWindows` o añade los semanales por modelo al evento: entonces la
  consulta activa podría espaciarse mucho más.

## 9. Estado de la implementación

### Lo que hay

| Pieza | Qué hace hoy |
| --- | --- |
| `Window`, `Reading` (`usage_monitor.py`) | Modelo de una lectura: tipo de medidor (`session`, `weekly_all`, `weekly_scoped` u otro), etiqueta, porcentaje, `resets_at` (UTC), `reset_text`, `severity` y `scope`. `Reading.from_usage_report()` lee el `usage_report` estructurado (sección 2.1) y `Reading.from_report()`, el texto, como respaldo y sin hora de reinicio exacta. |
| `ClaudeUsageSource` | Pide `/usage` con `stream-json --verbose --no-session-persistence --setting-sources project --strict-mcp-config` desde `state/usage-probe/`, con `run_json_lines`, `INHERITED_SESSION_VARS` y `USAGE_TIMEOUT` de `usage.py`. Toma `usage_report` y, si no llega, analiza el texto. Un "rate limited" sin datos es una lectura fallida. Si alguna vez `total_cost_usd` no es 0, lo registra como aviso. `/usage` y `alfred usage` siguen usando `usage.fetch()` sin cambios. |
| `Rule` y las seis reglas | Declaradas, sin lógica (`evaluate()` devuelve `[]`). `SpareWeeklyRule.estimate()` ya calcula, con `window_share_percent`, cuántas ventanas de 5 h haría falta para gastar el semanal sobrante y cuántas caben antes del reinicio. La regla aún no actúa. |
| `UsageHistory` | `state/usage.json`: cada lectura del monitor, con poda a 24 h y escritura atómica. |
| `MonitorSettings` | Interruptor y ajustes. Tienen efecto `ALFRED_USAGE_MONITOR`, `ALFRED_USAGE_POLL` (15 min) y `ALFRED_USAGE_WAKE_INTERVAL` (1 h); `ALFRED_USAGE_WINDOW_SHARE` (8) solo lo usa la estimación. Los demás siguen reservados. |
| `UsageMonitor`, `start()` | `BotPool.start` lo arranca una vez por proceso y `BotPool.stop` lo para. Su bucle revisa los avisos de los agentes cada `poll_seconds` con una sola lectura (sección 12). |
| `usage_alerts.py` | Condiciones, almacén por bot, decisión de qué se dispara y texto del evento (sección 12). |
| Permiso `usage` (`bots.py`, `menus.py`, `telegram_bot.py`, `cli.py`) | Sección 11. |
| `alfred usage-alert add\|list\|remove` (`cli.py`) | Sección 12. |

Garantías comprobadas por los tests (`tests/test_usage_monitor.py`, `test_usage_permission.py`,
`test_usage_alerts.py`; Telegram y Claude Code simulados):

- **Con el interruptor apagado (por defecto)** no se ejecuta nada: `start()` lee solo el
  interruptor y devuelve `None`. No hay bucle, ni lecturas, ni despertares. El permiso `usage` no
  tiene efecto y el panel lo advierte.
- **Encendido**, solo se lee el uso cuando algún bot activo con el permiso tiene un aviso
  pendiente, y entonces una vez por ciclo para todos. Al usuario no se le manda nada todavía.
- Un aviso despierta solo al bot que lo registró, se dispara una sola vez (o una por ventana con
  `--repeat`) aunque se reinicie el proceso, y un bot no se despierta más de una vez por intervalo.

### Bots creados (tras el merge de `refactor` / agent-chats)

Desde ese merge, un proceso (`alfred run` → `BotPool`) sirve el panel de control y todos los bots que
Alfred ha creado. Cada bot tiene su `TelegramBot`, su `Agent` y su `Config` (`config_for(bot_id)`),
pero todos usan **la misma cuenta de Claude**: el mismo `claude` con el mismo login. Los límites del
plan son de esa cuenta, así que:

- **El monitor corre una sola vez por proceso, en `BotPool.start`, con la configuración de Alfred.**
  No va en `TelegramBot`: un monitor por bot repetiría las consultas al endpoint de uso, que tiene
  límite de frecuencia, y duplicaría los avisos. `TelegramBot._post_init` ya no es la ruta de
  arranque (el pool llama a `started()`).
- `start()` devuelve `None` con la configuración de un bot creado (`config.bot_id`), aunque sus
  ajustes propios (`overrides`) activen el interruptor.
- Una sola lectura por ciclo sirve para los avisos de todos los bots (sección 12).
- El historial va en `state/usage.json` de Alfred. Los avisos de cada bot van en
  `state/bots/<id>/usage-alerts.json`.
- `/usage` sigue en cada bot como en `refactor`: todos muestran los mismos límites, los de la
  cuenta.

Pendiente por esta razón, cuando haya avisos para el usuario: deben salir del **panel**; el tope de
gasto va en `BotPool.deliver`, compartido por todos los bots; la captura pasiva de
`rate_limit_event` debe escribir en el historial del proceso; la proyección (R2) no debe atribuir el
consumo a un bot, y las propuestas de cuota sobrante deben decir a qué bot va cada tarea.

### Lo que falta

1. **Captura pasiva**: `rate_limit_event` de los turnos de los bots (sección 2.2) hacia el
   historial, para leer menos `/usage`.
2. **Avisos al usuario (R1-R6)** desde el panel, con deduplicación persistente y horas de silencio.
3. **Consulta adaptativa**: hoy el intervalo es fijo. Falta acortarlo cerca de los reinicios y
   respetar `rate limited until HH:MM`; ahora una lectura fallida solo se registra y no dispara
   nada.
4. **Regla de semanal sobrante (R6)** con `SpareWeeklyRule.estimate()`, y medir
   `window_share_percent` con el historial propio (sección 10).
5. **Tope de gasto** en `BotPool.deliver`, con `cap_percent` y `Postpone`.
6. **Backlog** y `/spare N`.
7. **Skill base para agentes** sobre `alfred usage-alert`. Hoy lo explican `--help` y una línea en
   el resumen de permisos del manual del bot.
8. **Más valores en las condiciones**: el semanal por modelo (`fable.used`) y si la ventana de 5 h
   está en marcha (`session.active`).
9. **Pruebas en vivo**: ver la lista al final de la sección 12.

## 10. Cuánto del semanal consume una ventana de 5 h llena

Pregunta: si se agota una ventana de 5 h, ¿qué porcentaje del límite semanal se ha gastado? Solo se
ha investigado en internet, sin pruebas propias ni gasto de cuota, como se pidió. Consultado el
2026-11-12.

### Lo oficial

| Fuente | Qué dice |
| --- | --- |
| [Precios](https://claude.com/pricing) | Pro: "More usage than Free, every 5 hours and every week". Max ($100): "5x Pro usage every 5 hours and every week", y Fable "up to half your weekly limit". Ultra ($200): "No 5-hour limit" (cuentas verificadas) y "10x Pro usage every week". Nota: "Usage limits vary with the model, chat length, and file size". |
| [¿Qué es el plan Max?](https://support.claude.com/en/articles/11049741-what-is-the-max-plan) | "five times the Pro plan's usage every five hours and every week". Límite semanal de todos los modelos más "a separate weekly limit for Fable". El semanal se reinicia "at a fixed time each week that is assigned to your account". |
| [Claude Code con Pro/Max](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan) | Pro y Max: límite de sesión de 5 h y semanal. Ultra: semanal; las cuentas verificadas no tienen límite de 5 h. |
| [Anuncio del 6 de mayo de 2026](https://www.anthropic.com/news/higher-limits-spacex) | "we're doubling Claude Code's five-hour rate limits for Pro, Max, Team, and seat-based Enterprise plans". No menciona el semanal. |
| Anuncio de julio de 2025 (recogido por [TechCrunch](https://techcrunch.com/2025/07/28/anthropic-unveils-new-rate-limits-to-curb-claude-code-power-users/)) | Semanales desde el 28 de agosto de 2025: Pro, "40 to 80 hours of Sonnet 4". Max $100: 140-280 h de Sonnet 4 y 15-35 h de Opus 4. Max $200: 240-480 h de Sonnet 4 y 24-40 h de Opus 4. Son "horas de uso" típicas, no horas de ventana llena, y son anteriores a la duplicación de mayo de 2026 y a los modelos actuales. |

**Anthropic no publica la proporción entre la ventana de 5 h y el semanal**, ni cantidades absolutas
(tokens, mensajes) para ninguno de los dos.

### Lo que se deduce de lo oficial

- **Pro y Max tienen la misma proporción.** Max es 5 veces Pro en la ventana y en el semanal, así
  que una ventana llena se lleva el mismo porcentaje del semanal en los dos planes.
- **Ultra verificado no tiene ventana de 5 h**, así que la pregunta no aplica. Para Ultra sin
  verificar no hay cifra oficial de la ventana.
- **El antiguo Max 20x**, hoy Ultra, era 20 veces Pro por sesión. Según las horas de 2025 su semanal
  era unas 6 veces el de Pro (240-480 h frente a 40-80 h de Sonnet 4). Por tanto, una ventana llena
  pesaba en su semanal unas 3 veces más que en Pro. Es una deducción a partir de cifras de 2025.
- **La duplicación del 6 de mayo de 2026** duplicó la ventana sin cambiar el semanal, según el
  anuncio. Eso **dobló el peso de una ventana llena** respecto a lo anterior. Fuentes no oficiales
  hablan de un +50 % temporal del semanal en Claude Code entre julio y septiembre de 2026 y de un
  +25 % permanente desde el 14 de septiembre
  ([cronología de explainx.ai](https://www.explainx.ai/blog/claude-usage-limits-2026-timeline-explained),
  sin enlace a Anthropic). Si fuera cierto, el peso actual sería unas 1,6 veces el anterior a mayo
  (2 / 1,25), no el doble.

### Mediciones de la comunidad

La única medición sistemática encontrada es la de [wokey.ai](https://wokey.ai/claude-usage-limits).
Es un tercero que mide cuentas reales a través de su pasarela y vuelve a medir cada lunes. Datos del
2 al 9 de noviembre de 2026:

| Plan | Ventanas de 5 h llenas por semana | Porcentaje del semanal por ventana | Muestra y dispersión |
| --- | --- | --- | --- |
| Pro | ≈ 13 | ≈ 7,7 % | 296 cuentas; la mitad central, ±4 % de la mediana |
| Max (5x) | sin publicar ("collecting") | — | 2 cuentas |
| Ultra (antes Max 20x), cuentas con ventana | ≈ 5,5 | ≈ 18 % | 21 cuentas; ±20 % |
| Team Standard / Premium | ≈ 9,3 / ≈ 9,5 | ≈ 11 % | 283 / 146 cuentas |

Método, según la página: cuando el semanal de una cuenta sube al menos 10 puntos, las peticiones de
ese tramo divididas por la subida dan una semana completa. Usa medianas por cuenta y luego entre
cuentas. Advierte que el proveedor puede hacer pruebas A/B de límites y que el uso fuera de la
pasarela distorsiona la medida. Otras guías citan la misma idea sin medir. Ninguna da cifras para
Max 5x.

### Por modelo

- La ventana y el semanal general miden el mismo consumo, así que el modelo cambia **cuánto
  trabajo cabe** en una ventana (Opus o Fable gastan más por petición que Sonnet o Haiku), pero no,
  en principio, la proporción entre las dos. Es una deducción: Anthropic no dice si pondera igual
  los dos medidores.
- **Fable** tiene en Max y Ultra un medidor semanal propio de "hasta la mitad" del semanal. Una
  ventana dedicada solo a Fable llenaría ese medidor unas dos veces más rápido que el general: del
  orden del **15-16 % del semanal de Fable** por ventana en Max. Es una deducción sin medición.
- Las cifras de modelos de 2025 (Sonnet 4 y Opus 4 por horas) no sirven para los modelos de 2026.

### Resultado y uso en Alfred

| Plan | Porcentaje del semanal por ventana de 5 h llena | Confianza |
| --- | --- | --- |
| Pro | **≈ 8 %** (7-9 %) | Media: una medición amplia de noviembre de 2026; no oficial |
| Max | **≈ 8 %** (6-10 %) | Baja-media: deducido de la proporción oficial 5x/5x, sin medir |
| Ultra verificado | No aplica (no hay ventana de 5 h) | Oficial |
| Ultra sin verificar / antiguo Max 20x | ≈ 18 % (14-22 %) | Baja: 21 cuentas |
| Semanal de Fable en Max, ventana solo con Fable | ≈ 15 % | Baja: deducción |

Incertidumbre: Anthropic cambia los límites sin publicar cifras (mayo y septiembre de 2026) y se
reserva limitar "at our discretion". La medición es de un solo tercero y casi toda de Pro. Si los
semanales subieron un 25 %, el dato ya lo incluye, porque se midió después.

**Parámetro**: `ALFRED_USAGE_WINDOW_SHARE`, por defecto `8` (`MonitorSettings.window_share_percent`),
con la fuente en el código y en `.env.example`. Lo usa `SpareWeeklyRule.estimate()`: con un 60 %
del semanal usado y 20 h hasta el reinicio, el 40 % sobrante pide 5 ventanas llenas y solo caben 4.
La regla aún no actúa sobre ese cálculo. Mejor que el valor por defecto sería medirlo en la propia
cuenta: con el historial de lecturas, la subida del semanal dividida por la de la ventana en un mismo
tramo da la proporción real (pendiente, punto 4 de la sección 9).

## 11. Permiso `usage` por bot

### Qué es

Un permiso más de cada bot, `usage`, **desactivado por defecto y en todos los preajustes**. Ninguno de
`minimal`, `standard` ni `broad` lo incluye, y aplicar un preajuste lo vuelve a apagar. Permite leer
el uso del plan (`alfred usage`) y registrar avisos (`alfred usage-alert`).

| Dónde | Cómo |
| --- | --- |
| `/bot perm <bot> usage on\|off` | Como cualquier interruptor |
| `/bot new ... +usage` | Opción al crear |
| `/bot` (menús) | Interruptor "📊 Plan usage and alerts" en el paso de permisos del asistente y en el editor de cada bot |
| Resumen de permisos | En el manual del bot ("You can/cannot read the Claude plan usage...") y en lo que ve el dueño, también en `/bot show` |

### Cómo se hace respetar

1. **Claude Code**: sin el permiso, los ajustes del bot deniegan `Bash(alfred usage)`,
   `Bash(alfred usage *)`, `Bash(alfred usage-alert)` y `Bash(alfred usage-alert *)`. Las reglas
   de denegación valen en todos los modos.
2. **La orden `alfred`**: `alfred usage` y `alfred usage-alert add|list|remove` rechazan a un bot sin
   el permiso. Es el segundo candado, como `require()` en el resto de órdenes.
3. **El monitor**: en cada ciclo solo revisa los bots activos que tienen el permiso. Si se lo
   quitan, sus avisos se conservan pero no se evalúan ni lo despiertan.
4. **Sandbox**: con `shell full` y sandbox, las dos órdenes salen del sandbox
   (`excludedCommands`) solo si el bot tiene el permiso, porque necesitan el estado de Alfred y el
   login de Claude Code.

No afecta a la orden `/usage` del chat de cada bot: esa la usa el dueño, no el agente, y se ha
mantenido igual. El operador en la terminal (`alfred usage` sin `ALFRED_BOT_ID`) tampoco cambia.

### Relación con el interruptor global (`ALFRED_USAGE_MONITOR`)

Como se recomendó, **el permiso no tiene efecto mientras el interruptor global está apagado**:

| Global | Permiso | `alfred usage` (bot) | `usage-alert add` | `list` / `remove` | Se evalúan sus avisos |
| --- | --- | --- | --- | --- | --- |
| apagado (defecto) | cualquiera | rechazado | rechazado | con el permiso, sí (para limpiar) | no: no hay monitor |
| encendido | apagado | rechazado | rechazado | rechazado | no |
| encendido | encendido | sí | sí | sí | sí |

El panel lo avisa con una nota, cuando el permiso está activado y el global apagado, en la respuesta
de `/bot perm`, en `/bot show` y en las pantallas de permisos de los menús. Dice que el permiso no
tiene efecto hasta poner `ALFRED_USAGE_MONITOR=1` y reiniciar.

### Qué se tocó del sistema de permisos (para el merge con `security-control`)

Cambios mínimos y localizados. No se ha cambiado `BotRecord`, `BotRegistry`, `overrides()`,
`ALWAYS_DENIED` ni el funcionamiento de ningún otro permiso.

| Archivo | Cambio |
| --- | --- |
| `alfred/bots.py` | Una fila en `PERMISSIONS` (`usage`, `off`). `"usage"` al final de `SWITCHES`. Dos constantes nuevas tras `BROWSER_RULES`: `USAGE_RULES` y `USAGE_UNSANDBOXED`. Una entrada en la lista de frases de `permission_summary()`. En `claude_settings()`, dos líneas de `deny` cuando falta el permiso y una suma en `excludedCommands`. |
| `alfred/menus.py` | `usage_monitor` en el import, una fila en `SWITCH_LABELS` y tres líneas en `_permission_text()` para la nota. |
| `alfred/telegram_bot.py` | `+usage` en el texto de ayuda de `/bot`, y la nota en `/bot perm` y `/bot show`. |
| `alfred/cli.py` | Funciones nuevas (`require_usage`, `usage_alert_bot`, `cmd_usage_alert_*`) y la comprobación en `cmd_usage`. Reutiliza `require`, `calling_bot`, `own_bot_only` y `own_chat_only` sin cambiarlos. |

Si `security-control` reorganiza los permisos (niveles, listas negras), basta con volver a declarar
`usage` como interruptor apagado por defecto y mantener `USAGE_RULES` en `deny` cuando esté apagado.
`tests/test_usage_permission.py` comprueba el comportamiento, no la posición de las líneas. Hay
que vigilar que el nivel de seguridad superior no permita `Bash(alfred *)` sin añadir esas
denegaciones.

## 12. Avisos para agentes

### Para qué

Un bot puede pedir que lo despierten cuando el uso cumpla una condición. Por ejemplo, el caso del
usuario: "queda menos de 1 h de ventana y no se ha consumido nada". El aviso llega **solo al bot
que lo registró**, como un evento que lo despierta en el chat (y tema) donde lo registró.

### Órdenes

```
alfred usage-alert add --when "session.left < 1h and session.used = 0" [--repeat] [nota libre...]
alfred usage-alert list
alfred usage-alert remove <id o prefijo>
```

Salida en JSON, como el resto de órdenes del agente. `add` devuelve el aviso, cada cuánto se
comprueba y el coste de despertarlo. Necesita el permiso `usage` y el interruptor global
encendido. El operador puede gestionarlos con `--bot <@username>`. Un bot solo ve y borra los suyos,
y solo puede registrarlos para sus propios chats (o para otros, con `contact`).

### Condiciones

| Valor | Significado | Ejemplo |
| --- | --- | --- |
| `session.used` (o `5h.used`) | % usado de la ventana de 5 h | `session.used < 10` |
| `session.left` (o `session.reset`) | Tiempo hasta que se reinicia la ventana de 5 h | `session.left < 1h` |
| `week.used` (o `weekly.used`) | % usado del semanal de todos los modelos | `week.used >= 80%` |
| `week.left` (o `week.reset`) | Tiempo hasta el reinicio semanal | `week.left <= 2d` |

Operadores `<`, `<=`, `>`, `>=`, `=` / `==` y `!=`. Se combinan con `and` / `&&`, `or` / `||`, `not`
y paréntesis, y `and` tiene prioridad sobre `or`. Los porcentajes van de 0 a 100, con o sin `%`. Los
tiempos llevan unidad (`s`, `m`, `h`, `d`, `w`). Una condición mal escrita se rechaza al registrarla,
con el motivo.

Un valor que no se pudo leer hace falsa su comparación. Pasa con una lectura fallida o cuando no hay
ventana de 5 h en marcha. Ojo con el ejemplo del usuario: la ventana de 5 h empieza con el primer
uso, así que si no se ha usado nada desde el último reinicio **no hay ventana ni hora de reinicio**,
y `session.left < 1h` es falso. La condición se cumple cuando hay una ventana en marcha con un uso
que el servidor redondea a 0 %. Para detectar que "no hay ventana" haría falta `session.active`
(pendiente).

### Cuándo se dispara

- **Una sola vez** por defecto: al dispararse queda gastado (`spent`) y sigue en la lista hasta
  que se borra. Con `--repeat`, como mucho **una vez por ventana**: la de las ventanas que nombra
  la condición, identificada por su hora de reinicio redondeada a 5 minutos, porque el servidor la
  devuelve con décimas de segundo de diferencia. En la ventana siguiente vuelve a armarse.
- El disparo se escribe en disco **antes** de crear el evento, con un bloqueo de archivo (lo
  escriben tanto `alfred usage-alert` como el monitor), así que un reinicio del proceso no lo
  repite.
- Se guardan en `state/bots/<id>/usage-alerts.json`, fuera del directorio del bot, que no puede
  leer el estado de Alfred. Solo los cambia a través de la orden. Como mucho 20 avisos activos por
  bot.

### Cómo llega

El monitor crea un `KIND_EVENT` en el planificador compartido con el `bot_id` del bot. `BotPool.deliver`
lo entrega solo a ese bot: a un bot en pausa se le aplaza y uno destruido da error, como cualquier
evento. El agente recibe, por ejemplo:

```
[Event] Usage alert met.

Alert 368ae85e: session.left < 1h and session.used = 0
Your note when you set it: use the spare window for the backlog
It will not fire again; it stays listed until removed.

Plan usage read at Thu 12 Nov 10:00 CET:
- 5-hour window: 0% used, resets Thu 12 Nov 10:50 CET (in 50m)
- Week (all models): 39% used, resets Sat 15 Nov 06:00 CET (in 2d 19h)
- Week (Fable): 59% used

Waking you used some plan usage. To stop an alert: alfred usage-alert remove <id>.
```

### Coste

- **Comprobar no gasta cuota.** Una lectura de `/usage` por ciclo, sin modelo, para todos los
  bots. Si ningún bot activo con permiso tiene avisos pendientes, no se lee nada.
- **Despertar sí gasta.** Cada aviso disparado es un turno del agente con todo su contexto, igual
  que si el usuario le escribiera. En una sesión larga puede ser una parte apreciable de la ventana.
- Límites: un bot no se despierta más de una vez por `ALFRED_USAGE_WAKE_INTERVAL` (1 h por
  defecto, mínimo 5 min). Los avisos que se cumplen antes esperan **sin gastarse** al primer ciclo
  después del intervalo. Si para entonces la condición ya no se cumple, no se disparan en esa
  ventana. Los avisos que se cumplen a la vez llegan en un solo evento. La comprobación es cada
  `ALFRED_USAGE_POLL` (15 min): una condición de menos de 15 minutos de margen, como
  `session.left < 10m`, puede pasar sin verse. El intervalo debe ser menor que el margen más
  estrecho.

### Qué falta probar en vivo

Todo lo anterior está probado con Telegram y Claude Code simulados. Queda:

1. Que el `usage_report` real de la versión instalada tenga la forma de la fixture, y qué devuelve
   cuando la ventana de 5 h no ha empezado.
2. Que la lectura con `--setting-sources project --strict-mcp-config` funcione desde el servicio
   (launchd), con su `PATH` y su acceso al llavero.
3. Que las reglas `deny` de `alfred usage*` bloqueen de verdad a un bot en Claude Code real, y que
   con `shell full` y sandbox las dos órdenes funcionen gracias a `excludedCommands`.
4. Un ciclo completo con Telegram real: registrar un aviso desde un bot, verlo dispararse una vez y
   que solo conteste ese bot.
5. El límite de frecuencia del endpoint de uso con lecturas cada 15 minutos durante un día.
6. Cuánto gasta un despertar real: `/usage` antes y después de un evento en una sesión larga.

## Fuentes

Oficiales:

- Claude Code, gestión de costes y `/usage`: <https://code.claude.com/docs/en/costs>
- Agent SDK TypeScript (`SDKUsageReport`, `SDKRateLimitEvent`, `usage_report`): <https://code.claude.com/docs/en/agent-sdk/typescript>
- Agent SDK Python (`RateLimitEvent`, `RateLimitInfo`): <https://code.claude.com/docs/en/agent-sdk/python>
- Status line, campos `rate_limits`: <https://code.claude.com/docs/en/statusline>
- Errores de límite de uso: <https://code.claude.com/docs/en/errors#usage-limits>
- Esperar al reinicio y `autoContinueAtUsageLimit`: <https://code.claude.com/docs/en/interactive-mode#wait-for-a-usage-limit-to-reset>
- Modo headless, `-p`, bare mode y `stream-json`: <https://code.claude.com/docs/en/headless>
- Referencia de la CLI (`--setting-sources`, `--strict-mcp-config`, `--no-session-persistence`): <https://code.claude.com/docs/en/cli-reference>
- Comandos (`/usage`, `/stats`, `/cost`, `/rate-limit-options`): <https://code.claude.com/docs/en/commands>
- Monitorización con OpenTelemetry: <https://code.claude.com/docs/en/monitoring-usage>
- Variables de entorno (`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, etc.): <https://code.claude.com/docs/en/env-vars>
- Legal y cumplimiento (OAuth, uso ordinario): <https://code.claude.com/docs/en/legal-and-compliance>
- Términos de consumidor: <https://www.anthropic.com/legal/consumer-terms>
- Política de uso: <https://www.anthropic.com/legal/aup>
- Centro de ayuda, Claude Code con Pro/Max: <https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan>

Límites por plan (sección 10):

- Precios: <https://claude.com/pricing>
- ¿Qué es el plan Max?: <https://support.claude.com/en/articles/11049741-what-is-the-max-plan>
- Anuncio del 6 de mayo de 2026: <https://www.anthropic.com/news/higher-limits-spacex>
- Semanales de agosto de 2025 (TechCrunch): <https://techcrunch.com/2025/07/28/anthropic-unveils-new-rate-limits-to-curb-claude-code-power-users/>
- Medición de la comunidad (no oficial): <https://wokey.ai/claude-usage-limits>
- Cronología no oficial de cambios de 2026: <https://www.explainx.ai/blog/claude-usage-limits-2026-timeline-explained>

Comunidad (no oficiales):

- ccusage: <https://github.com/ryoppippi/ccusage>
- Claude-Code-Usage-Monitor: <https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor>
- Ejemplos del endpoint no documentado: <https://docs.rs/crate/cship/1.7.0/source/src/usage_limits.rs>, <https://docs.rs/crate/clauth/0.5.5/source/src/usage/fetch.rs>
- Issue sobre el endpoint en planes Enterprise: <https://github.com/anthropics/claude-code/issues/34348>

Pruebas locales: Claude Code v2.1.322 en macOS. Tres consultas a `/usage` (json, stream-json y
stream-json con `--setting-sources project --strict-mcp-config`), todas con coste 0, y una llamada
de 4 tokens de salida a Haiku para capturar `rate_limit_event`.
