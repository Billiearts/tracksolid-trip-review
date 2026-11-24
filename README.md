Here is the updated `README.md`, completely rewritten to reflect the **working IIS + ARR + Waitress architecture** you successfully deployed. It now serves as a definitive operations manual for the company server.

***

```markdown
# Tracksolid Trip Review — README

A Flask web application that generates Excel "Trip Review" reports for a
haulage fleet from Tracksolid Pro (JIMI) GPS data, plus a live corridor map
of truck positions between Awaso and Takoradi.

This file is the **operations manual**: how to run it locally, how to deploy
it, how to back up the database, what to do when something breaks.

---

## Table of contents

1. [What you get](#1-what-you-get)
2. [Requirements](#2-requirements)
3. [Architecture (Three Environments)](#3-architecture-three-environments)
4. [Local development](#4-local-development)
5. [Environment variables](#5-environment-variables)
6. [Database setup](#6-database-setup)
7. [Deploying to Render](#7-deploying-to-render)
8. [Deploying to a Company Server (Windows)](#8-deploying-to-a-company-server-windows)
9. [Roles and access](#9-roles-and-access)
10. [Backups and recovery](#10-backups-and-recovery)
11. [Troubleshooting](#11-troubleshooting)
12. [Maintenance schedule](#12-maintenance-schedule)

---

## 1. What you get

Two independent features that share the same Tracksolid account and
Supabase project:

- **Trip Review reports** — pick trucks + date range → the app finds trips,
  computes stops, rests, yard time, driver-behaviour events, and a 3-month
  history → fills an Excel template → user can edit the metadata, draw a
  signature, and download the `.xlsx`.
- **Live corridor map** — a Leaflet map of every regular truck on the
  Awaso↔Takoradi route, with occupancy counts for Dispatch, Workshop, and
  Washing Plant, plus direction tiles (heading to / at Awaso / Takoradi).

The app also has an **audit trail** with a hash chain, alert rules, and a
super-admin role that is invisible to normal admins.

---

## 2. Requirements

| Component | Minimum | Notes |
|---|---|---|
| Python | 3.11 | 3.12 is what production runs on |
| Disk | 3 GB | For the app + a few hundred Excel files |
| RAM | 1 GB | The app peaks at ~400 MB under normal use |
| Network | Outbound HTTPS | To `eu-open.tracksolidpro.com` and `*.supabase.co` |
| OS | Any | Windows 10/11, Windows Server, Ubuntu 22.04 are all tested |

You also need:

- A **Tracksolid Pro account** (app key, app secret, user id, md5 password).
- A **Supabase project** (URL, anon key, service role key).
- A **contact email** for the primary super admin (currently
  `williamokyere360@gmail.com`).

---

## 3. Architecture (Three Environments)

The application uses a split-entrypoint design so it can run seamlessly in three different environments without configuration conflicts.

| Environment | Command Run | Entry File | Server | Access URL |
| :--- | :--- | :--- | :--- | :--- |
| **Local Dev** | `python app.py` | `app.py` | Flask Dev Server | `http://localhost:5000` |
| **Render (Cloud)** | `gunicorn -c gunicorn.conf.py wsgi:app` | `wsgi.py` | Gunicorn | Render URL |
| **Company Server** | Managed by NSSM | `start_server.py` | Waitress (hidden) | `http://<server-ip>` |

**Company Server Stack:** 
Browser → IIS (Port 80) → ARR Reverse Proxy → Waitress (127.0.0.1:5000) → Flask App. 
*Port 5000 is never exposed to the corporate LAN.*

---

## 4. Local development

### Windows

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python app.py
```
Open `http://localhost:5000`.

---

## 5. Environment variables

Copy `.env.example` to `.env` and fill in the values:

```env
# --- Tracksolid Pro ---
TRACKSOLID_APP_KEY="your_app_key"
TRACKSOLID_APP_SECRET="your_app_secret"
TRACKSOLID_USER_ID="your_user_id"
TRACKSOLID_USER_PWD_MD5="your_password_md5"

# --- Supabase ---
SUPABASE_URL="https://your-project.supabase.co"
SUPABASE_ANON_KEY="your_anon_key"
SUPABASE_SERVICE_ROLE_KEY="your_service_role_key"

# --- Flask ---
FLASK_SECRET="generate_a_random_long_string_here"
FLASK_DEBUG="0"

# --- Worker ---
JOB_WORKERS="4"
```

---

## 6. Database setup

The system uses Supabase for Auth and Postgres storage. You need the following tables created (see the `migrations/` folder or `SYSTEM_OVERVIEW.md` for schemas):
`users`, `profiles`, `user_sessions`, `reports`, `report_jobs`, `report_job_items`, `audit_log`, `audit_chain_head`, `audit_alert`, `fence_polygons`, `api_usage_window`, `api_usage_counters`.

---

## 7. Deploying to Render

Render uses the `wsgi.py` entry point and `gunicorn.conf.py`. 
1. Connect the GitHub repo to Render.
2. Set the Build Command: `pip install -r requirements.txt`
3. Set the Start Command: `gunicorn -c gunicorn.conf.py wsgi:app`
4. Add all the Environment Variables from `.env` into the Render dashboard.

---

## 8. Deploying to a Company Server (Windows)

This is the production-grade setup for the internal corporate network. It uses IIS as a reverse proxy to hide port 5000.

