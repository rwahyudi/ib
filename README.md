# ib

`ib` is a small command-line client for day-to-day Infoblox DNS work.

It talks to Infoblox WAPI over HTTPS, stores configuration in `~/.ib/config`,
encrypts the saved password with a local key, and uses Rich tables/panels for
readable terminal output.

## Features

- Configure server, credentials, DNS view, and default zone with `ib configure`.
- Create and edit A, AAAA, CNAME, TXT, MX, PTR, and HOST records.
- Search DNS records by name, value, or comment.
- Search the active zone plus child authoritative zones, or search globally with `-g`.
- Cache `allrecords` data in SQLite and refresh by SOA serial number.
- List, create, delete, use, and inspect authoritative DNS zones.
- View zone SOA settings including serial, MNAME, RNAME, refresh, retry,
  expiry, and negative caching TTL.
- Print copy-ready shell completion setup for Bash, Zsh, and Fish.

## Install

Run from the repository root:

```bash
python3 -m pip install -r requirements.txt
mkdir -p ~/.local/bin
install -m 755 ./ib ~/.local/bin/ib
export PATH="$HOME/.local/bin:$PATH"
```

Then configure Infoblox access:

```bash
ib configure
```

`ib configure` prompts for:

- Infoblox server
- Username and password
- WAPI version
- DNS view
- Default DNS zone
- SSL verification preference

You can run `ib configure` multiple times. It will not wipe existing values just
because you rerun it: saved values are shown as defaults, pressing Enter keeps
the current value, and leaving the password prompt blank keeps the current
password.

Private files are written under `~/.ib/`. The config directory is kept at
`0700`; the config and key files are kept at `0600`.

## Usage

Start with the built-in help when you want the exact command shape:

```bash
ib --help
ib dns --help
ib dns create --help
ib dns zone --help
```

Common workflow:

```bash
ib configure
ib dns zone list
ib dns zone use example.com
ib dns list
ib dns search app
ib dns create host app 192.0.2.10 -t 300 -c "Application host"
ib dns edit app host 192.0.2.20 -t 300 -c "Application host"
ib dns delete app
```

Command overview:

- `ib configure` creates or updates Infoblox connection settings.
- `ib completion [bash|zsh|fish]` prints shell completion setup instructions.
- `ib dns list [zone]` lists all DNS records in the active or specified zone.
- `ib dns create <a|aaaa|cname|host|mx|ptr|txt> <name> <value>` creates DNS
  records.
- `ib dns edit <name> [a|aaaa|cname|host|mx|ptr|txt] [value]` updates one
  existing DNS record.
- `ib dns search [-i] [-g] <keyword>` searches records by name, value, or comment.
- `ib dns delete <record-name> [zone]` deletes a single matching A, AAAA,
  CNAME, TXT, MX, or HOST record.
- `ib dns delete ptr <ip-address>` deletes a reverse DNS PTR record by full IP address.
- `ib dns zone list [search]` lists authoritative zones in the configured view.
- `ib dns zone view <zone>` shows zone details, network associations, and SOA settings.
- `ib dns zone create <zone> [--format forward|ipv4|ipv6] [--comment TEXT]
  [--ns-group TEXT]` creates a zone.
- `ib dns zone delete <zone>` deletes a zone.
- `ib dns zone use <zone>` sets the active zone for the current shell session.

Most commands use the configured DNS view. Create and edit commands resolve the
target zone from `--zone`, then the current shell session, then `IB_ZONE`, then
the default zone saved by `ib configure`. Delete commands accept an optional
positional zone, such as `ib dns delete app example.com`; when that is omitted,
they try a fully qualified record name first, then the same active/default zone
fallback.

## DNS Context

Most DNS commands use the current DNS view and active zone. Help output and
scoped search output show a compact `Current DNS Context` line.

Active zone precedence is:

1. Explicit command target, such as `--zone example.com` for create/edit or
   `ib dns delete app example.com` for delete
2. Current shell session from `ib dns zone use <zone>`
3. Environment variable `IB_ZONE`
4. Configured default zone from `ib configure`

Set a zone only for the current shell session:

```bash
ib dns zone use test.local
```

A new shell session falls back to `IB_ZONE` or the configured default zone.

Set an environment override:

```bash
export IB_ZONE=test.local
```

## Create Records

The command shape is `ib dns create {a|aaaa|cname|host|mx|ptr|txt} NAME VALUE`.
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
| `ib dns create ptr 192.0.2.10 host.example.com` | `PTR 192.0.2.10 -> host.example.com` |

CNAME creates check whether the target resolves from the local system before
submitting the record. If the target does not resolve, `ib` prints a warning
and continues with the create request.

If Infoblox returns `The IP address ... cannot be used for the zone ...`,
the selected forward zone does not allow that IP based on its network
association. Use an IP associated with that zone, choose the correct zone with
`--zone` or a fully qualified name, or update the zone network association
in Infoblox. The error details instruct the client to run
`ib dns zone view <zone>` to view the network association for the selected zone.

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
```

If a name matches multiple records, `ib` prints the ambiguous matches instead of
updating the wrong record.

## Search Records

Search by record name, value, or comment:

```bash
ib dns search app
```

Normal search uses the active/default zone as the root and includes child
authoritative zones. If no active/default zone is set, search uses all
non-secondary zones in the configured DNS view.

Search across the whole configured view explicitly:

```bash
ib dns search -g app
```

Use case-sensitive matching:

```bash
ib dns search -i App
```

Search performance notes:

- Secondary zones are skipped.
- Results are based on Infoblox `allrecords`.
- The SQLite cache lives at `~/.ib/allrecords-cache/cache.sqlite3`.
- `ib <TAB><TAB>` starts a silent background warm of the global DNS search cache.
- Zone serial lists are cached for 30 seconds.
- A zone's records are refreshed only when its SOA serial number changes.
- Cached records include normalized searchable fields, so repeated searches are faster.
- Cold or refreshed searches process zones with 8 workers by default.
- Successful DNS record or zone updates clear the DNS caches and start a silent
  background cache warm.

## List Records

List every record in the active zone:

```bash
ib dns list
```

List every record in a specific zone:

```bash
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

List zones in the configured view:

```bash
ib dns zone list
ib dns zone list test
```

View one zone, including SOA settings:

```bash
ib dns zone view example.com
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

Existing zone names support shell completion for `ib dns zone view` and
`ib dns zone use` when completion is enabled.

## Completion

Print setup instructions:

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

For persistent Bash completion:

```bash
_IB_COMPLETE=bash_source ib > ~/.ib-complete.bash
printf '\n# ib completion\n. ~/.ib-complete.bash\n' >> ~/.bashrc
exec bash
```

Verify:

```bash
ib <TAB><TAB>
ib dns create <TAB><TAB>
ib dns edit <TAB><TAB>
ib dns list <TAB><TAB>
ib dns delete <TAB><TAB>
ib dns zone view <TAB><TAB>
```

## Troubleshooting

Run `ib configure` if you see a missing configuration or credential error.

Check the active context:

```bash
ib --help
ib dns --help
```

Use `--zone` with `ib dns create`, or pass the optional positional zone to
`ib dns delete`, when the active zone is not the target zone.

Use `ib dns search -g <keyword>` when you need to search outside the active zone
and its child zones.

If search results look stale, confirm the zone SOA serial changed:

```bash
ib dns zone view example.com
```

The record cache refreshes from Infoblox when the serial number changes.
Successful record or zone updates clear the DNS caches and start a silent
background cache warm. Zone serial metadata can otherwise lag by up to 30
seconds because it is cached briefly for faster repeated searches.
