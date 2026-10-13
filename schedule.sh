#!/bin/bash
# Run job_alerts.py every day via launchd (macOS).
#   ./schedule.sh           daily at 08:00
#   ./schedule.sh 7:30      daily at 07:30
#   ./schedule.sh --remove  stop the daily run
# If the Mac is asleep at that time, launchd runs it when it wakes.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.linkedin-job-alerts"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [[ "${1:-}" == "--remove" ]]; then
  rm -f "$PLIST"
  echo "Removed the daily LinkedIn job alert."
  exit 0
fi

TIME="${1:-8:00}"
if [[ ! "$TIME" =~ ^([0-9]{1,2}):([0-9]{2})$ ]]; then
  echo "usage: $0 [HH:MM | --remove]" >&2
  exit 1
fi
HOUR=$((10#${BASH_REMATCH[1]}))
MINUTE=$((10#${BASH_REMATCH[2]}))
if (( HOUR > 23 || MINUTE > 59 )); then
  echo "invalid time: $TIME" >&2
  exit 1
fi

PYTHON=""
for candidate in "$DIR/venv/bin/python" "$DIR/.venv/bin/python"; do
  if [[ -x "$candidate" ]]; then PYTHON="$candidate"; break; fi
done
if [[ -z "$PYTHON" ]]; then
  echo "No virtualenv found in $DIR/venv or $DIR/.venv; set one up first (see README.md)." >&2
  exit 1
fi

mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$DIR/job_alerts.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$DIR/logs/job_alerts.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/job_alerts.log</string>
</dict>
</plist>
EOF

launchctl bootstrap "$DOMAIN" "$PLIST"
printf "Scheduled daily at %02d:%02d.\n" "$HOUR" "$MINUTE"
echo "Logs:        $DIR/logs/job_alerts.log"
echo "Run it now:  launchctl kickstart $DOMAIN/$LABEL"
