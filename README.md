# Physics Lab Schedule Agent System

> AI-powered assistant for university physics experiment scheduling: course arrangement, course selection consultation, class rescheduling, and lab tutoring.

[🇨🇳 中文版 README (Chinese)](README.zh.md)

---

## What is this?

A **multi-module intelligent system** for university physics experiment teaching:

| Module | Capability |
|--------|-----------|
| 📅 **Scheduling** | Constraint-based auto-scheduling, conflict detection, candidate plan recommendation |
| ⚡ **Parallel batch scheduling** | CP-SAT solves one class's *all experiments × student groups* in one shot (the #1 pain point from the advisor interview); missing teacher/room info is auto-completed with every assumption surfaced |
| 🗂️ **Master data** | Courses / experiments / teachers / rooms / classes as first-class CRUD entities, reverse-derived from the schedule, persisted across restarts, exportable to Excel and importable back |
| 🔄 **Rescheduling** | Template + natural language flow, graded conflict detection (room / teacher / whole-group), admin approval that actually lands in the schedule |
| 🎓 **Course Selection** | AI-powered Q&A on prerequisites, credits, eligibility |
| 🔬 **Lab Tutoring** | AI-powered Q&A on experiment principles, data processing, error analysis |

**Design principle**: rule engine guarantees correctness (conflict filtering), LLM enhances experience (intent parsing, candidate plans, consultation).

## Why this project?

- Paper research shows lab courses are rarely handled by university academic systems — scheduling relies on manual coordination.
- One schedule adjustment can trigger conflicts across multiple classes, teachers, and labs.
- Existing tools (Excel, asking class representatives) cannot efficiently resolve cross-class conflicts.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python FastAPI + openpyxl + python-constraint |
| Frontend | React + Vite + TypeScript + Tailwind CSS |
| LLM | TokenRhythm (OpenAI-compatible) / replaceable via env vars |
| Data | Excel schedule (433 entries) + student timetable |

## Quick Start

### Requirements

- Python 3.10+
- Node.js 18+
- Environment variables: `JIYUAN_LVDONG_BASE_URL`, `JIYUAN_LVDONG_API_KEY` (LLM credentials)

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
7. **Course selection / Lab tutoring** — ask AI questions with knowledge context

## Key Features

- ✅ **Graded conflict detection**: core conflicts (room / teacher / same class+group) say *what* is hit and *how to fix it* — whole-group clashes are judged per (class, group), so 47 legitimate parallel-teaching slots in the real table cause zero false positives
- ✅ **Master data as first-class citizens**: five entity tables, reverse-derived then editable; survives restarts (versioned JSON store, invalidated when the schedule changes); intro/exam projects are visible in the ledger but **never scheduled**
- ✅ **Parallel batch scheduling**: CP-SAT over the whole class; gray-box mode fills missing teachers (least-loaded first) / rooms / weeks and reports every assumption it made
- ✅ **Ledger round-trip**: export the five tables to Excel, edit offline, import back — with guards against stat-column masquerade and sharded class names
- ✅ **Reason transparency**: every excluded slot shows why
- ✅ **Natural language**: "I'm teacher Wu, move next week's class" → LLM intent parsing
- ✅ **Multi-turn follow-up**: system asks for missing info
- ✅ **SQLite persistence**: reschedule requests survive restart
- ✅ **308 backend tests + 60 frontend tests**, plus independent auditors (`audit_conflicts`, `verify_graybox`, `verify_master_roundtrip`, `verify_intro_filter`) that re-derive everything from the raw Excel with a separate parser

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
│   │   ├── agent/                  # RescheduleAgent, LLM parser, student schedule
│   │   ├── engine/                 # Constraint-based auto scheduler (CP-SAT)
│   │   └── utils/                  # Excel/PDF parsers
│   └── tests/                      # 308 pytest tests
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
| GET | `/api/reschedule/master/overview` | Master data | Five-table master data (read-only) |
| GET | `/api/reschedule/export/master-workbook` | Master data | Export editable ledger (importable back) |
| GET | `/api/reschedule/data-audit` | Dashboard | Graded conflict audit + parallel-teaching note |
| GET | `/api/reschedule/export/class-schedule` | Export | Per-class timetables (course column + cross-course clashes) |
| GET | `/api/reschedule/export/room-schedule` | Export | Per-room timetables |
| POST | `/api/reschedule/auto-schedule` | Scheduling | Full-school one-click draft (chunked, all 171 tasks) |
| POST | `/api/reschedule/auto-schedule/export` | Export | Draft workbook (master sheet + one sheet per class) |
| POST | `/api/reschedule/admin-login` | Auth | Admin login (approval desk) |
| POST | `/api/reschedule/course-select` | Selection | Course selection Q&A (LLM) |
| POST | `/api/reschedule/experiment-tutor` | Tutoring | Lab tutoring Q&A (LLM) |
| GET | `/health` | - | Health check |

