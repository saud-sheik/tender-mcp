# tender-aggregator MCP Server

An MCP (Model Context Protocol) server that collects tenders from three Indian government portals into a single SQLite database, making them accessible to Claude through structured tools.

**Built for:** Swamina International (petroleum & gas procurement team)

## Quick Start

```bash
# 1. Clone and enter the directory
cd tender-mcp

# 2. Install dependencies
pip install -e ".[dev]"

# 3. Install Playwright browsers
playwright install chromium

# 4. Copy and fill credentials
cp .env.example .env
# Edit .env with HMEL_USERNAME, HMEL_PASSWORD, and your KEYWORDS

# 5. Run first refresh
python scripts/refresh.py

# 6. Start the MCP server (stdio transport for Claude Desktop)
python tender_mcp/server.py
```

## Architecture

```
tender-mcp/
├── tender_mcp/
│   ├── server.py       # FastMCP server – 6 tools exposed to Claude
│   ├── db.py           # SQLite layer (thread-safe, WAL mode)
│   └── refresh.py      # Orchestrates all scrapers
├── scrapers/
│   ├── cppp.py         # CPPP portal (httpx + BeautifulSoup, GET only)
│   ├── gem.py          # GeM portal (Playwright, JS-rendered)
│   └── hmel.py         # HMEL/mjunction (Playwright + session reuse)
├── scripts/
│   └── refresh.py      # CLI for cron scheduling
└── tests/
    ├── fixtures/        # Saved real HTML snapshots
    ├── test_cppp.py
    ├── test_gem.py
    └── test_db.py
```

## Sources

| Portal | URL | Method | Notes |
|--------|-----|--------|-------|
| CPPP | https://eprocure.gov.in/cppp/latestactivetendersnew/cpppdata | httpx GET | Public; no CAPTCHA on listing page |
| GeM | https://bidplus.gem.gov.in/all-bids | Playwright | JS-rendered; requires browser |
| HMEL | https://hmeleprocurement.mjunction.in | Playwright + login | Saves session state |

### CAPTCHA / OTP Policy
- **CPPP search form** has CAPTCHA, but the **default listing** (GET /cpppdata) does not. We use GET-only.
- **HMEL**: If an OTP or CAPTCHA is detected at login, the scraper stops with a clear error message. No bypass attempted.

## MCP Tools

| Tool | Description |
|------|-------------|
| `refresh_tenders` | Pull fresh data from portals; returns summary of new/updated |
| `search_tenders` | Query DB by keyword, source, status, or closing date |
| `get_tender` | Fetch a single tender by its composite ID |
| `export_tenders` | Write filtered tenders to CSV in `./exports/`; returns path |
| `suggest_relevance` | Score a tender against your configured KEYWORDS |
| `tender_stats` | DB counts and last-refresh statistics |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HMEL_USERNAME` | Yes (for HMEL) | – | mjunction login username |
| `HMEL_PASSWORD` | Yes (for HMEL) | – | mjunction login password |
| `HEADLESS` | No | `true` | Set to `false` to see Playwright browser |
| `KEYWORDS` | No | petroleum,LPG,… | Comma-separated relevance keywords |
| `CPPP_MAX_PAGES` | No | `3` | CPPP pages per refresh (10 tenders/page) |
| `GEM_MAX_PAGES` | No | `3` | GeM pages per refresh |

## Cron Setup (every 3 hours)

Add the following line to your crontab (`crontab -e`) using absolute paths:

```cron
0 */3 * * * cd /Users/saud/Projects/tender-mcp && /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 scripts/refresh.py >> /Users/saud/Projects/tender-mcp/refresh.log 2>&1
```

### macOS launchd alternative (preferred on Mac):

Copy the provided launchd plist to your user LaunchAgents folder:

```bash
cp /Users/saud/Projects/tender-mcp/scripts/com.swamina.tender-aggregator.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.swamina.tender-aggregator.plist
```

To stop or unload:
```bash
launchctl unload ~/Library/LaunchAgents/com.swamina.tender-aggregator.plist
```

## Connecting to Claude Desktop

All credentials and options are read **strictly from the local `.env` file** at the project root. The Claude Desktop configuration contains **no passwords**:

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "tender-aggregator": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
      "args": ["-m", "tender_mcp"],
      "cwd": "/Users/saud/Projects/tender-mcp"
    }
  }
}
```

