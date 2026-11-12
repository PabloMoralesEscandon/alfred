# Monitor de uso de la suscripción de Claude: investigación y diseño

Estado: investigación terminada; esqueleto del monitor implementado y desactivado por defecto (ver
[sección 9](#9-estado-de-la-implementación)). Fecha: 2026-11-12. Claude Code probado: v2.1.322.

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

### Lo que hay (esqueleto, sin funcionalidad)

`alfred/usage_monitor.py` contiene las piezas del diseño de la sección 7 con nombres definitivos.
Ninguna actúa todavía.

| Pieza | Qué es hoy | Nombre en la sección 7 |
| --- | --- | --- |
| `Window`, `Reading` | Modelo de datos de una lectura: tipo de medidor (`session`, `weekly_all`, `weekly_scoped` u otro), etiqueta, porcentaje, `resets_at` (UTC, opcional), `reset_text`, `severity` y `scope`. Se serializa a dict. `Reading.from_report()` lee el texto de `/usage` con la misma `usage.LIMIT_LINE`, así que `resets_at` queda vacío y solo se guarda `reset_text`. | `Limit`, `Snapshot` |
| `UsageSource`, `ClaudeUsageSource` | Interfaz de la fuente (`async read() -> Reading`) y la implementación que reutiliza `usage.py`. | `probe()` |
| `Rule`, `RuleContext`, `Alert`, `default_rules()` | Interfaz de reglas y las seis declaradas: `ThresholdRule` (R1), `ProjectionRule` (R2), `LimitReachedRule` (R3), `ResetRule` (R4), `SpareSessionRule` (R5) y `SpareWeeklyRule` (R6). Todas tienen `implemented = False` y `evaluate()` devuelve `[]`. | `evaluate()` |
| `UsageHistory` | Historial en `state/usage.json`: `append()` con escritura atómica y poda a 24 h, `readings()` y `latest()`. Tolera archivos y entradas dañados. No toca el disco hasta que se le llama. | `UsageStore` (aún sin las claves de avisos enviados) |
| `MonitorSettings`, `is_enabled()` | Configuración: `ALFRED_USAGE_MONITOR` (interruptor, desactivado por defecto) y los ajustes de la sección 7 (`_CHAT_ID`, `_POLL` con mínimo de 5 min, `_WARN`, `_CAP`, `ALFRED_QUIET_HOURS`, `_SPARE` y `_AUTORUN`). Un valor inválido conserva su valor por defecto y deja un aviso en el log. Vive en el propio módulo, no en `config.py`. Solo el interruptor tiene efecto. | Variables de `config.py` |
| `UsageMonitor`, `start()` | Punto de entrada. `BotPool.start` llama a `usage_monitor.start(config)` con la configuración de Alfred, una vez por proceso, justo después de arrancar el planificador. Con la configuración de un bot creado (`config.bot_id` distinto de 0) devuelve `None` sin leer nada. | `run_usage_monitor` |

Comportamiento garantizado por los tests (`tests/test_usage_monitor.py`):

- **Desactivado (por defecto):** `start()` lee solo el interruptor y devuelve `None`. No lee otros
  ajustes, no construye nada, no ejecuta `claude`, no escribe archivos y no registra nada en el log.
- **Activado:** construye las piezas y registra una sola línea (`usage monitor is on but not
  implemented yet ...`). No lee el uso, no escribe el historial, no manda mensajes y no programa
  tareas.
- Cada pieza se puede construir y usar por separado.

Únicos cambios fuera del módulo nuevo:

- `usage.py`: `fetch()` se ha dividido en `fetch_report()`, que devuelve el texto bruto o lanza
  `UsageUnavailable`, y `fetch()`, que lo resume. La orden y los mensajes de error son los mismos y
  `/usage` responde igual: los tests anteriores pasan sin cambios.
- `telegram_bot.py`: el import, el atributo `BotPool.usage_monitor` y la llamada a `start()` en
  `BotPool.start`. `TelegramBot` no tiene nada del monitor.
- `.env.example`: `ALFRED_USAGE_MONITOR=0`, documentado como esqueleto.

### Bots creados (tras el merge de `refactor` / agent-chats)

Desde ese merge, un proceso (`alfred run` → `BotPool`) sirve el panel de control y todos los bots que
Alfred ha creado. Cada bot tiene su `TelegramBot`, su `Agent` y su `Config` (`config_for(bot_id)`),
pero todos usan **la misma cuenta de Claude**: el mismo `claude` con el mismo login. Los límites del
plan son de esa cuenta, así que:

- **El monitor corre una sola vez por proceso, en `BotPool.start`, con la configuración de Alfred.**
  No va en `TelegramBot`: un monitor por bot repetiría las consultas al endpoint de uso, que tiene
  límite de frecuencia, y duplicaría los avisos. `TelegramBot._post_init` ya no es la ruta de
  arranque (el pool llama a `started()`), así que con el enganche anterior el monitor no se habría
  activado nunca.
- `start()` devuelve `None` con la configuración de un bot creado (`config.bot_id`), aunque sus
  ajustes propios (`overrides`) activen el interruptor. Lo comprueban los tests
  (`CreatedBotTests` y `PoolWiringTests`).
- El historial va en `state/usage.json` de Alfred, no en el estado de cada bot.
- `/usage` sigue en cada bot como en `refactor`: todos muestran los mismos límites, los de la
  cuenta.

Pendiente por esta razón, cuando haya lógica:

- Los avisos deben salir del **panel**, no de un bot concreto. El chat por defecto es el del dueño
  con el panel (`ALFRED_USAGE_CHAT_ID`, o el único id numérico de `ALFRED_ALLOWED_USERS`).
- El tope de gasto (paso 7) afecta a las tareas programadas de **todos** los bots: va en
  `BotPool.deliver`, que es el planificador compartido, no en cada `TelegramBot`.
- La captura pasiva de `rate_limit_event` (paso 2) llega por el `Agent` de cada bot: todos deben
  escribir en el mismo historial del proceso (a través del pool), no en uno por bot.
- La proyección (R2) mezcla el consumo de todos los bots y del uso fuera de Alfred. El mensaje no
  debe atribuirlo a un bot. Saber qué bot consumió más necesitaría otra fuente (`total_cost_usd`
  por turno), y queda fuera del alcance.
- Las propuestas de cuota sobrante (`/spare`, paso 8) deberán decir **a qué bot** se encarga cada
  tarea pendiente, o guardarlas por bot (`bot_id` en la tabla `backlog`, igual que en el
  planificador).

### Lo que falta

Por orden, siguiendo los pasos de la sección 7:

1. **Fuente estructurada.** `ClaudeUsageSource` debería pedir `stream-json --verbose` con
   `--setting-sources project --strict-mcp-config` y `cwd` en `state/usage-probe/`, leer
   `usage_report.rate_limits` (con `resets_at` y `severity` reales) y dejar el texto como respaldo.
   Hace falta una `Reading.from_usage_report()`, detectar `rate limited until` y una fixture real
   para los tests. Puede hacerse en `usage_monitor.py` sin tocar `fetch()`, o pasando `/usage`
   también al formato estructurado.
2. **Captura pasiva.** `Listener.rate_limit()`, el manejo de `rate_limit_event` en `live.py` y
   `claude.py`, y `Reading.from_rate_limit_event()`.
3. **Historial completo.** Claves de avisos enviados en `UsageHistory`, para la deduplicación entre
   reinicios, y `alfred usage --json`.
4. **Lógica de R1, R3 y R4**, deduplicación y horas de silencio, con tests con reloj fijo.
5. **Bucle de consulta.** Sustituir `announce()` por una tarea de asyncio con intervalo
   adaptativo y espera exponencial, cancelada en `BotPool.stop`, que envíe desde el panel
   (`BotPool.main`) a `settings.chat_id`. Test de que nunca llama a `agent.submit` de ningún bot.
6. **Lógica de R2, R5 y R6** (proyección y cuota sobrante), con mensajes simples.
7. **Tope en `BotPool.deliver`** (el planificador compartido por todos los bots), usando
   `cap_percent`. `refactor` ya trae `Postpone` en el planificador; ver si sirve en lugar de un
   `SchedulerStore.defer()` nuevo.
8. **Backlog**: tabla, `alfred backlog`, skill base, `/spare N`, y `autorun` como opción explícita.
9. **Documentación de usuario**: README (`/spare`, la sección de configuración), `.env.example`
   con los ajustes restantes cuando tengan efecto, y `docs/PLAN.md`.
10. **Prueba real** de un día (sección 7, paso 10).

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

Comunidad (no oficiales):

- ccusage: <https://github.com/ryoppippi/ccusage>
- Claude-Code-Usage-Monitor: <https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor>
- Ejemplos del endpoint no documentado: <https://docs.rs/crate/cship/1.7.0/source/src/usage_limits.rs>, <https://docs.rs/crate/clauth/0.5.5/source/src/usage/fetch.rs>
- Issue sobre el endpoint en planes Enterprise: <https://github.com/anthropics/claude-code/issues/34348>

Pruebas locales: Claude Code v2.1.322 en macOS. Tres consultas a `/usage` (json, stream-json y
stream-json con `--setting-sources project --strict-mcp-config`), todas con coste 0, y una llamada
de 4 tokens de salida a Haiku para capturar `rate_limit_event`.
