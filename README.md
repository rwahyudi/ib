# ib

`ib` is a small command-line client for day-to-day Infoblox DNS work.

It talks to Infoblox WAPI over HTTPS, stores configuration in `~/.ib/config`,
encrypts the saved password with a local key, and uses Rich tables/panels for
readable terminal output.

## Features

- Configure multiple Infoblox profiles with one default profile.
- Create and edit A, AAAA, CNAME, TXT, MX, PTR, and HOST records.
- Search DNS records by name, value, or comment.
- Search the active zone plus child authoritative zones, or search globally with `-g`.
- Cache `allrecords` data in SQLite and refresh by SOA serial number.
- List, create, delete, use, and inspect authoritative DNS zones.
- View zone SOA settings including serial, MNAME, RNAME, refresh, retry,
  expiry, and negative caching TTL.
- Print copy-ready shell completion setup for Bash, Zsh, and Fish.

## Install

Install with `pipx` so `ib` and its Python dependencies stay isolated from the
system Python packages. Python 3.9 or newer is required:

```bash
pipx install git+https://github.com/rwahyudi/ib.git
pipx ensurepath
```

Upgrade later with:

```bash
(cd /tmp && pipx upgrade ib)
```

Running the upgrade from a neutral directory avoids `pipx` treating a local
`./ib` checkout or executable as a path instead of the installed package name.

If `pipx` is unavailable, use the Python user install path:

```bash
python3 -m pip install --user git+https://github.com/rwahyudi/ib.git
python3 -m pip install --user --upgrade git+https://github.com/rwahyudi/ib.git
```

For development from a local checkout, run from the repository root:

```bash
python3 -m pip install -r requirements.txt
mkdir -p ~/.local/bin
install -m 755 ./ib ~/.local/bin/ib
export PATH="$HOME/.local/bin:$PATH"
```

Then create an Infoblox profile:

```bash
ib config new --default
```

`ib config new [PROFILE]` prompts for a profile name when it is not supplied,
then asks for:

- Infoblox server
- Username and password
- WAPI version
- SSL verification preference
- DNS view
- Default DNS zone

When a new profile is created, `ib config new` connects to Infoblox with the
entered credentials and lists available DNS views so you can select one. If the
DNS view lookup fails, the command falls back to manual DNS view entry and still
saves the profile.

Profile setup also asks whether to configure a default DNS zone, with yes as the
default answer. If you choose yes, it loads forward zones from the selected DNS
view, including subdomain zones, and shows a live search box where the zone list
filters as you type. Reverse zones are excluded from selection. This picker is
used by `ib config new` and `ib config edit`.

Manage multiple profiles with:

```bash
ib config
ib config new prod --default
ib config new
ib config new lab
ib config list
ib config use prod
ib config edit lab
ib config delete lab
```

Bare `ib config` is read-only: it lists available profiles and shows profile
management usage. Use `ib config edit [profile]` to update an existing profile.
Saved values are shown as defaults during edits, pressing Enter keeps the
current value, and leaving the password prompt blank keeps the current password.

Private files are written under `~/.ib/`. Profiles are stored in
`~/.ib/config` as `[profile:<name>]` sections with `[meta] default_profile`
selecting the default. The config directory is kept at `0700`; the config and
key files are kept at `0600`.

## Usage

Common workflow:

```bash
ib config new --default
ib dns view list
ib dns view use "DNS Zone View"
ib dns zone list
ib dns zone use example.com
ib dns list
ib dns search app
ib dns create host app 192.0.2.10 -t 300 -c "Application host"
ib dns edit app host 192.0.2.20 -t 300 -c "Application host"
ib dns delete app
```

Use the global `-o/--output` option to produce structured output. It can be
placed at the root, on a command group, or after command arguments. Omitting it
keeps the current Rich table output. `-o jq` prints JSON for use with `jq`, and
`-o csv` prints comma-separated values. Structured output omits Infoblox
reference IDs:

```bash
ib -o jq dns search app
ib dns -o jq search app
ib dns search app -o jq
ib -o csv dns view list
ib -o csv dns zone list
```

Help always stays in normal help format, even when `-o/--output` is present:

```bash
ib -o jq dns edit -h
```

