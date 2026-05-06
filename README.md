# Phalanx

Minimal AI agent ported from [hermes-agent](https://github.com/NousResearch/hermes-agent).

Migration is in progress — see [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md) for the phased plan and [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the current state.

## Quick start

```bash
# REPL (default `hermes` entry point)
python -m hermes_cli                    # interactive REPL
python -m hermes_cli oneshot "..."      # single-turn query

# tools / config / sessions
python -m hermes_cli tools list
python -m hermes_cli session list
python -m hermes_cli logs <session-id> --follow
python -m hermes_cli doctor
```

## Web dashboard (Phase 2.7)

Browser-based console for sessions / logs / config / env / analytics.
Full guide: [`docs/phase-2.7-web-dashboard.md`](docs/phase-2.7-web-dashboard.md).

```bash
# First time only — build the React SPA into hermes_cli/web_dist/
cd web && npm install && npm run build && cd ..

# Start the server (auto-opens browser at http://127.0.0.1:9119)
python -m hermes_cli web

# Or, dev mode with HMR:
python -m hermes_cli web --no-open       # terminal A — backend only
cd web && npm run dev                    # terminal B — Vite at :5173 proxies /api → :9119
```

`pip install`-ed wheels ship with `web_dist/` already built, so `hermes web` works without a Node toolchain on the install host.
