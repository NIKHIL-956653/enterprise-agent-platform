# Dev bootstrap: infra up, deps installed, migrations applied, server running.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

docker compose up -d
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
alembic upgrade head
uvicorn eap.main:app --reload --port 8000