Most commands use the configured default profile and the active DNS view.
The active view comes from `ib dns view use`, then `IB_VIEW`, then the DNS view
saved in the selected profile. Create and edit commands resolve the target zone
from `--zone`, then the current profile's shell session, then `IB_ZONE`, then
the default zone saved in the selected profile. Delete commands accept an
optional positional zone, such as `ib dns delete app example.com`; when that is
omitted, they try a fully qualified record name first, then the same
active/default zone fallback.

## DNS Context

Most DNS commands use the current default profile, DNS view, and active zone.
Help output and scoped search output show a compact `Current DNS Context` line.

Profile selection is persistent:

```bash
ib config list
ib config use prod
```

Active view precedence is:

1. Current shell session from `ib dns view use <view>`
2. Environment variable `IB_VIEW`
3. Configured DNS view from the selected profile

Set a view only for the current shell session:

```bash
ib dns view use "DNS Zone View"
```

Set an environment override:

```bash
export IB_VIEW="DNS Zone View"
```

Active zone precedence is:

1. Explicit command target, such as `--zone example.com` for create/edit or
   `ib dns delete app example.com` for delete
2. Current profile's shell session from `ib dns zone use <zone>`
3. Environment variable `IB_ZONE`
4. Configured default zone from the selected profile

Set a zone only for the current shell session:

```bash
ib dns zone use test.local
```

This also starts a silent background prewarm for searches under that zone and
its child authoritative zones.

A new shell session falls back to `IB_ZONE` or the configured default zone.
Switching profiles also falls back to that profile's default zone unless you
set an active zone for that profile.

Set an environment override:

```bash
export IB_ZONE=test.local
```

## Create Records

The command shape is `ib dns create {a|aaaa|cname|host|mx|ptr|srv|txt} NAME VALUE`.
Record names are relative to the active zone unless already fully qualified.
Use `@` as the name for the zone apex. Use `-t` or `--ttl` for record TTL,
and `-c` or `--comment` for a plain ASCII comment.

For forward records, a fully qualified name can select its zone
automatically. For example, if `example-dns.com` exists as a forward zone,
`host1.example-dns.com` uses `example-dns.com`; if no forward zone
matches, `ib` falls back to the active/default zone.

Assuming the active zone is `example.com`, these commands produce:

| Command | Record produced |
| --- | --- |
| `ib dns create host app 192.0.2.10 -t 300 -c "Application host"` | `HOST app.example.com` with IPv4 address `192.0.2.10`, TTL `300`, comment `Application host` |
| `ib dns create host host1.example-dns.com 192.0.2.10` | `HOST host1.example-dns.com` with IPv4 address `192.0.2.10` in zone `example-dns.com` when that forward zone exists |
| `ib dns create aaaa app 2001:db8::10 --ttl 300` | `AAAA app.example.com -> 2001:db8::10`, TTL `300` |
| `ib dns create cname www app.example.com` | `CNAME www.example.com -> app.example.com` |
| `ib dns create txt _spf "v=spf1 include:example.net -all"` | `TXT _spf.example.com = "v=spf1 include:example.net -all"` |
| `ib dns create mx @ "10 mail.example.com"` | `MX example.com -> mail.example.com` with preference `10` |
| `ib dns create srv _sip._tcp "10 20 5060 sip.example.com"` | `SRV _sip._tcp.example.com -> sip.example.com` with priority `10`, weight `20`, and port `5060` |
| `ib dns create ptr 192.0.2.10 host.example.com` | `PTR 192.0.2.10 -> host.example.com` |

CNAME creates check whether the target resolves from the local system before
submitting the record. If the target does not resolve, `ib` prints a warning
and continues with the create request.

If Infoblox returns `The IP address ... cannot be used for the zone ...`,
the selected forward zone does not allow that IP based on its network
association. Use an IP associated with that zone, choose the correct zone with
`--zone` or a fully qualified name, or update the zone network association
in Infoblox. The error details instruct the client to run
`ib dns zone info <zone>` to view the network association for the selected zone.

Use `--zone` to bypass the active zone for one command:

```bash
ib dns create host app 192.0.2.10 --zone example.com
```

For A/AAAA workflows, add `--noptr` when you do not want PTR handling:

```bash
ib dns create aaaa app 2001:db8::10 --noptr
```

## Edit Records

The command shape is `ib dns edit NAME [TYPE] [VALUE]`. The `NAME` argument
supports shell completion when completion is enabled. `TYPE` completion only
suggests the selected record's current type, and a supplied type must match the
existing record. Use `TYPE VALUE` to update the record value, or omit both when
changing only `-t/--ttl` or `-c/--comment`.

