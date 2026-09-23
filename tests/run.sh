#!/usr/bin/env bash
# Bequemlichkeit fuer die Kommandozeile; die Arbeit macht tests/run.py, damit der Lauf
# auf allen Systemen derselbe ist (siehe Kommentar dort).
exec "$(command -v python3 || command -v python)" "$(dirname "$0")/run.py"
