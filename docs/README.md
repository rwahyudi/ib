# Docs

This repo is intentionally tiny: one executable, one requirements file, package
metadata for installation, and one job.

`ib` wraps Infoblox DNS tasks with friendly prompts, Rich tables, tab completion,
encrypted local config, and clear errors when setup is missing.

Start at the project README, then run:

```bash
ib --help
ib dns --help
ib dns zone --help
```

## Guides

- [Cache architecture](cache-architecture.md) explains the completion cache,
  SQLite record cache, SOA serial validation, and background prewarm flow.
