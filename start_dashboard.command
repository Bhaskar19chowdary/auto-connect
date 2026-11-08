#!/bin/bash
cd "$(dirname "$0")"
echo "=========================================================="
echo " Starting Auto-Connect Enterprise Banking Dashboard"
echo "=========================================================="
echo "Opening browser at http://localhost:8088..."
sleep 1 && open http://localhost:8088 &
.venv/bin/python -m auto_connect.main --server --port 8088

