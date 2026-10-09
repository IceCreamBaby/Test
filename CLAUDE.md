# Hinweise für Claude

- Änderungen immer direkt auf `main` committen und pushen. **Keine Pull Requests** und keine Feature-Branches anlegen.
- Sprache: Der Nutzer schreibt Deutsch – Antworten, Oberfläche, README und Commit-Nachrichten auf Deutsch.
- Vor dem Push: `uv run pytest -q` und `uvx ruff check --select F,E9 podcast_animator tests`.
- Testmaterial (Podcast-Audio/-Video) niemals committen.
