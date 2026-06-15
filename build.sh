#!/usr/bin/env bash
# Build the installable add-on zip: dist/blmn-ai-blender-addon.zip
# Thin wrapper around build.py (pure stdlib, cross-platform — no `zip` needed).
set -euo pipefail
cd "$(dirname "$0")"
exec "${PYTHON:-python3}" build.py
