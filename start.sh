#!/usr/bin/env bash
# Startet den Podcast Animator (Linux / macOS). Installiert beim ersten Mal automatisch alles Nötige.
cd "$(dirname "$0")" || exit 1
if ! command -v uv >/dev/null 2>&1; then
  if [ -x "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo "Installiere uv (Python-Paketmanager) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi
fi
exec uv run python -m podcast_animator "$@"
