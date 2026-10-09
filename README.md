# The Street Talks Back

Street furniture watches the cameras, files a grounded complaint with video proof, and ships a vehicle matchmaker who recaps the clips like a sarcastic anime-girl gossip president.

Built for the **VAST Builders Challenge** on the pre-running VSS (video understanding + search) stack. Nothing is uploaded at runtime — the app **reads** the already-indexed archive.

**Live demo:** [workshop.thecosmoslabs.com](https://workshop.thecosmoslabs.com) → **App**

---

## What it does

| You do | The app does |
|---|---|
| Press **Go live** | Bike-cam (or any camera) replays 5-second segments; the persona judges each clip |
| Open **Complaints / Offenders** | Repeat vehicles accumulate strikes; 6+ → escalate to a 311-style ticket |
| **Search** in plain language | Hybrid search over Cosmos Embed + captions |
| **Interview** a hydrant / bike lane / crosswalk | Grounded answer from search + that persona's complaint ledger |
| **Matches** | Vehicles that shared a clip or a camera get a dating briefing, spoken in first person |

A complaint is grounded: `evidence` is a **verbatim quote** from the Cosmos Reason caption, and the UI always links the clip.

## Judging map

| Criterion | Where |
|---|---|
| Understand video | Each 5s segment is judged from the Cosmos Reason caption + YOLO counts |
| Search hours in plain language | **Search** → VSS `POST /api/v1/search` |
| Ask what happened | **Interview** → VSS `POST /api/v1/agent/search-and-answer`, then voiced by the persona |
| Detect / track objects | YOLO counts **gate** the judge; offender signatures accumulate strikes |
| Trigger an action | 6+ strikes → **Escalate → ticket** (optional `WEBHOOK_URL`) |

## Sponsors (critical path, not a logo wall)

| Sponsor | Role in this app |
|---|---|
| **VAST Data** | Indexed archive (`vss-collection`), S3 Range streaming, VastDB schema `street_talks_back` (`verdicts`, `tickets`), retrieval API |
| **NVIDIA** | Cosmos Reason captions (the only evidence the judge may quote), YOLO detections (trigger), Cosmos Embed search |
| **Weights & Biases / CoreWeave** | `gpt-oss-20b` grounded judge; `Llama-3.3-70B` persona voice, tickets, interviews, matchmaker briefings; Weave traces |
| **Cursor** | Built on workshop skills (`retrieval/login`, search, agent-qa, videos, dashboard, vastdb-read/write, deploy-app-no-registry) |

Live counters: `GET /api/stack`.

## How it works

```
VSS archive ──► persona trigger (YOLO) ──► judge (fast LLM, temp 0)
                                              │
                                              ├─ no violation → store verdict, stay quiet
                                              └─ violation → voice (smart LLM) → complaint
                                                            │
                                                            ├─ 3× same offender → formal
                                                            └─ 6× → ticket eligible → escalate
```

Matchmaker: pair vehicles that co-starred in a clip or haunt the same camera → re-read captions + detections → cute / sarcastic first-person briefing. Paint is paint, never race — nicknames come from job or behavior (Mail Truck, yellow-light bandit).

## Quick start (workshop VM)

Environment is already configured. **Do not paste secrets.**

```bash
python3 -m venv /tmp/stb-venv
/tmp/stb-venv/bin/pip install -r requirements.txt
PORT=8090 AUTO_SCAN=0 /tmp/stb-venv/bin/python main.py
# open http://localhost:8090/
```

`AUTO_SCAN=0` skips a full re-judge (verdicts already live in VastDB). Set `AUTO_SCAN=1` only to re-scan.

### Cluster (the judged demo)

```bash
./deploy.sh
# https://workshop.thecosmoslabs.com → App
```

Public image `python:3.12-slim`, app code from a ConfigMap, credentials from a Secret, Ingress path `/app`.

## Configuration

All values come from the environment (workshop `config.example`). Never commit real values.

| Variable | Used for |
|---|---|
| `VSS_URL` / `INGRESS_URL` | Retrieval API (in-cluster: `video-backend-service`) |
| `VSS_USERNAME` / `VSS_PASSWORD` | Server-side JWT login; the browser never sees these |
| `WANDB_API_KEY`, `WANDB_TEAM`, `WANDB_PROJECT` | Serverless inference + Weave |
| `S3_ENDPOINT`, `ACCESS_KEY`, `SECRET_KEY`, `VASTDB_BUCKET` | Clip streaming + VastDB |
| `WEBHOOK_URL` | Optional ticket POST |
| `PORT`, `AUTO_SCAN`, `SCAN_WORKERS` | Process |

## Evaluate

Five-minute protocol: [EVAL.md](EVAL.md)

```bash
./eval.sh http://localhost:8090
./eval.sh https://team-50-app.thecosmoslabs.com/app
```

`GET /api/eval` returns the same checks as JSON (inputs, outputs, pass/fail).

## API

Auth to VSS is server-side. Behind Ingress, prefix these with `/app`.

| Method | Path | Out |
|---|---|---|
| GET | `/health` | `{ok, store, verdicts, scan}` |
| GET | `/api/eval` | pass/fail + live counts |
| GET | `/api/dashboard` | KPIs, personas, tickets |
| GET | `/api/complaints` | filterable ledger |
| GET | `/api/offenders` | repeat-offender board |
| GET | `/api/matches` | vehicle pairings |
| GET | `/api/search?q=` | ranked clips |
| POST | `/api/interview` | voiced answer + evidence |
| POST | `/api/escalate` | 311 ticket |
| POST | `/api/matches/briefing` | dating briefing from both vehicles' clips |
| POST | `/api/live/start` | SSE on `/api/events` |
| GET | `/api/stream?source=` | Range-proxied MP4 |
| GET | `/api/stack` | sponsor counters |

## Layout

| File | Role |
|---|---|
| `main.py` | HTTP server, live replay, interview, tickets, matches |
| `pipeline.py` | Personas, judge, voice, scanner |
| `vss.py` | VSS client (login, explore, search, stream) |
| `llm.py` | W&B serverless chat |
| `store.py` | VastDB + JSON fallback |
| `telemetry.py` | Weave traces + counters |
| `personas.json` | Watch lists and voices |
| `index.html` `app.js` `style.css` | UI |
| `deploy.sh` | Cluster deploy |
| `eval.sh` | One-command evaluation |

## License

MIT. Built on the VAST Builders Challenge workshop stack; the archive and credentials stay in the team environment.
