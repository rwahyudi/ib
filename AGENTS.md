# Repository Guidelines

## Project Structure & Module Organization

This repository contains the `ib` Python CLI for day-to-day Infoblox DNS work.
The main executable is the root-level `ib` script; do not create a new `src/`
layout unless the project is deliberately refactored.

- `ib` contains the Click command tree, Infoblox WAPI client, DNS workflows,
  Rich output helpers, shell completion, and cache handling.
- `tests/test_default_zone.py` contains the current regression suite for CLI
  behavior, DNS context output, completion, WAPI payloads, and zone/search logic.
- `README.md` is the user-facing command guide and should stay aligned with CLI
  examples and help text.
- `docs/` is for supporting user-facing documentation.
- `requirements.txt` lists runtime dependencies.

Keep implementation changes tightly scoped to the CLI behavior being changed.
Prefer small helper functions in `ib` over broad utility abstractions.

## Build, Test, and Development Commands

Run validation from the repository root.

- `env PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_default_zone.py`
  runs the regression suite without writing bytecode.
- `python3 -B -m py_compile ib` checks syntax for the executable.
- `env PYTHONDONTWRITEBYTECODE=1 ./ib --help` or a command-specific help check
  validates user-facing help output after CLI changes.
- `git diff --check` catches whitespace errors before finishing.

If validation creates `__pycache__/`, remove the generated files/directories
before handing work back.

## Coding Style & Naming Conventions

Use Python 3 style with clear function names, focused control flow, and type
annotations where the surrounding code already uses them. Keep the root `ib`
script executable.

The CLI uses Click for commands, options, arguments, and shell completion. Keep
existing command shapes stable, especially `ib configure`, `ib dns create`,
`ib dns search`, `ib dns delete`, and `ib dns zone ...` subcommands.

The terminal UI uses Rich. Keep output structured and readable, but avoid
unnecessary decoration. Shared display helpers such as DNS context rendering
should remain consistent across commands.

## CLI Behavior Guidelines

When changing a command, update every affected surface together:

- Click options, callbacks, and completion behavior.
- Rich help or context panels/lines.
- Error context and operator-facing messages.
- README examples and command descriptions.
- Regression tests in `tests/test_default_zone.py`.

Prefer explicit, actionable errors. Missing configuration should guide the user
to run `ib configure`. WAPI failures should preserve the Infoblox error while
adding local context when it helps explain the attempted command.

## Testing Guidelines

New behavior should include a focused regression test. Prefer tests that assert
the public CLI contract, generated WAPI payload, completion output, or rendered
Rich text instead of only testing private helpers.

Use manual command checks for help output or completion when the visual/user
surface changes. Keep these checks lightweight and avoid live Infoblox calls
unless the task explicitly requires them.

## Commit & Pull Request Guidelines

Use concise imperative commit messages, such as `Add dns create name shortcut`
or `Fix zone view network association fallback`.

Pull requests should include a short summary, the validation commands run,
linked issues when applicable, and screenshots or pasted terminal excerpts for
substantial CLI output changes.

## Security & Configuration Tips

Do not commit secrets, credentials, local configuration, generated caches, or
machine-specific files. The CLI stores private configuration under `~/.ib/`,
including `~/.ib/config`; tests and docs should not expose real Infoblox
servers, usernames, passwords, API tokens, or production DNS data.

Preserve strict local config permissions when touching configuration code:
`~/.ib/` should be `0700`, and config/key files should be `0600`.
