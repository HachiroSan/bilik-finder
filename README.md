# move-to-kl

Three rental-search tools kept in one repository so a fresh machine or server
only needs one clone. Each stays independent: no shared package, no merged
scraper, and no build step.

- **mudah-finder** searches [Mudah.my](https://www.mudah.my) rooms and
  properties and exposes the search through a small MCP server.
- **ibilik-finder** does the same job for [ibilik.com](https://www.ibilik.com)
  through that site's public client API.
- **mudah-alerts** polls saved Mudah searches and sends new listings to
  Telegram.

The alert service talks to Mudah only. iBilik has its own search and `watch`
CLI, but there is no iBilik alerting in this repository.

## Permission first

Mudah's [robots.txt](https://www.mudah.my/robots.txt) states that automated
access requires permission. Every Mudah network call in `mudah-finder` and
`mudah-alerts` stays disabled until this is set:

```pwsh
$env:MUDAH_AUTOMATION_ALLOWED = "1"
```

Set it only after Mudah has authorised your use. The code will not solve
CAPTCHAs, rotate identities, or get past a block. While the flag is not `1`,
the alert service exits before making any request. iBilik uses a public API;
check the iBilik README for its own terms.

## Layout

```text
move-to-kl/
├── Dockerfile                  alert-service image (stdlib Python, non-root)
├── docker-compose.yml          one service, named /data volume, no ports
├── deploy/config.example.json  container config, database_path=/data/mudah_alerts.db
├── .env.example                credential placeholders
├── .dockerignore               strict allowlist for the build context
├── .gitignore
├── .github/workflows/ci.yml    offline tests + compile checks
├── mudah-finder/
│   ├── mudah.py                stdlib search client + CLI
│   ├── mcp_server.py           MCP server (needs fastmcp)
│   ├── test_mudah.py
│   └── requirements-mcp.txt
├── ibilik-finder/
│   ├── ibilik.py               stdlib search client + CLI
│   ├── mcp_server.py           MCP server (needs fastmcp)
│   └── requirements-mcp.txt
└── mudah-alerts/
    ├── mudah_alerts.py         Telegram polling service (stdlib only)
    ├── test_mudah_alerts.py
    ├── config.example.json
    └── README.md
```

## What each piece needs

| Component | Entry point | Dependencies | Network gate |
| --- | --- | --- | --- |
| mudah-finder CLI | `mudah-finder/mudah.py` | stdlib | `MUDAH_AUTOMATION_ALLOWED=1` |
| mudah-finder MCP | `mudah-finder/mcp_server.py` | `fastmcp==4.0.8` | `MUDAH_AUTOMATION_ALLOWED=1` |
| ibilik-finder CLI | `ibilik-finder/ibilik.py` | stdlib | none |
| ibilik-finder MCP | `ibilik-finder/mcp_server.py` | `fastmcp==4.0.8` | none |
| mudah-alerts | `mudah-alerts/mudah_alerts.py` | stdlib | `MUDAH_AUTOMATION_ALLOWED=1` |

The MCP servers are optional. The finder CLIs and the alert service run on a
plain Python install.

## Local setup

Run these from the repository root. The examples use the Windows `py` launcher;
`python` works the same on other systems.

Finder CLI:

```pwsh
py mudah-finder\mudah.py search --location kuala-lumpur --max-price 900 --limit 5
```

MCP server (only if you want it):

```pwsh
py -m pip install -r mudah-finder\requirements-mcp.txt
py -m pip install -r ibilik-finder\requirements-mcp.txt
```

Alert service:

```pwsh
cd mudah-alerts
Copy-Item config.example.json config.json
$env:MUDAH_AUTOMATION_ALLOWED = "1"
$env:TELEGRAM_BOT_TOKEN = "123456:..."
$env:TELEGRAM_CHAT_ID = "123456789"
py mudah_alerts.py --config config.json --once
```

`--once` runs one cycle and exits. Leave it off to poll on
`poll_interval_seconds`. `--dry-run` reports matches without sending or writing
state. The alerts README covers every config field.

## Docker

The image is `python:3.13-slim` with nothing installed. It copies only
`mudah-alerts/mudah_alerts.py` and `mudah-finder/mudah.py`, runs as a non-root
user, and keeps SQLite on a named volume at `/data`.

```pwsh
Copy-Item .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and MUDAH_AUTOMATION_ALLOWED
docker compose up -d --build
docker compose logs -f
```

Compose mounts `deploy/config.example.json` read-only at `/config/config.json`.
That file sets `database_path` to `/data/mudah_alerts.db`, so state lands on the
`alerts-data` volume. If you would rather not edit the example, mount your own
config instead:

```yaml
# docker-compose.override.yml
services:
  mudah-alerts:
    volumes:
      - ./deploy/config.local.json:/config/config.json:ro
```

No ports are published; the service makes outbound HTTPS calls only.

Stop with `docker compose down`, which keeps the volume. `docker compose down -v`
also erases the volume, so reach for it only when you intend to discard state.

## Credentials

`.env` is git-ignored and holds real values. `.env.example` holds placeholders
and nothing else. The image never bakes credentials in; they arrive as
environment variables. Leave `MUDAH_AUTOMATION_ALLOWED=0` and the service stops
without touching Mudah.

## Persistent state and backups

State is one SQLite file on the `alerts-data` volume. Copy it while the service
is stopped so no write is in flight. Or use SQLite's own online backup, which is
safe while running:

```pwsh
sqlite3 mudah_alerts.db ".backup 'backup-mudah-alerts.db'"
```

If you copy files by hand, take the `-wal` and `-shm` sidecars too, or none of
them. A database copied mid-write can be corrupt. Updates replace code, not
data: keep the volume and the DB file across versions. Deleting the database is
never part of an update, and doing it makes the next run alert on every fresh
listing again.

## Delivery and polling

The service checks on an interval, so a match arrives on the next cycle after
the listing is published, not at that instant. Freshness is capped at 48 hours.
Delivery is best-effort with retries: if the process dies or the response is lost between
Telegram accepting a message and the local commit, the listing can be sent
again. Failed listings must be rediscovered while still fresh to be retried.
There is no guaranteed-delivery or exactly-once guarantee.

## Tests and CI

Everything is offline. The tests inject fake search, sender, and clock objects,
so no Mudah or Telegram request is made.

```pwsh
cd mudah-alerts
py -m unittest discover -v

cd ..\mudah-finder
py -m unittest discover -v
```

CI runs those two suites on `windows-latest`, one directory per step so the two
`mcp_server.py` modules cannot collide, plus a compile check of all three
directories.

## License

No license file is included here. Add one before you publish or share the code.
