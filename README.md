# move-to-kl

A room/bilik alert app that watches **both** [Mudah.my](https://www.mudah.my)
and [ibilik.com](https://www.ibilik.com) and pushes new rental listings to
Telegram, plus the two finder tools it is built on. Everything stays
stdlib-only. The pieces are kept in one repository so a fresh machine or server
only needs one clone, and each stays independent: no shared package, no merged
scraper, and no build step.

- **mudah-finder** searches Mudah.my rooms and properties and exposes the search
  through a small MCP server.
- **ibilik-finder** does the same job for ibilik.com through that site's public
  client API.
- **mudah-alerts** is the combined alerting service. It polls saved Mudah
  keyword searches and saved iBilik searches in one process and sends new
  listings through one Telegram bot. Mudah and iBilik each keep their own dedupe
  state, so an iBilik id can never mask a Mudah one.

Both providers are configured together by default (see
`config.example.json`) and the service polls once an hour by default. The two
providers are independent: if Mudah is not authorised, or one site has a bad
cycle, the other keeps polling.

## Permission first

Mudah's [robots.txt](https://www.mudah.my/robots.txt) states that automated
access requires permission. Every Mudah network call in `mudah-finder` and
`mudah-alerts` stays disabled until this is set:

```pwsh
$env:MUDAH_AUTOMATION_ALLOWED = "1"
```

Set it only after Mudah has authorised your use. The code will not solve
CAPTCHAs, rotate identities, or get past a block. While the flag is not `1`, the
alert service logs a warning and skips the Mudah searches, and any configured
iBilik searches keep polling, because iBilik uses a public API. A Mudah-only
config with the flag off has nothing left to run and exits with a clear message.
The standalone `mudah-finder` keeps its own hard gate: every Mudah tool fails
until `MUDAH_AUTOMATION_ALLOWED=1`. Check the iBilik README for that site's
terms.

## Layout

```text
move-to-kl/
├── run_alerts.py               local launcher: loads root .env, runs the service
├── config.example.json         combined Mudah + iBilik default config
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
    ├── mudah_alerts.py         combined Telegram polling service (stdlib only)
    ├── test_mudah_alerts.py
    ├── test_ibilik_alerts.py
    ├── test_alert_scheduling.py
    ├── test_combined_providers.py
    ├── config.example.json     combined Mudah + iBilik (same defaults)
    ├── config.ibilik.example.json   iBilik-only alternative
    └── README.md
```

## What each piece needs

| Component | Entry point | Dependencies | Network gate |
| --- | --- | --- | --- |
| mudah-finder CLI | `mudah-finder/mudah.py` | stdlib | `MUDAH_AUTOMATION_ALLOWED=1` |
| mudah-finder MCP | `mudah-finder/mcp_server.py` | `fastmcp==4.0.8` | `MUDAH_AUTOMATION_ALLOWED=1` |
| ibilik-finder CLI | `ibilik-finder/ibilik.py` | stdlib | none |
| ibilik-finder MCP | `ibilik-finder/mcp_server.py` | `fastmcp==4.0.8` | none |
| combined alerts (local) | `run_alerts.py` | stdlib | Mudah side only |
| alert service | `mudah-alerts/mudah_alerts.py` | stdlib | Mudah side only |

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

Combined alert service (both providers). `run_alerts.py` loads the root `.env`
for you and defaults `--config` to the tracked `config.example.json` when no
`config.json` exists:

```pwsh
Copy-Item .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
# Mudah stays off until MUDAH_AUTOMATION_ALLOWED=1 is set; iBilik is public
py run_alerts.py --dry-run     # safe first look: report matches, send nothing
py run_alerts.py --once        # one cycle; sends alerts and seeds new iBilik
py run_alerts.py               # poll hourly (config default)
```

`.env` loading is plain stdlib and never prints values. Precedence is explicit:
a variable already set in the real environment always wins; `.env` only fills
what is unset. Do not commit `.env`.

iBilik-only or Mudah-only configs still work: copy
`mudah-alerts\config.ibilik.example.json` or write a `config.json` with only the
provider you want, and pass it with `--config`. Both providers can also be
driven directly:

```pwsh
cd mudah-alerts
py mudah_alerts.py --config config.example.json --once
```

`--once` runs one cycle and exits. Leave it off to poll on
`poll_interval_seconds` (default `3600`). `--dry-run` reports matches without
sending or writing state, and for an unseeded iBilik search it reports what it
would seed instead of listing hundreds of messages. The alerts README covers
every field.

## Docker

The image is `python:3.13-slim` with nothing installed. It copies
`mudah-alerts/mudah_alerts.py`, `mudah-finder/mudah.py`, and
`ibilik-finder/ibilik.py`, runs as a non-root user, and keeps SQLite on a named
volume at `/data`.

```pwsh
Copy-Item .env.example .env
# edit .env: set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, and MUDAH_AUTOMATION_ALLOWED
docker compose up -d --build
docker compose logs -f
```

Compose mounts `deploy/config.example.json` read-only at `/config/config.json`.
It contains the current Kuala Lumpur focus searches and sets `database_path` to
`/data/mudah_alerts.db`, so alert state lives on the `alerts-data` volume. With
`MUDAH_AUTOMATION_ALLOWED=1` in `.env` both providers poll; leave it `0` and
Mudah is skipped while iBilik keeps running. Set the flag to `1` only after
Mudah has authorised your automated access. To use another config, mount it at
`/config/config.json` instead:

```yaml
# docker-compose.override.yml
services:
  mudah-alerts:
    volumes:
      - ./deploy/config.local.json:/config/config.json:ro
```

No ports are published; the service makes outbound HTTPS calls only.

### Arcane Docker Manager

Use an Arcane **Project** with Compose, not a Swarm stack. The service builds
from the repository (`build.context: .`), so Arcane needs the Dockerfile and
source folders as well as `docker-compose.yml`.

1. Push the repository changes to a branch Arcane can read. In Arcane, open
   **Projects → Create Project → From Git Repo**. Choose **Pull**, set **Target
   Type** to **Project**, select the repository and branch, and set the Compose
   file path to `docker-compose.yml` at the repository root. Turn on **Sync
   Files** so Arcane copies `Dockerfile`, the finder folders, and `deploy/`.
2. In the project's **Environment Configuration (.env)** editor, set
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Set
   `MUDAH_AUTOMATION_ALLOWED=1` only after Mudah has authorised your use; at `0`,
   iBilik still runs but Mudah is skipped. Keep these credentials out of Git.
3. Choose **Build & Deploy**, then check the project logs. The process polls
   hourly and restarts automatically unless you stop it.

Arcane saves the project `.env` beside the Compose file. The Compose mount uses
the tracked `deploy/config.example.json`, so it receives the configured searches
when Arcane syncs the repository. The named `alerts-data` volume keeps sent
state across redeploys; don't delete it unless you want the service to forget
which listings it has already handled. A new Docker volume starts with fresh
state: iBilik silently seeds current listings on its first cycle, while Mudah
may send matching listings from its 48-hour freshness window. The local
`mudah_alerts.db` file is not copied into that volume automatically.

Stop with `docker compose down`, which keeps the volume. `docker compose down -v`
also erases the volume, so reach for it only when you intend to discard state.

## Credentials

`.env` is git-ignored and holds real values. `.env.example` holds placeholders
and nothing else. The image never bakes credentials in; they arrive as
environment variables. `run_alerts.py` reads the root `.env` with a stdlib
parser and never prints values. Precedence is explicit: a variable already set
in the real environment wins, and `.env` only fills the unset ones, so a shell
export or Docker `environment:` entry always overrides the file. Leave
`MUDAH_AUTOMATION_ALLOWED=0` and the Mudah side is skipped with a warning while
any iBilik searches keep running, because that provider needs no flag.

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

The service checks on an interval. The default is one hour
(`poll_interval_seconds: 3600`); config may set anything from 60 seconds to 24
hours. A match therefore arrives on the next cycle after the listing is
published, not at that instant. Mudah freshness is capped at 48 hours; an iBilik
search has its own window (default 48, capped at 7 days) and, by default, seeds
its current listings on first run without sending them, so adding a new iBilik
search does not flood the chat. Delivery is best-effort with retries: if the
process dies or the response is lost between Telegram accepting a message and
the local commit, the listing can be sent again. Failed sends stay retryable,
while listings already sent or seeded are never re-sent, so old inventory is not
pushed every hour. Failed listings must be rediscovered while still fresh to be
retried. There is no guaranteed delivery or exactly-once guarantee.

## Tests and CI

Everything is offline. The tests inject fake search, sender, and clock objects,
so no Mudah, iBilik, or Telegram request is made.

```pwsh
cd mudah-alerts
py -m unittest discover -v

cd ..\mudah-finder
py -m unittest discover -v
```

The alerts directory discovers four modules: `test_mudah_alerts.py`,
`test_ibilik_alerts.py`, `test_alert_scheduling.py`, and
`test_combined_providers.py`. CI runs those suites on `windows-latest`, one
directory per step so the two `mcp_server.py` modules cannot collide, plus a
compile check of the three directories and `run_alerts.py`.

## License

No license file is included here. Add one before you publish or share the code.