Restart Claude Desktop. You should see the tools available in your conversation.

## Running Locally

To start the web dashboard locally on `127.0.0.1:8000`:
```bash
./scripts/run_dashboard.sh
# or directly:
uvicorn tender_mcp.web.app:app --host 127.0.0.1 --port 8000
```
Visit http://127.0.0.1:8000 in your browser and log in with credentials from `.env` (`DASHBOARD_USER` / `DASHBOARD_PASSWORD`).

## Running Tests

```bash
pytest tests/ -v
```

## Terms of Service Notes

- **CPPP**: Government of India public portal. Public listing access is unrestricted. No robots.txt found (404).
- **GeM**: Government of India public procurement marketplace. Public bid listings are publicly accessible. No access restrictions found.
- **HMEL/mjunction**: Vendor-login-only portal. Only access with your own registered credentials. Do not share credentials or bypass authentication.

## Known Limitations & Fragility

| Issue | Severity | Notes |
|-------|----------|-------|
| GeM JS rendering | Medium | Requires Playwright; breaks if GeM changes DOM structure |
| CPPP pagination URL encoding | Low | Base64-encoded URLs may change; but direct `?page=N` works |
| HMEL portal structure | High | mjunction portals frequently change HTML structure; scraper has broad strategies but may miss some tenders |
| HMEL OTP enforcement | Blocking | If HMEL enables OTP-on-every-login, session state reuse won't help |

## Custom Keyword Extensions

The following terms were added to `keywords.json` beyond the base keyword set. All keyword lists reside solely in `keywords.json` with none hardcoded in Python:

| Term | Target List in `keywords.json` | Reason Added |
|------|--------------------------------|--------------|
| `seals` | `product_kind_words`, `consumables` | Correctly identifies LPG cylinder tamper-evident seals and mechanical seals as product procurement rather than services. |
| `cylinders` | `product_kind_words` | Classifies LPG and high-pressure gas cylinder procurement as product tenders. |
| `filters` | `product_kind_words` | Identifies refinery fuel, lube oil, gas coalescers, and filtration elements as products. |
| `valves` | `product_kind_words` | Ensures gate, ball, globe, check, and control valve procurements are classified as products. |
| `fittings` | `product_kind_words` | Captures pipe fittings, flanges, and piping adapters procured for oil and gas installations as products. |
| `spares` | `product_kind_words` | Categorises mechanical and instrumentation spare parts for pumps, compressors, and plant machinery as products. |
| `equipment` | `product_kind_words` | Captures packages, skid units, testing apparatus, and industrial machinery as products. |
| `ups` | `staffing_noise` | Identifies and caps routine UPS maintenance and battery AMC tenders across refinery and bottling plant sites. |
| `sanitary` | `staffing_noise` | Identifies and caps plumbing, washroom, and sanitation facility maintenance at refinery complexes. |
| `gardening` | `staffing_noise` | Identifies and caps horticulture and landscaping contracts for refinery premises and townships. |
| `cleaning` | `staffing_noise` | Identifies and caps janitorial and drain/tank manual cleaning contracts at petroleum installations. |
| `canteen` | `staffing_noise` | Identifies and caps catering, mess, and canteen operation contracts at PSU refinery facilities. |
| `suiting` | `staffing_noise` | Identifies and caps staff uniform and fabric supply contracts for refinery personnel. |
| `goods transport` | `logistics_noise` | Segregates bulk haulage, packed bitumen trucking, and freight contracts into an independently configurable logistics noise cap. |

## Running in Production

