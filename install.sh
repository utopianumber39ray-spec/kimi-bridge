#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
mkdir -p "$UNIT_DIR" "$ROOT/dump"
PYTHON="${PYTHON:-/home/ai/anu_ai/.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON="$(command -v python3)"
"$PYTHON" - <<'PY'
import importlib.util
assert importlib.util.find_spec('websocket'), 'python3 -m pip install --user websocket-client requests'
assert importlib.util.find_spec('requests'), 'python3 -m pip install --user requests'
PY
# Use the current system user's config directory; no account or cookie data is copied.
for service in kimi-playground.service nv-playground-browser.service kimi-bridge-watchdog.service kimi-bridge-watchdog.timer; do
  sed -e "s|@ROOT@|$ROOT|g" \
      -e "s|@PYTHON@|$PYTHON|g" \
      -e "s|@HOME@|$HOME|g" \
      -e "s|@UID@|$(id -u)|g" \
      "$ROOT/systemd/$service" > "$UNIT_DIR/$service"
done
systemctl --user daemon-reload
systemctl --user enable kimi-playground.service nv-playground-browser.service
systemctl --user enable --now kimi-playground.service
if [[ "${1:-}" == "--attach-existing" ]]; then
  echo "Bridge installed; existing CDP 9333 browser session is used. No browser restart requested."
fi
if [[ "${1:-}" == "--enable-watchdog" ]]; then
  systemctl --user enable --now kimi-bridge-watchdog.timer
fi
printf 'Package: %s\nBridge: http://127.0.0.1:8120/v1/chat/completions\nCDP: http://127.0.0.1:9333\n' "$ROOT"