### Phase 8.1: Prerequisites
1. **Python 3.12**: Install from python.org. **Crucial:** Check "Add python.exe to PATH".
2. **Git**: Install Git for Windows.
3. **NSSM**: Download `nssm-2.24.zip` from nssm.cc, extract, and copy `win64\nssm.exe` to `C:\Windows\System32\`.
4. **IIS Features**: Open "Turn Windows features on or off". Enable **Internet Information Services** -> **Web Management Tools** -> **IIS Management Console**. Expand **World Wide Web Services** -> **Application Development Features** and check **WebSocket Protocol**.
5. **IIS Modules**: Download and install **URL Rewrite Module 2.0** and **Application Request Routing (ARR) 3.0** from Microsoft.

### Phase 8.2: Project Setup
Open an Administrator Command Prompt:
```cmd
cd C:\
git clone https://github.com/Billiearts/tracksolid-trip-review.git
cd tracksolid-trip-review
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```
Create the `.env` file and populate it with your production keys.

### Phase 8.3: Backend Configuration (Crucial)
Create a file named `start_server.py` in the root folder (if it doesn't exist). It must bind to `127.0.0.1`, **not** `0.0.0.0`:
```python
from waitress import serve
from app import app

if __name__ == "__main__":
    serve(app, host="127.0.0.1", port=5000, threads=8)
```

### Phase 8.4: NSSM Service Installation
Run these commands in the Administrator Command Prompt to install the app as a background service:
```cmd
:: Create logs folder
mkdir C:\tracksolid-trip-review\logs

:: Install and configure NSSM
nssm install TracksolidApp "C:\tracksolid-trip-review\.venv\Scripts\python.exe"
nssm set TracksolidApp AppParameters "C:\tracksolid-trip-review\start_server.py"
nssm set TracksolidApp AppDirectory "C:\tracksolid-trip-review"
nssm set TracksolidApp Start SERVICE_AUTO_START

:: Setup logging
nssm set TracksolidApp AppStdout "C:\tracksolid-trip-review\logs\service-out.log"
nssm set TracksolidApp AppStderr "C:\tracksolid-trip-review\logs\service-error.log"
nssm set TracksolidApp AppRotateFiles 1

:: Start the service
nssm start TracksolidApp
```
Test that the backend works: Open `http://127.0.0.1:5000` on the server itself. If it loads, the backend is ready.

### Phase 8.5: IIS Reverse Proxy Configuration
1. Open **IIS Manager** (`inetmgr`).
2. Click the top-level **Server Name** node on the left.
3. Double-click **Application Request Routing Cache**.
4. On the right, click **Server Proxy Settings**.
5. Check **Enable proxy**. Uncheck **Reverse rewrite host in response headers**. Click **Apply**.
6. Right-click **Sites** -> **Add Website**.
   * Site name: `TracksolidTripReview`
   * Physical path: Create a dummy folder like `C:\inetpub\tracksolid-frontend` (IIS only uses this as a placeholder).
   * Binding: Type `http`, IP `All Unassigned`, Port `80`.
7. Click on the new site `TracksolidTripReview`.
8. Double-click **URL Rewrite**.
9. Click **Add Rule(s)** -> **Reverse Proxy**.
10. Server name or IP address: `127.0.0.1:5000` (Do NOT include `http://`). Click **OK** and **Apply**.

### Phase 8.6: Firewall
Open port 80 for the corporate network. Port 5000 should remain closed.
```cmd
netsh advfirewall firewall add rule name="Tracksolid IIS Port 80" dir=in action=allow protocol=TCP localport=80
```

**Final Test:** From another machine on the network, open `http://<company-server-ip>`. The app should load without the port.

---

## 9. Roles and access

- **user**: Can search for trips, generate previews, and export reports for their own jobs.
- **admin**: Can view all reports, manage users, and edit fences on the live map.
- **super_admin**: Can view the audit trail, manage alerts, and edit other admins. The primary super admin is protected.
- **extra_roles**: `live_fleet_view` grants access to the Live map.

---

## 10. Backups and recovery

- **Supabase**: Upgrade to a paid plan for automated daily backups.
- **Manual Backup**: Run `pg_dump` on a cron job on the server, or export tables directly from the Supabase dashboard.
- **Excel Files**: The app uploads generated Excel files to the Supabase Storage `reports` bucket. Local copies are kept in the `reports/` folder on the server.

---

## 11. Troubleshooting

**I get a 500 error when accessing the server IP on IIS**
- Check `C:\tracksolid-trip-review\logs\service-error.log`.
- Open IIS Manager, ensure the `DefaultAppPool` is started.
- Verify ARR Proxy is enabled (Phase 8.5, Step 5).

**I get a 502 Bad Gateway on IIS**
- The Waitress backend is likely down. Run `nssm status TracksolidApp` (should be `SERVICE_RUNNING`).
- Check `netstat -ano | findstr LISTENING | findstr :5000` (should show `127.0.0.1:5000`).

**The app is slow or times out**
- Tracksolid API might be rate-limited. Check the quota tracker in the nav bar.
- Check the server's RAM usage. Waitress `threads=8` might be too high for a low-spec VM.

**Preview expired error**
- Single-trip previews are stored in memory and expire if the server restarts or after 20 other previews. Generate the report again.

---

## 12. Maintenance schedule

- **Daily**: Check the Render/VM server memory usage.
- **Weekly**: Check the `service-error.log` for any unhandled Python tracebacks.
- **Monthly**: Verify the IIS logs (`C:\inetpub\logs\LogFiles`) for 404/500 errors. Rotate `.env` secrets if any staff members leave.
- **Yearly**: Verify the Supabase database backup restores correctly.
```