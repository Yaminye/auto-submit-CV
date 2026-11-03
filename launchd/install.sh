#!/bin/zsh
# Installs the macOS background jobs (run it yourself - it changes your login items):
#   17:00   scan.py       search LinkedIn, filter, score with Claude -> new cards on the board
#   18:00   send.py       email/Telegram digest - only if SMTP/Telegram is set in .env
#   always  dashboard.py  the board at http://127.0.0.1:8765 (starts at login, restarts if it stops)
# The Mac must be awake at 17:00 for the scan.
#   ./launchd/install.sh              install / update
#   ./launchd/install.sh --uninstall  remove everything
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"
LABELS=(com.autosubmit.scan com.autosubmit.send com.autosubmit.dashboard)

remove() {
  launchctl bootout gui/$(id -u)/$1 2>/dev/null || true
  rm -f "$LA/$1.plist"
}

if [[ "$1" == "--uninstall" ]]; then
  for l in $LABELS; do remove $l; echo "removed $l"; done
  exit 0
fi

mkdir -p "$LA" "$DIR/logs"

write_plist() {  # label script launch-keys
cat > "$LA/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/$2</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  $3
  <key>StandardOutPath</key><string>$DIR/logs/$2.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$2.log</string>
</dict></plist>
PLIST
launchctl bootout gui/$(id -u) "$LA/$1.plist" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$LA/$1.plist"
}

at() { echo "<key>StartCalendarInterval</key><dict><key>Hour</key><integer>$1</integer><key>Minute</key><integer>0</integer></dict>"; }

write_plist com.autosubmit.scan scan.py "$(at 17)"
echo "installed: scan + scoring every day at 17:00"

if grep -qE '^(SMTP_APP_PASSWORD|TELEGRAM_BOT_TOKEN)=.+' "$DIR/.env" 2>/dev/null; then
  write_plist com.autosubmit.send send.py "$(at 18)"
  echo "installed: digest every day at 18:00"
else
  remove com.autosubmit.send
  echo "skipped:   18:00 digest (no email/Telegram in .env) - the board shows everything"
fi

write_plist com.autosubmit.dashboard dashboard.py "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
echo "installed: board at http://127.0.0.1:8765 (always on)"
