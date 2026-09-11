# PrimeCut

Drop in a two-hour podcast episode. PrimeCut transcribes it on GPUs, reads the transcript with Gemini, watches the frames with a VLM, scores every moment against your rubric, then composes two formats from one ranking: a 15-minute best-of cut for YouTube and 60-second 9:16 verticals for Shorts, Reels, and TikTok.

## Repo map

```
primecut/
  api/                 Python package + API (uv project; pipeline modules live in api/primecut/)
    primecut/          config.py is real; every other module is a placeholder naming its task
    tests/             test_config.py (settings + friendly failure), test_no_secrets.py (public-repo guard)
    .env.example       every key name, no values; copy to api/.env
  web/                 Next.js 16 app (App Router, TypeScript, Tailwind)
    .env.example       copy to web/.env.local
  fixtures/            small test media for probe / mezzanine / transcribe tests (arrives in Task 2)
  docker-compose.yml   Postgres 16 with a pg_isready healthcheck
  .gitignore           ignores secrets, caches, work dirs, and media; un-ignores fixtures/**
```

## Quickstart

Each block below starts from the repo root.

Start Postgres (matches the default `database_url` in `api/primecut/config.py`):

```bash
docker compose up -d
docker compose ps   # postgres should report "healthy"
```

Set up the Python package and API:

```bash
cd api && uv sync
cp .env.example .env   # fill in GOOGLE_API_KEY and HF_TOKEN
```

Set up the web app:

```bash
cd web && npm install
cp .env.example .env.local
```

Run the web dev server:

```bash
cd web && npm run dev
```

Verify the API side:

```bash
cd api
uv run pytest -q
uv run python -c "from primecut.config import settings; print(settings.ranking_model)"
# -> gemini-3.1-pro-preview
```

If `api/.env` is missing a required key, the settings print fails with a short human-readable
error that names the missing variables and where to get them, not a pydantic traceback.

## Secrets

This repo is public. Secrets live only in `api/.env` and `web/.env.local`, both gitignored.
`.env.example` is the only committed copy and holds key names with no values.
`api/tests/test_no_secrets.py` scans every tracked file for key-shaped strings on each run.
If a key ever leaks, rotate it; deleting it from a later commit does nothing, because git
history is forever and public history is scraped within minutes.
