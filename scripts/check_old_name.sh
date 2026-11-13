#!/usr/bin/env bash
# Busca restos de un nombre viejo en el repo (y, opcionalmente, en la máquina). Solo lee.
#
#   check_old_name.sh [-n alfred] [-r .] [-v] [--runtime]
#
#   -n NOMBRE   nombre viejo (por defecto: alfred). Se busca sin distinguir mayúsculas.
#   -r RAIZ     raíz del repo (por defecto: .)
#   -v          lista cada línea encontrada, no solo los recuentos
#   --runtime   mira también el home, el servicio y los proyectos de Claude Code (solo lectura)
#
# Una línea que contenga el marcador `rename-compat` se da por intencionada (alias, lectura de la
# variable vieja, migración) y no cuenta. Un fichero `.rename-allow` en la raíz, con un patrón de
# ruta por línea (sintaxis de `case`, por ejemplo `docs/*RESEARCH*.md`), excluye ficheros enteros.
# Sale con 0 si no queda nada, 1 si quedan restos.
set -u
OLD=alfred; ROOT=.; VERBOSE=0; RUNTIME=0
while [ $# -gt 0 ]; do
  case "$1" in
    -n) OLD="$2"; shift 2 ;;
    -r) ROOT="$2"; shift 2 ;;
    -v) VERBOSE=1; shift ;;
    --runtime) RUNTIME=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "opción desconocida: $1" >&2; exit 2 ;;
  esac
done
cd "$ROOT" || exit 2
UPPER=$(printf '%s' "$OLD" | tr '[:lower:]' '[:upper:]')

allowed() {  # ¿excluye .rename-allow este fichero?
  [ -f .rename-allow ] || return 1
  while IFS= read -r pattern; do
    case "$pattern" in ''|'#'*) continue ;; esac
    # shellcheck disable=SC2254
    case "$1" in $pattern) return 0 ;; esac
  done < .rename-allow
  return 1
}

hits=$(grep -rIni "$OLD" . \
  --exclude-dir=.git --exclude-dir=venv --exclude-dir=.venv --exclude-dir=node_modules \
  --exclude-dir=__pycache__ --exclude-dir='*.egg-info' --exclude-dir=build --exclude-dir=dist \
  --exclude=.git --exclude=.rename-allow --exclude='check_old_name.sh' 2>/dev/null \
  | grep -v 'rename-compat' | sed 's|^\./||')
filtered=""
while IFS= read -r line; do
  [ -n "$line" ] || continue
  allowed "${line%%:*}" && continue
  filtered="$filtered$line"$'\n'
done <<< "$hits"

category() {
  case "$1" in
    tests/*) echo tests ;;
    */templates/*) echo plantillas ;;
    */plugin/*) echo plugin-skills ;;
    docs/*|README*|*.md) echo documentacion ;;
    pyproject.toml|setup.cfg|setup.py|.env*|.gitignore) echo configuracion ;;
    *.py|*/bin/*) echo codigo ;;
    *) echo otros ;;
  esac
}

total=$(printf '%s' "$filtered" | grep -c . || true)
echo "== '$OLD' en $(pwd): $total líneas sin marcar"
printf '%s' "$filtered" | while IFS= read -r line; do [ -n "$line" ] && category "${line%%:*}"; done \
  | sort | uniq -c | sort -nr | sed 's/^/   /'

echo "== variables ${UPPER}_* distintas"
printf '%s' "$filtered" | grep -o "${UPPER}_[A-Z0-9_]*[A-Z0-9]" | sort -u | tr '\n' ' '; echo
echo "== rutas ~/.$OLD*"
printf '%s' "$filtered" | grep -oi "\.$OLD[a-z_-]*" | sort | uniq -c | sed 's/^/   /'
echo "== comandos '$OLD <subcomando>' y prefijo de skills '$OLD:'"
printf '%s' "$filtered" | grep -oE "\b$OLD (run|init|service|usage|usage-alert|browser-setup|chat|skills|send|event|ext|bots|chrome|screenshot|schedule)\b" | wc -l | sed 's/^ */   comandos: /'
printf '%s' "$filtered" | grep -oE "\b$OLD:[a-z<]" | wc -l | sed 's/^ */   skills:   /'
echo "== ficheros y carpetas con '$OLD' en el nombre"
find . \( -path ./.git -o -path ./venv -o -path ./.venv -o -name '*.egg-info' -o -name __pycache__ \) -prune \
  -o -iname "*$OLD*" -print | sed 's|^\./|   |'

if [ "$VERBOSE" = 1 ]; then echo "== detalle"; printf '%s' "$filtered"; fi

if [ "$RUNTIME" = 1 ]; then
  echo "== máquina (solo lectura)"
  for path in "$HOME/.$OLD" "$HOME/.$OLD-bots" "$HOME/.$OLD-shared" \
              "$HOME/Library/LaunchAgents/com.$OLD.bot.plist" "$HOME/.config/systemd/user/$OLD.service"; do
    if [ -L "$path" ]; then echo "   enlace  $path -> $(readlink "$path")"
    elif [ -e "$path" ]; then echo "   existe  $path"; fi
  done
  ls -d "$HOME/.claude/projects/"*"-.$OLD"* "$HOME/.claude/projects/"*"--$OLD"* 2>/dev/null | sort -u | sed 's/^/   proyecto Claude Code: /'
  command -v "$OLD" >/dev/null 2>&1 && echo "   comando en PATH: $(command -v "$OLD")"
  if command -v launchctl >/dev/null 2>&1; then
    launchctl list 2>/dev/null | grep -i "com.$OLD" | sed 's/^/   launchd: /'
  fi
fi

[ "$total" -eq 0 ]