## Tests

```bash
cd backend
python -m pytest tests/ -v
```

## Roadmap

- [x] Conflict grading (core conflicts must-adjust / light per-student conflicts blocked on student-timetable PII)
- [x] Master data CRUD + persistence + Excel ledger round-trip
- [x] Parallel batch scheduling with gray-box completion
- [x] Multi-course consolidation in class timetables (course column + cross-course clash marking)
- [x] Full-school one-click scheduling (chunked CP-SAT, 171/171 coverage, draft export)
- [ ] Visual schedule grid (weekly timetable view)
- [ ] Data import wizard (field mapping for different schools)
- [ ] Multi-school data isolation
- [ ] RAG knowledge base for lab materials

## Deployment & Publishing

Live app: <https://physics-scheduler.app.workbuddy.host/>

Deployed as a **single-port, same-origin** service: FastAPI serves both the API and the built
frontend, so the frontend's relative `API_BASE = '/api'` works unchanged.

```bash
# 1) Build the frontend (output MUST be frontend/webui, not dist — see the two pitfalls below)
cd frontend && npm run build

# 2) Run in production form (the process reads PORT itself; defaults to 0.0.0.0:8000)
python run.py
# or with an explicit port:  PORT=8080 python run.py
```

Publish parameters:

| Parameter | Value |
|---|---|
| directory | repository root |
| language | `python` |
| installCmd | `python3 -m pip install -r backend/requirements.txt` |
| startCmd | `python3 run.py` |
| domainPrefix | `physics-scheduler` |

### Two pitfalls (both found the hard way)

1. **The sandbox does not expand `$PORT`.** `--port $PORT` receives an empty value and fails with
   `Option '--port' requires an argument` (`SITES_START_FAILED`). The port must be read by the
   process itself — that is exactly why `run.py` exists at the repo root. Do not change it back to
   `--port $PORT`.
2. **Uploads exclude only `node_modules` / `.git` / build output — NOT `.gitignore` entries.**
   - Frontend output left in `dist/` is treated as build output and **excluded**, leaving no
     `index.html` in the sandbox. Hence `build.outDir: 'webui'` in `vite.config.ts` and
     `FRONTEND_DIST = frontend/webui` in `main.py`.
   - The gitignored `schedule.xlsx` **is still uploaded**, so it must never be the default data
     source: the default is always the sanitized `schedule.demo.xlsx`. Use
     `SCHEDULE_PATH=schedule.xlsx` locally to load real data.

### Data & privacy

- Default dataset is the sanitized `schedule.demo.xlsx` (built by `scripts/make_demo_schedule.py`;
  teachers appear as `教师A…`, no phone numbers).
- Cloud capabilities (auth / database / LLM) are gated by **exact server-side Origin match**:
  only the published domain is allowed, other origins get 403. So `localhost` can only verify the
  public data plane — WeChat login must be verified on the live domain.

> Re-publishing requires the user to explicitly ask for it ("publish / deploy"); publish consent
> does not carry over between turns.

## Documentation

- [中文 README (Chinese)](README.zh.md)
- [PRD V1.0 (Chinese)](docs/PRD-V1.0.md)
- [Technical Architecture V1.0 (Chinese)](docs/技术架构设计文档-V1.0.md)
- [User Guide (Chinese)](docs/产品使用指南.md)
- [Project Reflection (Chinese)](docs/项目总结与反思.md)
