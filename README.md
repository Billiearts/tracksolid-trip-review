# Tracksolid Trip Review — README

A Flask web application that generates Excel "Trip Review" reports for a
haulage fleet from Tracksolid Pro (JIMI) GPS data, plus a live corridor map
of truck positions between Awaso and Takoradi.

This file is the **operations manual**: how to run it locally, how to deploy
it, how to back up the database, what to do when something breaks.
For "what it is and how it works", read `SYSTEM_OVERVIEW.md`.

---

## Table of contents

1. [What you get](#1-what-you-get)
2. [Requirements](#2-requirements)
3. [Local development](#3-local-development)
4. [Environment variables](#4-environment-variables)
5. [Database setup](#5-database-setup)
6. [Deploying to Render](#6-deploying-to-render)
7. [Deploying to a Windows 10 VM (test / pilot)](#7-deploying-to-a-windows-10-vm-test--pilot)
8. [Deploying to a Linux server (production)](#8-deploying-to-a-linux-server-production)
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
| OS | Any | Windows 10, Windows Server, Ubuntu 22.04 are all tested |

You also need:

- A **Tracksolid Pro account** (app key, app secret, user id, md5 password).
- A **Supabase project** (URL, anon key, service role key).
- A **contact email** for the primary super admin (currently
  `williamokyere360@gmail.com`).

---

## 3. Local development

### Windows

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
notepad .env
python app.py