#!/bin/bash
set -e

# Auto-detect project root (where this script lives)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
SERVER_SCRIPT="mcp_servers/memory_server_sse.py"
PORT=8071

# Auto-detect the user running the script (use sudo's original user if applicable)
RUN_USER="${SUDO_USER:-$USER}"
RUN_GROUP=$(id -gn "$RUN_USER" 2>/dev/null || echo "$RUN_USER")

if [ ! -f "$VENV_PYTHON" ]; then
  echo "Error: venv Python not found at $VENV_PYTHON"
  echo "Make sure you're running this from the Odysseus project root."
  exit 1
fi

if [ ! -f "$PROJECT_DIR/$SERVER_SCRIPT" ]; then
  echo "Error: $SERVER_SCRIPT not found in $PROJECT_DIR"
  exit 1
fi

echo "🚀 Installing Odysseus Memory MCP Server (SSE) systemd service..."
echo "   Project: $PROJECT_DIR"
echo "   User:    $RUN_USER"
echo "   Port:    $PORT"
echo ""

# Generate the service file dynamically with absolute paths
sudo tee /etc/systemd/system/odysseus-memory-sse.service > /dev/null <<EOF
[Unit]
Description=Odysseus Memory MCP Server (SSE transport)
Documentation=https://github.com/smalldata/odysseus
Wants=network.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$PROJECT_DIR
ExecStart=$VENV_PYTHON $PROJECT_DIR/$SERVER_SCRIPT --port $PORT
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable odysseus-memory-sse
sudo systemctl restart odysseus-memory-sse

echo ""
echo "✅ Service installed and started."
echo ""
sudo systemctl status odysseus-memory-sse --no-pager
echo ""
echo "📡 SSE endpoint: http://$(hostname -I | awk '{print $1}'):$PORT/sse"