`ib dns edit` cannot change a record's type because Infoblox does not support
changing the WAPI object type of an existing record. Delete and recreate the
record when the type must change.

```bash
ib dns edit app host 192.0.2.20 -t 300 -c "Application host"
ib dns edit app -t 300 -c "Application host"
ib dns edit www cname app.example.com
ib dns edit app.example.com a 192.0.2.20
ib dns edit _sip._tcp srv "10 20 5061 sip2.example.com"
```

If a name matches multiple records, `ib` prints the ambiguous matches instead of
updating the wrong record.

## Search Records

Search by record name, value, or comment:

```bash
ib dns search app
```

Normal search uses the active/default zone as the root and includes child
authoritative zones. Use `-z/--zone` to search a different zone scope for one
command, and `-v/--view` to search a different DNS view. If no active/default
zone is set, search uses all non-secondary zones in the selected DNS view.
Search uses exact substring matching by default.
PTR entries index both the target hostname and IP address, so a global search
can find reverse records by normal IP address when the reverse zone is in scope.
PTR results display the normal IP address in the Name column even when Infoblox
returns a relative reverse name such as `21` under `10.0.0.0/24`.

Search a specific zone and its child authoritative zones:

```bash
ib dns search app -z test.local
```

Search in a specific DNS view:

```bash
ib dns search app -v "DNS Zone View"
```

When shell completion is enabled, `-z/--zone` completes zone names and
`-v/--view` completes DNS view names.

Search across the whole selected view explicitly:

```bash
ib dns search -g app
ib dns search -g 192.0.2.10
```

Combine `-g` with `-v/--view` to search the whole selected view:

```bash
ib dns search -g -v "DNS Zone View" app
```

Use case-sensitive matching:

```bash
ib dns search -i App
```

Enable fuzzy matching for typo-tolerant results:

```bash
ib dns search app -f
```

With `-f`, close matches can be returned when a record name, value, or comment is
near the keyword.

Filter by one or more record types:

```bash
ib dns search app --type a,host,cname
ib dns search app -t txt
```

When shell completion is enabled, `-t/--type` completes supported record types
and can complete the current value in a comma-separated list.

Exclude records matching one or more keywords:

```bash
ib dns search app -e old -e test
```

Exclusions use the same record name, value, and comment fields as the search
keyword. They follow `-i` when case-sensitive matching is enabled.

### Performance Architecture

Search and record completion use a local cache so repeated DNS operations do not
need to query Infoblox for every request. Results are based on Infoblox
`allrecords`, normalized into searchable `name`, `value`, and `comment` fields,
and stored in SQLite. PTR search values include both the target hostname and IP
address while table and structured output still display the target hostname.

| Cache | Location | Freshness rule |
| --- | --- | --- |
| Zone completion names | `~/.ib/zone-completion-cache.json` | 300 seconds fresh, then 48 hours stale-while-revalidate, scoped to the active DNS view |
| Zone serial metadata | `~/.ib/allrecords-cache/cache.sqlite3` | 30 seconds fresh, then 300 seconds stale-while-revalidate |
| Record search entries | `~/.ib/allrecords-cache/cache.sqlite3` | reused only when the zone SOA serial matches |
| Prewarm lock | `~/.ib/allrecords-cache/prewarm.lock` | prevents duplicate warmers; stale after 600 seconds; uncached search polls every 200 ms while active |

Each record-cache key includes the Infoblox server, WAPI version, DNS view, and
zone name, so cached data does not cross profiles, views, or zones. When a zone
serial matches, `ib` searches local SQLite even if the cached record rows are
old; record rows have no separate time-based expiry. Background prewarm refreshes
the row `updated_at` timestamp when it validates that cached rows still match the
current serial. When the serial changes or the cache is missing, `ib` fetches
fresh `allrecords` with WAPI paging and rewrites that zone's cached rows. If a
background prewarm is already running, search serves existing cached rows,
including stale rows, immediately. For an uncached zone, search waits for the
warmer to finish, polling every 200 ms, retries the cache, and only then fetches
live `allrecords` if the cache is still missing.

