# Repository Guidelines

## Project Structure & Module Organization

This repository is currently a starter workspace. It contains only agent/tooling directories (`.agents/`, `.codex/`) and an empty `.git/` placeholder, with no application source yet.

When adding implementation code, keep the layout predictable:

- `src/` for application or library source.
- `tests/` for automated tests mirroring `src/` structure.
- `assets/` for static files such as images, fixtures, or sample data.
- `docs/` for user-facing or architecture documentation.
- `scripts/` for repeatable local maintenance tasks.

Prefer small, purpose-specific modules over broad utility files.

## Build, Test, and Development Commands

No build system or test runner is currently checked in. Add the relevant commands here when tooling is introduced, and keep them runnable from the repository root.

Expected examples once tooling exists:

- `npm test`, `pytest`, or `go test ./...` to run the full test suite.
- `npm run lint`, `ruff check .`, or equivalent to run style checks.
- `npm run dev`, `make dev`, or equivalent to start a local development server.

If you add a `Makefile`, package script, or task runner, prefer short command names that wrap longer invocations.

## Coding Style & Naming Conventions

Follow the formatter and linter for the language introduced. Until those tools exist, use consistent indentation, descriptive names, and focused files.

Use lowercase, hyphenated names for shell scripts and generated artifacts, for example `scripts/import-data.sh`. Use language-native naming for source files and symbols.

## Testing Guidelines

Place tests under `tests/` unless the chosen framework has a stronger convention. Name tests after behavior, not only the function name. For example, prefer `test_rejects_invalid_config` over `test_config`.

New behavior should include a regression test or a documented manual verification step when automation is not practical.

## Commit & Pull Request Guidelines

No readable Git history is available in this workspace, so no existing commit convention can be inferred. Until a project convention is established, use concise imperative commits such as `Add config loader` or `Fix report sorting`.

Pull requests should include a short summary, the commands or manual checks run, linked issues when applicable, and screenshots for UI changes.

## Security & Configuration Tips

Do not commit secrets, local credentials, or machine-specific configuration. Use ignored local files such as `.env.local` for private settings, and document required environment variables in `docs/` or a sample config file.
