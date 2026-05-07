# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## OpenWolf

@.wolf/OPENWOLF.md

This project uses OpenWolf for context management. Read and follow .wolf/OPENWOLF.md every session. Check .wolf/cerebrum.md before generating code. Check .wolf/anatomy.md before reading files.

## Project Overview

**yex-trader** (aka agent-cli / Nunchi) — autonomous trading agent for Hyperliquid perps and YEX yield markets. Python 3.10+, CLI entry point `hl`. 14 strategies, APEX multi-slot orchestrator, REFLECT self-improvement loop, MCP server, Agent Skills.

## Build & Test Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run all tests
pytest tests/ -v

# Run a single test file
pytest tests/test_apex_engine.py -v

# Run a single test
pytest tests/test_apex_engine.py::test_function_name -v

# Run quoting engine tests only
pytest tests/quoting_engine/ -v

# Lint
ruff check .

# Type check
mypy .
```

## Architecture

### Module Layout & Separation of Concerns

The codebase follows a strict **pure engine + I/O bridge** pattern:

- **`modules/`** — Pure computation engines (zero I/O). Each `*_engine.py` is stateless logic; each `*_guard.py` is its I/O bridge handling persistence, logging, and wiring to the engine. Examples: `apex_engine.py` + `strategy_guard.py`, `trailing_stop.py` + `guard_bridge.py`, `pulse_engine.py` + `pulse_guard.py`.
- **`strategies/`** — 14 strategy implementations. All extend `sdk.strategy_sdk.base.BaseStrategy` with a single `on_tick(snapshot, context)` method. No shared state between strategies.
- **`cli/`** — CLI commands (Typer), trading engine tick loop (`engine.py`), HL adapter (`hl_adapter.py`), config loading, MCP server.
- **`cli/commands/`** — Subcommand modules registered in `cli/main.py`.
- **`parent/`** — Hyperliquid API proxy (`hl_proxy.py`), position tracking, risk management, SQLite/JSONL stores.
- **`common/`** — Shared models (Pydantic), credential management, logging, crypto utilities.
- **`execution/`** — Order routing, TWAP execution, managed order book, portfolio risk.
- **`quoting_engine/`** — HFT quoting pipeline: fair value → spread → inventory skew → ladder → orders. Used by `engine_mm`, `regime_mm`, `funding_arb`, `liquidation_mm`, `grid_mm` strategies.
- **`adapters/`** — `VenueAdapter` implementations wrapping existing proxies (HL live, mock).
- **`sdk/`** — `BaseStrategy` interface, dynamic loader, model registry.
- **`skills/`** — Agent Skills (SKILL.md + standalone runners) for APEX, Guard, Radar, Pulse, Reflect, Onboard.

### Key Data Flow

```
CLI (hl run/apex) → TradingEngine tick loop → Strategy.on_tick() → StrategyDecision
                  → OrderManager → HLProxy/VenueAdapter → Hyperliquid API
```

APEX orchestrator composes: Radar (screening) → Pulse (momentum) → Strategy → Guard (trailing stop) → REFLECT (review).

### VenueAdapter Pattern

`common/venue_adapter.py` defines the abstract interface. `adapters/hl_adapter.py` and `adapters/mock_adapter.py` wrap existing proxies without rewriting them. The engine talks only to the adapter interface.

### State Persistence

- Trade log: `data/cli/trades.jsonl` (append-only JSONL, never archived)
- APEX slot state: `data/apex/` JSON files
- REFLECT reports: `data/apex/reflect/`
- Archive: `data/archive/{YYYY-MM-DD}/`
- SQLite KV store: `parent/store.py` StateDB

### Config

YAML-based strategy config loaded via `cli/config.py`. Example configs in `configs/` and `quoting_engine/configs/`. Strategy registry in `cli/strategy_registry.py` maps short names to `module:class` paths.

## Key Conventions

- **Entry point**: `hl` CLI (registered in pyproject.toml as `cli.main:main`)
- **pythonpath**: Project root is on the Python path (configured in `[tool.pytest.ini_options]`)
- **Imports**: Use dotted paths from project root (e.g., `from modules.apex_engine import ApexEngine`, `from common.models import MarketSnapshot`)
- **Strategy registration**: Add to `cli/strategy_registry.py` STRATEGY_MAP dict; external strategies use `module.path:ClassName` syntax
- **Testing**: All tests in `tests/` (flat) and `tests/quoting_engine/`. Shared fixtures in `tests/conftest.py` provide `snapshot`, `context`, and `tmp_data_dir`. Tests are pure unit tests — no network calls, heavy mocking of HLProxy.
- **Credentials**: Pluggable backend system in `common/credentials.py` — env vars, keystore, or file-based. Resolution priority: env > keystore > file.
- **Builder fee**: All orders include a builder fee (10 bps default). Configured in `cli/builder_fee.py`.
