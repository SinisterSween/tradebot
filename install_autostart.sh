#!/bin/bash
set -e

AGENT_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$AGENT_DIR"

TRADEALERTS="$AGENT_DIR/com.tradebot.alerts.plist"


echo "Creating Tradebot LaunchAgents in $AGENT_DIR ..."

# --- Start Job ---
cat > "$TRADEALERTS" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tradebot.alerts</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-c</string>
    <string>cd /Users/hatefulsween/code/tradebot && make alerts &gt;&gt; logs/alerts.log 2&gt;&amp;1</string>
  </array>
  <key>StartInterval</key><integer>900</integer> <!-- every 15 minutes -->
  <key>StandardOutPath</key><string>/Users/hatefulsween/code/tradebot/logs/alerts.out</string>
  <key>StandardErrorPath</key><string>/Users/hatefulsween/code/tradebot/logs/alerts.err</string>
</dict>
</plist>
EOF
echo "Loading Trade Alert job..."
launchctl load "$TRADEALERTS"


echo "✅ Trade Alerts jobs installed."
