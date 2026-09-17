#!/usr/bin/env bash
# Remove Jarvis. Leaves your config unless you pass --purge.
# Non-interactive: pass -y / --yes, or set JARVIS_UNINSTALL_YES=1.
set -euo pipefail

JARVIS_DIR="$HOME/.local/share/jarvis"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/dorian.voice"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/jarvis"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/jarvis.service"

purge=false
yes=false
for arg in "$@"; do
  case "$arg" in
    --purge) purge=true ;;
    -y|--yes) yes=true ;;
    -h|--help)
      echo "Usage: uninstall.sh [-y|--yes] [--purge]"
      echo "  -y, --yes   skip the confirmation prompt"
      echo "  --purge     also remove ~/.config/jarvis"
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      echo "Usage: uninstall.sh [-y|--yes] [--purge]" >&2
      exit 1
      ;;
  esac
done
[[ ${JARVIS_UNINSTALL_YES:-} == 1 ]] && yes=true

echo "This will remove:"
echo "  $JARVIS_DIR  (daemon, venv, voice model)"
echo "  $PLUGIN_DIR  (bar widget)"
echo "  $UNIT"
$purge && echo "  $CONFIG_DIR  (your config)"

if ! $yes; then
  read -rp "Continue? [y/N] " reply
  [[ ${reply,,} == y* ]] || { echo "aborted"; exit 0; }
fi

systemctl --user stop jarvis.service 2>/dev/null || true
systemctl --user disable jarvis.service 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload 2>/dev/null || true

rm -rf "$JARVIS_DIR" "$PLUGIN_DIR"
$purge && rm -rf "$CONFIG_DIR"

echo
echo "Removed. Take the Voice Assistant widget out of your bar layout"
echo "(Omarchy menu -> Setup -> Bar) if it is still there."
$purge || echo "Your config is still at $CONFIG_DIR -- delete it or re-run with --purge."
