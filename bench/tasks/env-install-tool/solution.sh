set -eu
: "${BENCH_HOME:?}"
mkdir -p "$BENCH_HOME/.local/bin"
{
    printf '#!/usr/bin/env python3\n'
    cat tools/messages.py
    grep -v '^from messages import' tools/hello.py
} > "$BENCH_HOME/.local/bin/hello"
chmod +x "$BENCH_HOME/.local/bin/hello"
