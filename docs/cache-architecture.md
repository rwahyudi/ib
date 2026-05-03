# Cache Architecture

`ib` caches DNS data to make repeated searches, record completions, and zone
completions fast without hiding Infoblox changes for long. The design is built
around one rule: cached record data is reused only when the zone SOA serial still
matches Infoblox.

![ib DNS cache flow](assets/cache-flow.svg)

## What Gets Cached

| Cache | Location | Scope | Expiry or validation |
| --- | --- | --- | --- |
| Zone completion names | `~/.ib/zone-completion-cache.json` | Active DNS view | 300 seconds |
| Zone serial metadata | `~/.ib/allrecords-cache/cache.sqlite3` | Server, WAPI version, DNS view | 30 seconds fresh, then 90 seconds stale-while-revalidate |
| Record search entries | `~/.ib/allrecords-cache/cache.sqlite3` | Server, WAPI version, DNS view, zone | SOA serial match |
| Prewarm lock | `~/.ib/allrecords-cache/prewarm.lock` | Local machine | Stale after 600 seconds |

The cache directory is private. `ib` creates `~/.ib` and
`~/.ib/allrecords-cache` with mode `0700`, and writes cache files with mode
`0600` where the platform allows it.

## Search And List Flow

When a command such as `ib dns search` or `ib dns list` needs records, `ib`
first resolves the active profile, Infoblox server, WAPI version, DNS view, and
zone scope. It then retrieves zone serial metadata.

Zone serial metadata is fresh for 30 seconds. If that short-lived cache is
fresh, `ib` can avoid re-querying the full zone list. From 31 through 120
seconds, `ib` serves the cached serial list immediately and starts a hidden
serial-only refresh so the next command can use updated serial metadata. If the
serial cache is missing, corrupt, or older than 120 seconds, `ib` queries
`zone_auth` with WAPI paging before continuing and writes the new serial
metadata back to SQLite.

For each zone, `ib` builds a cache key from:

- Infoblox server
- WAPI version
- DNS view
- normalized zone name

That key prevents cached records from crossing profile, server, view, or zone
boundaries.

The record cache is trusted only when the cached `zone_record_cache` serial
matches the current zone SOA serial. If the serial matches, `ib` searches local
SQLite `search_entries`. If the serial is different, missing, or unreadable,
`ib` fetches fresh `allrecords` from Infoblox, normalizes those records, and
replaces the per-zone cache rows.

## Search Entry Shape

Each cached record is normalized into fields that are cheap to search and easy
to render:

- record type
- zone
- display name
- display value
- comment
- original record JSON

Search uses the normalized `name`, `value`, and `comment` fields. The original
record JSON remains available so table, JSON, and CSV output can use the same
record rendering path as live WAPI results.

## Completion Flow

Zone-name completion has its own small JSON cache:

```text
~/.ib/zone-completion-cache.json
```

That file stores the active DNS view, creation timestamp, and zone names. It is
valid for 300 seconds. When completion runs, `ib` checks that the cached view
matches the current DNS view, then filters the zone names locally by the typed
prefix.

If completion cannot read config, connect to Infoblox, parse the cache, or
write the cache, it fails quietly and returns no candidates. This keeps shell
completion responsive and avoids printing tracebacks into the interactive shell.

Record-name completion for `ib dns delete` and `ib dns edit` uses the DNS search
cache. It can suggest records outside the active zone because it searches the
global forward-zone cache and then filters out reverse or unsupported record
types that should not be completed in the normal forward-delete and edit flows.

## Invalidation And Prewarming

Successful DNS mutations call the shared cache refresh path. That includes:

- record create
- record edit
- record delete
- zone create
- zone delete

The refresh path removes the allrecords cache directory and the zone completion
JSON file, then starts a detached hidden command:

```text
ib _prewarm-search-cache
```

The prewarmer reloads config, acquires `prewarm.lock`, scans authoritative zones,
and fills the SQLite search cache in the background. The foreground write command
does not wait for that warmup. If another prewarmer is already running, the new
one exits. If the lock file is older than 600 seconds, it is considered stale
and can be replaced.

## Failure Behavior

Cache failures are treated as performance misses, not command failures. If a
cache file is missing, corrupt, locked, expired, or not writable, `ib` falls back
to live WAPI queries when the foreground command needs data.

The main exception is shell completion: completion is intentionally best-effort.
If completion cannot safely use cached data or fetch fresh data, it returns an
empty candidate list so the shell remains clean.

## Operational Notes

- The SQLite cache is the current cache format.
- Older legacy JSON allrecords cache files are still readable and are migrated
  into SQLite when they match the current SOA serial.
- Secondary zones are skipped during global search and warmup.
- Cold searches and background warmup process multiple zones concurrently, up to
  the configured worker limit.
- Zone serial metadata can lag by up to 120 seconds by design during the
  stale-while-revalidate window; record data is still validated against the SOA
  serial supplied by that serial metadata before reuse.
