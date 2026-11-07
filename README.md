# Physics Lab Schedule Agent System

> AI-powered assistant for university physics experiment scheduling: course arrangement, class rescheduling, and lab-spec query.

[🇨🇳 中文版 README (Chinese)](README.zh.md)

---

## What is this?

A **three-module intelligent system** for university physics experiment teaching:
**scheduling, rescheduling, and lab-spec query** (the scheduling module covers parallel batch
scheduling, whole-school one-click scheduling, master data, plan landing, the weekly timetable grid,
the data audit and standard-timetable export):

| Module | Capability |
|--------|-----------|
| 📅 **Scheduling** | Constraint-based auto-scheduling, conflict detection, candidate plan recommendation |
| ⚡ **Parallel batch scheduling** | CP-SAT solves one class's *all experiments × student groups* in one shot (the #1 pain point from the advisor interview); missing teacher/room info is auto-completed with every assumption surfaced |
| 🗂️ **Master data** | Courses / experiments / teachers / rooms / classes as first-class CRUD entities, reverse-derived from the schedule, persisted across restarts, exportable to Excel and importable back |
| 🔄 **Rescheduling** | Template + natural language flow, graded conflict detection (room / teacher / whole-group), admin approval that actually lands in the schedule |
| 🔬 **Lab-spec query** | Lab staff / teachers look up an experiment's contact hours, required room, procedure, data-processing and error-analysis rules, and report spec — used to cross-check a schedule |

**Design principle**: rule engine guarantees correctness (conflict filtering), LLM enhances experience (intent parsing, candidate plans, consultation).

## Why this project?

- Paper research shows lab courses are rarely handled by university academic systems — scheduling relies on manual coordination.
- One schedule adjustment can trigger conflicts across multiple classes, teachers, and labs.
- Existing tools (Excel, asking class representatives) cannot efficiently resolve cross-class conflicts.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python FastAPI + openpyxl + **OR-Tools CP-SAT** (`ortools`) |
| Frontend | React + Vite + TypeScript + Tailwind CSS |
| LLM | TokenRhythm (OpenAI-compatible) / replaceable via env vars |
| Data | Excel schedule (433 entries) + five-table master data (no student data) |

## Quick Start

### Requirements

- Python 3.10+
- Node.js 18+
- **LLM credentials** (pick one; auto-detected in this order):

| Priority | Env var | Note |
|---:|---|---|
| 1 | `ZHIPU_API_KEY` | Free & unlimited (primary) |
| 2 | `MODELSCOPE_API_KEY` | Free 2000/day |
| 3 | `TOKENHUB_API_KEY` | Trial pack, 402 when exhausted |
| 4 | `DEEPSEEK_API_KEY` | Paid |
| 5 | `AMD_API_KEY` / `XFYUN_API_KEY` | Free tier, rate-limited |
| — | `JIYUAN_LVDONG_API_KEY` | **Deprecated: 402 exhausted, do not use** |

> LLM is **optional** — the rule engine handles all scheduling logic.
> Without it, intent parsing falls back to rules (set `LLM_DISABLED=1` to disable explicitly).

### Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Open **http://localhost:5173**

## Usage Flow

1. **Select identity** — pick your teacher card (system knows who you are)
2. **My courses** — view all courses with adjustable/non-adjustable status
3. **Template rescheduling** — select course + target week, auto conflict filtering
4. **Results** — green available slots + gray excluded slots with reasons + AI candidate plans
5. **Submit** — create reschedule request
6. **Admin approval** — approve/reject (requires admin login)
7. **Lab-spec query** — experiment-spec Q&A for lab staff / teachers

## Core Design Decisions

One principle runs through the entire project: **the rule engine guarantees correctness, the LLM only enhances experience, and LLM output never directly modifies scheduling data**.

Four concrete manifestations:

1. **Conflict detection is never delegated to the LLM**. The three core conflicts (room / teacher / same-class-same-group) are judged by the rule engine at (class, group) granularity — so the 47 legitimate parallel-teaching slots in the real table produce **zero false positives**. The LLM parses natural-language intent and generates candidate plans, but its suggestions must pass rule-engine filtering before being shown to the user.

2. **The solver only schedules what should be scheduled**. Intro/exam projects are visible in the master data and UI, but **never enter the CP-SAT solver** — they are not teaching units that need time-slot assignment. This is an explicit filter, not something handled by data cleaning.

3. **No silent guessing when information is missing**. The gray-box mode of parallel batch scheduling auto-fills missing teachers (least-loaded first) / rooms / weeks, but **every assumption is reported to the user** — they can see what decisions the system made on their behalf.

4. **Write operations are reversible**. Plan landing follows five steps: diff preview → admin approval → write-back → undo → restart replay. Every step is persisted; you can always return to the pre-adoption snapshot.

## Key Features

