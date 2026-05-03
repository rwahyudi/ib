# Performance Architecture

`ib` is designed to make repeated DNS work feel fast even when Infoblox has many
authoritative zones and records. The speed comes from combining scoped caching,
SOA serial validation, zone-level parallelism, and background warming.

The important rule is: `ib` can avoid live `allrecords` calls only when it has a
safe reason to trust local data. Record data is reused when the cached zone
serial matches the active SOA serial metadata. When the cache is absent or no
longer valid, `ib` fetches fresh data and rebuilds the local SQLite rows.

![ib DNS cache flow](assets/cache-flow.svg)

## Why Search Is Fast

Search and list commands avoid doing one large, repeated live scan whenever
possible:

1. Resolve the active profile, DNS view, and zone scope.
2. Read short-lived zone serial metadata.
3. Split work by authoritative zone.
4. Process multiple zones concurrently when more than one zone is in scope.
5. For each zone, search local SQLite if the cached serial still matches.
6. Fetch live `allrecords` only for zones that miss cache validation.
7. Sort and deduplicate records once all zone workers finish.

This keeps the common hot path local: SQLite lookup, normalized exact field
matching, optional fuzzy matching, and table/JSON/CSV rendering. Live WAPI calls
are reserved for cold cache, expired serial metadata, changed zones, or explicit
cache refresh paths.

## Parallel Zone Workers

`ib` parallelizes by zone, not by individual DNS record. A search first builds
the list of zones to inspect. If there is more than one zone, it starts a
`ThreadPoolExecutor` with this limit:

```text
min(DNS_SEARCH_WORKERS, number_of_zones)
```

`DNS_SEARCH_WORKERS` is currently `8`, so a search across 20 zones can have up to
8 zones being processed at the same time. As each worker finishes, the executor
assigns it another zone until the zone list is complete.

Each worker gets its own cloned Infoblox client. That avoids sharing one HTTPS
connection object across threads while keeping the same server, WAPI version,
credentials, DNS view, timeout, and SSL settings.

Threads help here because the expensive parts are I/O-bound: waiting for WAPI,
reading SQLite, and writing refreshed cache rows. While one worker is waiting on
Infoblox, another worker can continue processing a different zone. After the
workers return their matches, the main thread sorts records and removes
duplicates so output remains stable.

Background prewarm uses the same zone-level worker model. The hidden
`ib _prewarm-search-cache` command scans the global forward-zone set and warms
each zone cache concurrently, also capped by `DNS_SEARCH_WORKERS`.

## Cache Layers

| Cache | Location | Scope | Freshness rule |
| --- | --- | --- | --- |
| Zone completion names | `~/.ib/zone-completion-cache.json` | Active DNS view | 300 seconds |
| Zone serial metadata | `~/.ib/allrecords-cache/cache.sqlite3` | Server, WAPI version, DNS view | 30 seconds fresh, then 90 seconds stale-while-revalidate |
| Record search entries | `~/.ib/allrecords-cache/cache.sqlite3` | Server, WAPI version, DNS view, zone | SOA serial match |
| Prewarm lock | `~/.ib/allrecords-cache/prewarm.lock` | Local machine | Stale after 600 seconds |

The cache directory is private. `ib` creates `~/.ib` and
`~/.ib/allrecords-cache` with mode `0700`, and writes cache files with mode
`0600` where the platform allows it.

Cache keys include the Infoblox server, WAPI version, DNS view, and normalized
zone name where applicable. That prevents cached data from crossing profiles,
views, or zones.

## Serial Freshness

Zone serial metadata is fresh for 30 seconds. During that fresh window, `ib`
avoids querying the full zone list again.

From 31 through 120 seconds, the serial list enters stale-while-revalidate.
Foreground commands keep using the cached serial list immediately and start a
hidden serial-only refresh:

```text
ib _refresh-zone-serial-cache
```

That hidden refresh updates only serial metadata. It does not rebuild record
caches and does not use the 8-worker search pool because the serial refresh is a
single zone-list query.

If the serial cache is missing, corrupt, or older than 120 seconds, `ib` refreshes
serial metadata synchronously before continuing. This prevents very old serial
state from driving record-cache decisions.

## Record Cache Validation

Record cache rows are stored as normalized search entries:

- record type
- zone
- display name
- display value
- comment
- original record JSON

Search uses the normalized `name`, `value`, and `comment` fields. By default it
checks exact substring matches. When the user passes `-f`, search also checks
typo-tolerant fuzzy matches. The original record JSON remains available so
table, JSON, and CSV output can use the same rendering path as live WAPI
results.

For a zone, cached records are trusted only when the stored `zone_record_cache`
serial matches the current serial metadata for that zone. If the serial matches,
`ib` searches local SQLite. If it differs, is missing, or cannot be read, `ib`
fetches fresh `allrecords` with WAPI paging, normalizes the records, and replaces
that zone's cached rows.

## Completion Performance

Zone-name completion uses the small JSON cache and filters zone names locally by
the typed prefix. If completion cannot read config, connect to Infoblox, parse
the cache, or write the cache, it returns no candidates instead of printing
errors into the shell.

Record-name completion for `ib dns delete` and `ib dns edit` uses the DNS search
cache. It can suggest records outside the active zone because it searches the
global forward-zone cache and filters out reverse or unsupported record types
that should not be completed in normal forward delete and edit flows.

## Invalidation And Prewarm

Successful DNS mutations call the shared cache refresh path. That includes:

- record create
- record edit
- record delete
- zone create
- zone delete

The refresh path removes the allrecords cache directory and the zone completion
JSON file, then starts detached background prewarm. Foreground write commands do
not wait for prewarm to finish.

The prewarmer uses `prewarm.lock` to avoid duplicate global warmers. If another
prewarmer is already running, the new one exits. If the lock is older than 600
seconds, it is considered stale and can be replaced.

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
