#!/usr/bin/env bash
# Compile the helper app's launcher as a universal binary (Apple silicon +
# Intel, macOS 11+) into the package, so the wheel ships it prebuilt and
# `install-app` needs no compiler. Run on macOS; the release workflow does.
set -euo pipefail
cd "$(dirname "$0")/.."
src="src/apple_calendar_mcp/macos_helper/launcher.c"
out="src/apple_calendar_mcp/macos_helper/apple-calendar-mcp-launcher"
clang -O2 -Wall -Werror -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 -o "$out" "$src"
archs="$(lipo -archs "$out")"
[ "$archs" = "x86_64 arm64" ] || { echo "unexpected architectures: $archs" >&2; exit 1; }
echo "built $out ($archs)"