- ✅ **Graded conflict detection**: core conflicts (room / teacher / same class+group) say *what* is hit and *how to fix it*
- ✅ **Parallel batch scheduling**: CP-SAT over the whole class (all experiments × all student groups); gray-box mode auto-fills and reports assumptions
- ✅ **Master data as first-class citizens**: five entity tables, reverse-derived then editable, survives restarts; intro/exam projects are visible but **never scheduled**
- ✅ **Ledger round-trip**: export the five tables to Excel, edit offline, import back — with guards against stat-column masquerade and sharded class names
- ✅ **Natural language rescheduling**: "I'm teacher Wu, move next week's class" → LLM intent parsing with multi-turn follow-up for missing info
- ✅ **Plan landing**: whole-school plans show a four-category diff first; admin adoption writes back to the schedule, with undo and restart replay
- ✅ **Weekly timetable grid**: visual overview of any week (day × slot), conflicts marked

## Project Structure

```
physics-scheduler-agent/
├── backend/
│   ├── app/
│   │   ├── main.py                 # FastAPI entry
│   │   ├── api/reschedule.py       # API endpoints
│   │   ├── master_data.py          # Five-table master data (reverse-derived, CRUD)
│   │   ├── master_store.py         # Versioned JSON persistence for master data
│   │   ├── master_workbook.py      # Excel ledger export / import (round-trip)
│   │   ├── conflict_model.py       # Graded conflict classification (core 3 kinds)
│   │   ├── course_bridge.py        # Course attribution + cross-course clash scan
│   │   ├── agent/                  # RescheduleAgent, LLM intent parser
│   │   ├── engine/                 # Constraint-based auto scheduler (CP-SAT)
│   │   └── utils/                  # Excel parser / time model
│   └── tests/                      # pytest suite
├── frontend/
│   └── src/                        # React UI (dashboard, master data, parallel, …)
├── scripts/                        # Independent auditors + release preflight
├── docs/                           # PRD, architecture, user guide, reflection
└── start.ps1 / start.bat           # One-click launcher
```

## API Overview

| Method | Path | Module | Description |
|--------|------|--------|-------------|
| POST | `/api/reschedule/upload` | Scheduling | Upload experiment schedule Excel |
| GET | `/api/reschedule/teachers` | Scheduling | Teacher list (identity selection) |
| GET | `/api/reschedule/teachers/{name}/courses` | Scheduling | Teacher's courses |
| POST | `/api/reschedule/auto-schedule` | Scheduling | Auto schedule generation |
| POST | `/api/reschedule/chat` | Rescheduling | Reschedule query (available + excluded + AI plans) |
| POST | `/api/reschedule/requests` | Rescheduling | Submit reschedule request |
| POST | `/api/reschedule/requests/{id}/approve` | Rescheduling | Admin approval (applies to schedule via /apply-move) |
| GET | `/api/reschedule/parallel/overview` | Parallel | Course overview (experiments × groups) |
| POST | `/api/reschedule/parallel/solve` | Parallel | One-shot CP-SAT solve for a whole class |
| POST | `/api/reschedule/upload-master` | Master data | Upload syllabus/task list (from-zero input) |
| POST | `/api/reschedule/master/import-preview` | Master data | Import wizard step 2: read-only preview of the workbook field mapping, unrecognized/stat columns and row counts — writes nothing |
| POST | `/api/reschedule/master/tasks-preview` | Master data | Import wizard step 2 (task-list variant): read-only preview of the list column mapping — writes nothing |
| GET | `/api/reschedule/master/overview` | Master data | Five-table master data (read-only) |
| GET | `/api/reschedule/export/master-workbook` | Master data | Export editable ledger (importable back) |
| GET | `/api/reschedule/data-audit` | Dashboard | Graded conflict audit + parallel-teaching note |
| GET | `/api/reschedule/export/class-schedule` | Export | Per-class timetables (course column + cross-course clashes) |
| GET | `/api/reschedule/export/room-schedule` | Export | Per-room timetables |
| POST | `/api/reschedule/auto-schedule` | Scheduling | Full-school one-click draft (chunked, all 171 tasks) |
| POST | `/api/reschedule/parallel/solve-all` | Scheduling | Whole-school group-level plan (per class, per day, 17/17) |
| POST | `/api/reschedule/parallel/land/preview` | Landing | Diff preview vs current table (added/changed/uncovered/kept + warnings, no write) |
| POST | `/api/reschedule/parallel/land` | Landing | Commit plan into the schedule table (admin; ledger in sqlite for restart replay) |
| POST | `/api/reschedule/parallel/land/undo` | Landing | Undo last landing (admin; restores pre-landing snapshot) |
| GET | `/api/reschedule/parallel/land/status` | Landing | Landing state (drives the undo entry in the UI) |
| GET | `/api/reschedule/schedule-grid` | View | Weekly timetable grid: one week as weekday × period cells (7 days × 4 periods, cross-period entries appear in every occupied cell, conflict marks from the audit) |
| POST | `/api/reschedule/auto-schedule/export` | Export | Draft workbook (master sheet + one sheet per class) |
| POST | `/api/reschedule/admin-login` | Auth | Admin login (approval desk) |
| POST | `/api/reschedule/experiment-tutor` | Lab-spec | Lab-spec query (LLM, for lab staff/teachers) |
| GET | `/health` | - | Health check |

## Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt   # pytest lives in requirements-dev
cd backend
python -m pytest tests/ -v
```

Ruff gate (the same rules CI enforces; every `ignore` is justified in `ruff.toml`):

```bash
ruff check --config ruff.toml .
```

CI (`.github/workflows/ci.yml`) runs on every push/PR: **backend** (py3.12 — ruff → import smoke → pytest) and **frontend** (node 22 — `npm run verify` = tsc + vitest). In a clean checkout (no `schedule.xlsx`) the expected CI shape is "passed + skipped, zero failed" — real-table cases skip there by design.

Backend tests need no network and no LLM key (`conftest.py` defaults `LLM_DISABLED=1` /
`RATELIMIT_DISABLED=1`). The real `schedule.xlsx` is gitignored; without it, the real-table
cases **skip** (clean checkouts stay green) instead of erroring.

Independent auditors (they re-read the raw Excel / re-solve from scratch instead of reusing
backend code, so a green pytest run alone is not trusted):

```bash
python scripts/verify_graybox.py        # lexical/coverage/internal-collision audit
python scripts/verify_school_plan.py    # independent re-solve of the whole-school plan
python scripts/audit_conflicts.py       # openpyxl + real-clock conflict scan
python scripts/probe_approval_chain.py  # reschedule write path (API level, --restore)
python scripts/probe_restart_replay.py  # write → kill process → cold restart: is the change still there?
python scripts/probe_land_plan.py       # plan-landing write path (API level, undoes by default)
python scripts/probe_import_wizard.py   # import wizard: read-only preview + commit + self-cleanup
python scripts/probe_schedule_grid.py   # weekly grid vs independent openpyxl re-count (all 16 weeks)
node ../scripts/probe_ui_newviews.cjs   # UI wiring of the two new views (local SITE; land chain self-restores)
```

## Roadmap

- [x] Conflict grading (room / teacher / whole-group core conflicts must-adjust; student affairs are permanently out of scope — there is no "light per-student conflict" tier and no force-override)
- [x] Master data CRUD + persistence + Excel ledger round-trip
- [x] Parallel batch scheduling with gray-box completion
- [x] Multi-course consolidation in class timetables (course column + cross-course clash marking)
- [x] Full-school one-click scheduling (chunked CP-SAT, 171/171 coverage, draft export)
- [x] Whole-school group-level plan in one click (per class × day, cross-class occupancy chaining)
- [x] Landing a plan as the official schedule table (diff preview → admin commit → undo, restart-replay ledger)
- [x] Visual schedule grid (weekly timetable view: 7 days × 4 periods, cross-period entries, conflict marks)
- [x] Data import wizard (read-only field-mapping preview → admin commit, shared parse code, only-adds import)
- [ ] Multi-school data isolation
- [ ] RAG knowledge base for lab materials

## Deployment

Deployed as a **single-port, same-origin** service: FastAPI serves both the API and the built
frontend, so the frontend's relative `API_BASE = '/api'` works unchanged.

```bash
# 1) Build the frontend (output goes to frontend/webui, not dist — see the two pitfalls below)
cd frontend && npm run build

# 2) Run in production form (the process reads PORT itself; defaults to 8000)
python run.py
# or with an explicit port:  PORT=8080 python run.py
```

Two pitfalls found the hard way, recorded here so nobody repeats them:

1. **Do not use `$PORT` in the start command.** Some PaaS sandboxes do not expand shell
   variables, so `--port $PORT` receives an empty value and fails outright. The port must be
   read by the process itself — that is the sole reason `run.py` exists at the repo root.
2. **Do not name the frontend output directory `dist/`.** Deployment uploads exclude common
   build-output directories, so an `index.html` inside `dist/` gets stripped and the live site
   shows a blank page. Hence `build.outDir: 'webui'` in `vite.config.ts` and
   `FRONTEND_DIST = frontend/webui` in `main.py`.

## Data & Privacy

A scheduling system necessarily touches teacher names and contact details. The boundary:

| Data | In the system? | Why |
|---|---|---|
| Teacher names | ✅ | They are the business subject of scheduling ("move teacher Wu's class to week 6") |
| Teacher phone / e-mail | ❌ **never** | Contact columns are stripped at parse time and never enter any API response; locked by `test_privacy_export.py` |
| Student-level data | ❌ **never** | The system works at (class, group) granularity; student timetables are not served by any endpoint |

What an anonymous caller can see: teacher **names**, class **names**, rooms, week / weekday /
slot, experiment project names. Nothing else.

Two datasets ship with the repo: the real `schedule.xlsx` (real teacher names, gitignored and
not committed) and a sanitized `schedule.demo.xlsx` (teachers appear as `教师A…`). Whichever is
present is used by default; `SCHEDULE_PATH` points elsewhere. Cloud capabilities (auth /
database / LLM) are gated by exact server-side Origin matching — non-allowlisted origins get 403.

## Documentation

- [中文 README (Chinese)](README.zh.md)
- [PRD V1.0 (Chinese)](docs/PRD-V1.0.md)
- [Technical Architecture V1.0 (Chinese)](docs/技术架构设计文档-V1.0.md)
- [User Guide (Chinese)](docs/产品使用指南.md)
- [Project Reflection (Chinese)](docs/项目总结与反思.md)
