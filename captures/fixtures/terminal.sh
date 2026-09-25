#!/bin/sh
# Terminal fixture for the native `windows` / `terminal` captures. It prints the
# studio's shared transcript (web/fixtures/terminal-session.ans, the same bytes
# the terminal specimen renders), hides the cursor so no cursor cell differs
# between runs, and waits until the harness closes the window.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
printf '\033[?25l\033[H\033[2J'
cat "$here/../../web/fixtures/terminal-session.ans"
exec sleep 2147483647