### Web Dashboard Service
Start the dashboard on `127.0.0.1:8000`:
```bash
./scripts/run_dashboard.sh
# or directly:
uvicorn tender_mcp.web.app:app --host 127.0.0.1 --port 8000
```

### 1. Environment Keys (.env)
Credentials and runtime configuration are read exclusively from `.env`:
- `DASHBOARD_USER`: Username for HTTP Basic Auth on the web dashboard.
- `DASHBOARD_PASSWORD`: Secure 24-character password for web dashboard access.
- `DASHBOARD_OWNERS`: Comma-separated list of team member names for bid assignment.
- `HMEL_USERNAME` / `HMEL_PASSWORD`: Login credentials for HMEL mjunction portal.
- `HEADLESS`: `true` in production to run headless Chromium.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`: SMTP server credentials for alerts.
- `DIGEST_FROM`, `DIGEST_TO`: Sender and recipient email addresses for daily digest.
- `SLACK_WEBHOOK_URL`: (Optional) Slack incoming webhook for instant alert notifications.

### 2. Database Backups
Nightly backups are automated via `scripts/backup_db.py`:
```bash
python3 scripts/backup_db.py --keep 14
```
- Uses SQLite's online backup API (`conn.backup()`), guaranteeing clean, uncorrupted snapshots even while writes/refreshes occur in WAL mode.
- Retains the 14 most recent backup files in `./backups/tenders_YYYYMMDD_HHMMSS.db` and automatically prunes older ones.

### 3. Clearing Account Lockout (.hmel_locked)
If HMEL authentication encounters repeated failures or unexpected portal lockout responses, the system writes `.hmel_locked` to prevent account suspension:
- To inspect lockout details: check `.hmel_locked` content or `refresh.log`.
- To safely unlock once portal access is confirmed:
  ```bash
  python3 -c "from tender_mcp.config import unlock_hmel; unlock_hmel()"
  # or simply:
  rm .hmel_locked
  ```

### 4. Reading refresh.log
Refresh cycles, errors, and scheduled runs log to `refresh.log`:
```bash
tail -n 100 -f refresh.log
```
Key events to look for:
- `refresh: cppp ... finished (N new, M updated)`: Normal operation.
- `hmel: Login failed`: Authentication failure; check credentials or OTP prompts.
- `refresh_log`: Summary records stored in SQLite `refresh_log` table and displayed in dashboard health strip.

### 5. Troubleshooting Zero Tenders (0 tenders twice in a row)
If a source returns 0 tenders across two consecutive refresh runs:
1. **Check Connectivity & Block Pages**:
   - GeM: Run `HEADLESS=false python3 -m scrapers.gem` to check for Cloudflare / CAPTCHA challenge or layout changes.
   - CPPP: Run `python3 -m scrapers.cppp` to verify `eprocure.gov.in` is online (NIC servers frequently undergo maintenance on weekends).
2. **Inspect DOM Selectors**:
   - Verify if listing table classes or pagination parameters changed.
   - For GeM, verify `#bidCard .card` selectors on `https://bidplus.gem.gov.in/all-bids`.
3. **Session Cookies / IP Blocking**:
   - If blocked or timed out, increase request timeouts in scraper config or rotate outbound IP.

## Not yet tested: Docker and systemd. Test on the Linux host.

Docker and systemd unit files are provided in `Dockerfile`, `docker-compose.yml`, and `deploy/systemd/`, but they have **not yet been tested locally** because the local development machine is running macOS without Docker or systemd installed.

Do not claim the containerized deployment or systemd services work until they have been tested on a Linux deployment host.

After deploying on the target Linux host, run the verification smoke test:
```bash
./scripts/smoke_test.sh
```
This script sequentially:
1. Checks the `/health` endpoint on `http://127.0.0.1:8000/health`
2. Runs one refresh cycle with `--sources cppp`
3. Executes a digest dry-run (`python3 scripts/send_digest.py --dry-run`)
4. Runs a full database backup (`python3 scripts/backup_db.py --keep 14`)



