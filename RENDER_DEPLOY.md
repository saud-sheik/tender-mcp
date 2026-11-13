# Deploying Swamina Tender Aggregator to Render

This guide outlines how to deploy the Tender Aggregator to **[Render](https://render.com/)** using Docker.

---

## Prerequisites

1. A **[Render account](https://dashboard.render.com/)**.
2. Your GitHub repository connected to Render.

---

## Method 1: Automatic Deployment using Render Blueprint (Recommended)

The repository contains a `render.yaml` specification that sets up the web service, Docker runtime, health checks, and environment keys automatically.

1. Go to your **[Render Dashboard](https://dashboard.render.com/)**.
2. Click **New +** (top right) $\rightarrow$ **Blueprint**.
3. Select your `tender-mcp` repository.
4. Render will detect `render.yaml`.
5. Under **Environment Variables**, fill in:
   - `DASHBOARD_USER`: Username to log in to the dashboard (e.g. `admin`).
   - `DASHBOARD_PASSWORD`: Click **Generate** or type a secure password.
   - `HMEL_USERNAME` (optional): Vendor ID for HMEL mjunction (can also be entered later in `/settings`).
   - `HMEL_PASSWORD` (optional): Vendor password for HMEL mjunction.
   - `SLACK_WEBHOOK_URL` (optional): Your team's Slack webhook URL for instant Tier A alerts.
6. Click **Apply**.
7. Render will build the Docker container, install Chromium with Playwright, and launch the service.

---

## Method 2: Manual Deployment as a Web Service

If you prefer setting up the web service manually:

1. In the **Render Dashboard**, click **New +** $\rightarrow$ **Web Service**.
2. Select your repository: `tender-mcp`.
3. Configure the service settings:
   - **Name**: `tender-mcp` (or any name you prefer)
   - **Region**: Choose the closest region (e.g., Oregon, Frankfurt, Singapore)
   - **Branch**: `main`
   - **Runtime**: **Docker**
   - **Dockerfile Path**: `Dockerfile`
   - **Instance Type**: **Free** (or **Starter** if using persistent disk)
4. Under **Advanced Settings**:
   - **Health Check Path**: `/healthz`
5. Under **Environment Variables**, add:
   - `DASHBOARD_USER` = `your_username`
   - `DASHBOARD_PASSWORD` = `your_secret_password_here`
   - `HEADLESS` = `true`
   - `AUTO_REFRESH_INTERVAL_MINUTES` = `180` (runs background refresh every 3 hours)
   - `HMEL_USERNAME` = `(optional)`
   - `HMEL_PASSWORD` = `(optional)`
   - `SLACK_WEBHOOK_URL` = `(optional)`
6. Click **Create Web Service**.

---

## Important Architecture & Persistence Notes

### 1. Port Binding
Render dynamically assigns a port via the `$PORT` environment variable. The Dockerfile starts Uvicorn with:
```bash
uvicorn tender_mcp.web.app:app --host 0.0.0.0 --port ${PORT:-8000}
```
This automatically binds to whichever port Render assigns.

### 2. Liveness Check
The service exposes `/healthz` as a lightweight probe that returns HTTP 200 immediately upon startup.

### 3. Persistent Storage (SQLite Data)
- **Free Tier**: Render's free tier uses an ephemeral filesystem. If the container sleeps or redeploys, SQLite re-initializes tables automatically.
- **Starter Tier with Persistent Disk**:
  If you upgrade to a Starter instance, add a **Disk** under your service settings:
  - **Name**: `tender-data`
  - **Mount Path**: `/app/data`
  - **Size**: `1 GB`
  Then set an environment variable:
  - `DB_PATH` = `/app/data/tenders.db`
  This ensures the database persists across all restarts and redeploys.

---

## Post-Deployment Checklist

Once the build finishes and shows **Live**:

1. Click your Render URL (`https://tender-mcp-xxxx.onrender.com`).
2. Log in using `DASHBOARD_USER` and `DASHBOARD_PASSWORD`.
3. Go to **Settings** (`/settings`):
   - Click **⚡ Run Full Refresh (All Portals)** to perform the initial ingestion from GeM, CPPP, and HMEL.
   - If you have HMEL client credentials, save them under the HMEL card.
   - Configure or test your **Instant Slack Webhook Alerts**.
   - Confirm the **Auto-Refresh Scheduler** status shows **Active (every 180 minutes)**.
