#!/bin/zsh
# Installs two daily macOS jobs: scan at 17:00, send at 18:00.
# Uninstall: launchctl bootout gui/$(id -u)/com.autosubmit.scan ; same for .send
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/.venv/bin/python"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$DIR/logs"

make_plist() {  # label script hour
cat > "$LA/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/$2</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>$3</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$DIR/logs/$2.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/$2.log</string>
</dict></plist>
PLIST
launchctl bootout gui/$(id -u) "$LA/$1.plist" 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$LA/$1.plist"
echo "installed $1 at $3:00"
}

make_plist com.autosubmit.scan scan.py 17
make_plist com.autosubmit.send send.py 18
