# Phalanx

Minimal AI agent ported from [hermes-agent](https://github.com/NousResearch/hermes-agent).

Migration is in progress — see [`docs/MIGRATION_PLAN.md`](docs/MIGRATION_PLAN.md) for the phased plan.

## Status

- **Phase 0** ✅ — project skeleton + import-safe top-level modules
- **Phase 1** ⏳ — minimal `AIAgent.run_conversation` loop + minimal CLI
- **Phase 2+** — see plan

## Phase 0 quick verification

```bash
python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"
python -c "import hermes_logging, hermes_time, utils; print('ok')"
```
