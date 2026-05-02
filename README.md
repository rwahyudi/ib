# ib

`ib` is a small, punchy command-line remote control for Infoblox DNS.

It talks to Infoblox WAPI, saves your connection details in `~/.ib/config`,
keeps the password encrypted with a local key, and makes common DNS chores feel
less like spelunking through a web UI.

## What It Does

- `ib configure` sets up your Infoblox server, login, DNS view, and default zone.
- `ib dns create <type> --name <name> <value>` creates A, AAAA, CNAME, TXT, MX, or PTR records.
- `ib dns search <keyword>` finds records by name, value, or comment under the active zone, including child zones and host records.
- `ib dns delete <record name> <zone>` removes the record you meant to remove.
- `ib dns zone list|view|create|delete|use` handles authoritative zones and shows current DNS context.
- `ib completion` prints copy-ready shell completion setup for Bash, Zsh, and Fish.

## Quick Start

```bash
python3 -m pip install -r requirements.txt
install -m 755 ./ib ~/.local/bin/ib
ib configure
```

Then make DNS happen:

```bash
ib dns create a --name app 192.0.2.10 --ttl 300
ib dns search app
```

Use the session active zone, `IB_ZONE`, or the configured default zone when you
want shorter commands. Searches use that zone as the root and include child
authoritative zones; if no active/default zone is set, search falls back to the
configured DNS view.

Use `ib dns search -g <keyword>` to search across the configured DNS view
instead of limiting results to the active/default zone.

Searches skip secondary zones, cache `allrecords` data under
`~/.ib/allrecords-cache`, and refresh a zone only when its SOA serial number
changes.

To switch the active zone for the current shell session, run:

```bash
ib dns zone use test.local
```

A new shell session falls back to the configured default zone unless you run
`ib dns zone use` again.
