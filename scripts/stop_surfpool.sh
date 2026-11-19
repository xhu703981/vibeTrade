#!/usr/bin/env bash
cd "$(dirname "$0")/.."
for n in main scratch; do f=.surfpool/$n/pid; [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && echo "stopped $n"; rm -f "$f"; done