`ib <TAB><TAB>` starts a silent background warm of the global DNS search cache.
`ib dns zone use <zone>` starts a silent scoped warm for that zone and its child
authoritative zones. Warmers use the same zone serial stale-while-revalidate
path as foreground search, so a hidden zone metadata refresh starts only when
the cached serial metadata is stale enough to revalidate. While that hidden
refresh is running, new requests keep serving the existing cached zone list
immediately. If the hidden refresh finds changed SOA serials, it takes the record
prewarm lock before publishing the newer serial metadata and refreshes only the
changed zones' record caches in the background. Successful DNS record and zone
updates refresh the specific changed zone: they remove that zone's record-search
rows, remove the current server/view zone serial metadata, and start a scoped
silent prewarm. Cache failures are treated as performance misses: foreground
commands fall back to live WAPI calls, while shell completion fails quietly.
Zone-name completion can serve stale cached names for 48 hours after the
300-second fresh window while a hidden refresh updates the cache.

For the full performance flow, parallel worker model, and cache diagram, see
[Performance architecture](docs/performance-architecture.md).

## List Records

```bash
ib dns list
ib dns list example.com
```

The optional zone argument supports shell completion when completion is enabled.
The command retrieves `allrecords` with WAPI paging and refreshes cached zone
records when the SOA serial number changes.

## Delete Records

Delete A, AAAA, CNAME, TXT, MX, and HOST records by record name. When the zone
argument is omitted, a fully qualified record name is tried first, then the
active/default zone fallback is used. Tab completion for the record name uses
the global DNS search cache, so `ib dns delete <TAB>` can suggest records
outside the active zone. PTR records use the dedicated reverse form; NS, SOA,
PTR, reverse-zone, and unsupported `allrecords` entries are excluded from normal
delete completion. The optional zone completion only suggests forward zones.

```bash
ib dns delete app
ib dns delete app.other.example.net
ib dns delete app example.com
```

Delete reverse PTR records with the dedicated PTR command and a full IP
address. `ib` looks up the most specific reverse zone for the IP address before
deleting the PTR record.

```bash
ib dns delete ptr 192.168.1.3
```

If a name matches multiple records, `ib` prints the ambiguous matches instead of
deleting the wrong record.

## Zone Commands

List zones in the active view:

```bash
ib dns zone list
ib dns zone list test
```

Show one zone, including SOA settings:

```bash
ib dns zone info example.com
```

Create and delete authoritative zones. `--format` accepts `forward`, `ipv4`, or
`ipv6` case-insensitively and defaults to `FORWARD`; `--comment` and
`--ns-group` are optional.

```bash
ib dns zone create test.local --comment "Lab zone" --ns-group default
ib dns zone create 2.0.192.in-addr.arpa --format IPV4
ib dns zone delete test.local
```

Set the active zone for the current shell:

```bash
ib dns zone use test.local
```

This command also starts a silent background prewarm for search and record
completion under the selected zone.

Existing zone names support shell completion for `ib dns zone info` and
`ib dns zone use` when completion is enabled.

## Completion

```bash
ib completion
ib completion bash
ib completion zsh
ib completion fish
```

For Bash in the current shell:

```bash
eval "$(_IB_COMPLETE=bash_source ib)"
```

Bash completion shows a Rich-colored candidate table by default while inserted
completion values remain plain text. Set `IB_COMPLETION_LAYOUT=plain` to use
plain Bash completion, `IB_COMPLETION_COLOR=always` to force color, or
`IB_COMPLETION_COLOR=never` to disable color.

For persistent Bash completion:

```bash
_IB_COMPLETE=bash_source ib > ~/.ib-complete.bash
printf '\n# ib completion\n. ~/.ib-complete.bash\n' >> ~/.bashrc
exec bash
```

## Troubleshooting

Run `ib config new <profile>` if you see a missing configuration or credential
error. Use `ib config` or `ib config list` to inspect profiles, and
`ib config use <profile>` to switch the default profile.

Use `--zone` with `ib dns create`, or pass the optional positional zone to
`ib dns delete`, when the active zone is not the target zone.

Use `ib dns search -g <keyword>` when you need to search outside the active zone
and its child zones. Use `ib dns search -z <zone> <keyword>` or
`ib dns search -v <view> <keyword>` for one-off search scope changes.
Use `ib dns search -g <ip-address>` to find cached PTR entries by address.

If search results look stale, confirm the zone SOA serial changed:

```bash
ib dns zone info example.com
```

The record cache refreshes from Infoblox when the serial number changes.
Successful record or zone updates clear the DNS caches and start a silent
background cache warm. Zone serial metadata is fresh for 30 seconds, then may
lag for up to 300 more seconds while `ib` serves the cached serial list and
refreshes it in the background.
