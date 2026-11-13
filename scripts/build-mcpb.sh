#!/usr/bin/env bash
# Pack the Claude Desktop extension (dist/apple-calendar-mcp-<version>.mcpb).
#
# The bundle holds only mcpb/ and the icon: Claude Desktop installs the server
# from PyPI with uv, so it must already be published at the pinned version.
set -euo pipefail
cd "$(dirname "$0")/.."

MCPB_VERSION=2.1.2
version="$(jq -r .version mcpb/manifest.json)"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp mcpb/manifest.json mcpb/pyproject.toml mcpb/server.py mcpb/.mcpbignore "$stage/"
cp .claude-plugin/icon.png "$stage/icon.png"

mkdir -p dist
npx --yes "@anthropic-ai/mcpb@$MCPB_VERSION" validate "$stage/manifest.json"
npx --yes "@anthropic-ai/mcpb@$MCPB_VERSION" pack "$stage" "dist/apple-calendar-mcp-$version.mcpb"
echo "dist/apple-calendar-mcp-$version.mcpb"
