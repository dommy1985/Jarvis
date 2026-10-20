#!/usr/bin/env bash
# Installa il runner Jarvis: token in ~/.config/jarvis/runner.env (copiato dal server senza stamparlo) e un
# servizio utente che lo tiene acceso: LaunchAgent su macOS, unit systemd --user su Linux. Rilanciabile.
#   scripts/install-runner.sh            installa o aggiorna
#   scripts/install-runner.sh --remove   ferma e rimuove il servizio (il token resta)
# Collegamento al server (variabili d'ambiente, scritte in runner.env alla prima installazione):
#   JARVIS_SSH_HOST    host SSH del server: il runner apre il tunnel (host separati). Vuoto: nessun tunnel.
#   JARVIS_URL         URL del server (un solo host, o porta locale diversa dal tunnel).
#   JARVIS_SSH_LOCAL_PORT / JARVIS_SSH_REMOTE_PORT   porte del tunnel (default 8090).
#   JARVIS_SERVER_ENV  file .env sul server da cui copiare il token via SSH (con JARVIS_SSH_HOST).
#   JARVIS_RUNNER_TOKEN  token già in mano: niente accesso SSH al server.
# Servizio:
#   JARVIS_RUNNER_LABEL  macOS: label del LaunchAgent (default dev.jarvis.runner).
#   JARVIS_RUNNER_LEGACY_LABEL  macOS: label di una vecchia installazione, fermata e rimossa se presente.
#   JARVIS_RUNNER_UNIT   Linux: nome dell'unit systemd (default jarvis-runner).
#   JARVIS_RUNNER_LOG    macOS: file di log (default ~/Library/Logs/jarvis-runner.log); su Linux: journalctl --user.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${JARVIS_SSH_HOST:-}"
SERVER_ENV="${JARVIS_SERVER_ENV:-}"
LEGACY_LABEL="${JARVIS_RUNNER_LEGACY_LABEL:-}"
LABEL="${JARVIS_RUNNER_LABEL:-dev.jarvis.runner}"
UNIT="${JARVIS_RUNNER_UNIT:-jarvis-runner}"
LOG="${JARVIS_RUNNER_LOG:-$HOME/Library/Logs/jarvis-runner.log}"
ENV_FILE="$HOME/.config/jarvis/runner.env"
OS="$(uname -s)"
case "$OS" in
  Darwin) PLIST="$HOME/Library/LaunchAgents/$LABEL.plist" ;;
  Linux) UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"; UNIT_FILE="$UNIT_DIR/$UNIT.service" ;;
  *) echo "sistema non supportato: $OS (solo macOS e Linux)"; exit 1 ;;
esac

remove_launchagent() {  # $1 = label
  launchctl bootout "gui/$(id -u)/$1" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$1.plist"
}

if [[ "${1:-}" == "--remove" ]]; then
  if [[ "$OS" == "Darwin" ]]; then
    remove_launchagent "$LABEL"
    [[ -z "$LEGACY_LABEL" || "$LEGACY_LABEL" == "$LABEL" ]] || remove_launchagent "$LEGACY_LABEL"
  else
    systemctl --user disable --now "$UNIT.service" 2>/dev/null || true
    rm -f "$UNIT_FILE"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  echo "runner rimosso"
  exit 0
fi

if [[ ! -s "$ENV_FILE" ]]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  umask 077
  if [[ -n "${JARVIS_RUNNER_TOKEN:-}" ]]; then
    echo "JARVIS_RUNNER_TOKEN=$JARVIS_RUNNER_TOKEN" > "$ENV_FILE"
  elif [[ -n "$HOST" && -n "$SERVER_ENV" ]]; then
    ssh "$HOST" "grep '^JARVIS_RUNNER_TOKEN=.' $SERVER_ENV" > "$ENV_FILE" \
      || { rm -f "$ENV_FILE"; echo "JARVIS_RUNNER_TOKEN non trovato in $SERVER_ENV su $HOST"; exit 1; }
  else
    echo "Serve JARVIS_RUNNER_TOKEN (o JARVIS_SSH_HOST per copiarlo dal server): vedi l'intestazione dello script"
    exit 1
  fi
  for var in JARVIS_SSH_HOST JARVIS_URL JARVIS_SSH_LOCAL_PORT JARVIS_SSH_REMOTE_PORT; do
    if [[ -n "${!var:-}" ]]; then echo "$var=${!var}" >> "$ENV_FILE"; fi
  done
  chmod 600 "$ENV_FILE"
  echo "token scritto in $ENV_FILE"
fi

UV="$(command -v uv)" || { echo "uv non trovato nel PATH"; exit 1; }
if [[ "$OS" == "Darwin" ]]; then
  mkdir -p "$HOME/Library/LaunchAgents" "$(dirname "$LOG")"
  sed -e "s|@LABEL@|$LABEL|g" -e "s|@UV@|$UV|g" -e "s|@REPO@|$REPO|g" -e "s|@LOG@|$LOG|g" "$REPO/runner/jarvis-runner.plist" > "$PLIST"
  [[ -z "$LEGACY_LABEL" || "$LEGACY_LABEL" == "$LABEL" ]] || remove_launchagent "$LEGACY_LABEL"
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "runner avviato. Log: tail -f $LOG"
else
  mkdir -p "$UNIT_DIR"
  sed -e "s|@UV@|$UV|g" -e "s|@REPO@|$REPO|g" -e "s|@UNIT@|$UNIT|g" "$REPO/runner/jarvis-runner.service" > "$UNIT_FILE"
  systemctl --user daemon-reload
  systemctl --user enable "$UNIT.service"
  systemctl --user restart "$UNIT.service"
  echo "runner avviato. Log: journalctl --user -u $UNIT -f"
  echo "Per tenerlo acceso anche senza login: loginctl enable-linger $USER"
fi
