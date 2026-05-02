# ib

`ib` is a small command-line client for day-to-day Infoblox DNS work.

It talks to Infoblox WAPI over HTTPS, stores configuration in `~/.ib/config`,
encrypts the saved password with a local key, and uses Rich tables/panels for
readable terminal output.

## Features

- Configure server, credentials, DNS view, and default zone with `ib configure`.
- Create A, AAAA, CNAME, TXT, MX, PTR, and HOST records.
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
ib dns search app
ib dns create a -n app 192.0.2.10 -t 300 -c "Application VIP"
ib dns delete app
```

Command overview:

- `ib configure` creates or updates Infoblox connection settings.
- `ib completion [bash|zsh|fish]` prints shell completion setup instructions.
- `ib dns create <type> -n|--name <name> <value>` creates DNS records.
- `ib dns search [-g] [-i] <keyword>` searches records by name, value, or comment.
- `ib dns delete <record-name> [zone]` deletes a single matching DNS record.
- `ib dns zone list [keyword]` lists authoritative zones in the configured view.
- `ib dns zone view <zone>` shows zone details and SOA settings.
- `ib dns zone create <zone> [--format FORWARD|IPV4|IPV6]` creates a zone.
- `ib dns zone delete <zone>` deletes a zone.
- `ib dns zone use <zone>` sets the active zone for the current shell session.

Most commands use the configured DNS view. Record commands resolve the target
zone from `--zone`, then the current shell session, then `IB_ZONE`, then the
default zone saved by `ib configure`.

## DNS Context

Most DNS commands use the current DNS view and active zone. Help output and
scoped search output show a compact `Current DNS Context` line.

Active zone precedence is:

1. Explicit command option, such as `--zone example.com`
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

Record names are relative to the active zone unless already fully qualified.
Use `-t` or `--ttl` for record TTL, and `-c` or `--comment` for a plain ASCII
comment.

For forward records, a fully qualified `-n` or `--name` can select its zone
automatically. For example, if `example-dns.com` exists as a forward zone,
`-n host1.example-dns.com` uses `example-dns.com`; if no forward zone
matches, `ib` falls back to the active/default zone.

Assuming the active zone is `example.com`, these commands produce:

| Command | Record produced |
| --- | --- |
| `ib dns create a -n app 192.0.2.10 -t 300 -c "Application VIP"` | `A app.example.com -> 192.0.2.10`, TTL `300`, comment `Application VIP` |
| `ib dns create a -n host1.example-dns.com 192.0.2.10` | `A host1.example-dns.com -> 192.0.2.10` in zone `example-dns.com` when that forward zone exists |
| `ib dns create host -n app 192.0.2.10 -t 300 -c "Application host"` | `HOST app.example.com` with IPv4 address `192.0.2.10`, TTL `300`, comment `Application host` |
| `ib dns create aaaa -n app 2001:db8::10 --ttl 300` | `AAAA app.example.com -> 2001:db8::10`, TTL `300` |
| `ib dns create cname -n www app.example.com` | `CNAME www.example.com -> app.example.com` |
| `ib dns create txt -n _spf "v=spf1 include:example.net -all"` | `TXT _spf.example.com = "v=spf1 include:example.net -all"` |
| `ib dns create mx -n @ "10 mail.example.com"` | `MX example.com -> mail.example.com` with preference `10` |
| `ib dns create ptr -n 192.0.2.10 host.example.com` | `PTR 192.0.2.10 -> host.example.com` |

If Infoblox returns `The IP address ... cannot be used for the zone ...`,
the selected forward zone does not allow that IP based on its network
association. Use an IP associated with that zone, choose the correct zone with
`--zone` or a fully qualified `-n` name, or update the zone network association
in Infoblox.

Use `--zone` to bypass the active zone for one command:

```bash
ib dns create a -n app 192.0.2.10 --zone example.com
```

For A/AAAA workflows, add `--noptr` when you do not want PTR handling:

```bash
ib dns create a -n app 192.0.2.10 --noptr
```

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

## Delete Records

Delete by record name. The optional zone argument uses the same active-zone
fallback rules as create.

```bash
ib dns delete app
ib dns delete app example.com
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

Create and delete authoritative zones:

```bash
ib dns zone create test.local --comment "Lab zone"
ib dns zone create 2.0.192.in-addr.arpa --format IPV4
ib dns zone delete test.local
```

Set the active zone for the current shell:

```bash
ib dns zone use test.local
```

Zone names support shell completion when completion is enabled.

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
ib dns zone view <TAB><TAB>
```

## Troubleshooting

Run `ib configure` if you see a missing configuration or credential error.

Check the active context:

```bash
ib --help
ib dns --help
```

Use `--zone` for one-off commands when the active zone is not the target zone.

Use `ib dns search -g <keyword>` when you need to search outside the active zone
and its child zones.

If search results look stale, confirm the zone SOA serial changed:

```bash
ib dns zone view example.com
```

The record cache refreshes from Infoblox when the serial number changes. Zone
serial metadata can lag by up to 30 seconds because it is cached briefly for
faster repeated searches.
