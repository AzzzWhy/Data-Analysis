#!/usr/bin/env bash
# Load STEPFUN_API_KEY from ~/.bashrc even in a non-interactive shell.
#
# Ubuntu's default ~/.bashrc returns early for non-interactive shells, so `source ~/.bashrc`
# silently does nothing and the key is missing. This extracts and exports the one variable
# we need, without printing it.
set -a
if [ -z "${STEPFUN_API_KEY:-}" ]; then
  eval "$(grep -E '^[[:space:]]*export[[:space:]]+STEPFUN_API_KEY=' "$HOME/.bashrc" 2>/dev/null | tail -1)"
fi
set +a