#!/bin/bash
# Start the web UI at http://127.0.0.1:7788
cd "$(dirname "$0")"
exec .venv/bin/python app.py
