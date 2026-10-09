# Evaluation protocol (5 minutes)

Base URL is either `http://localhost:8090` or `https://team-50-app.thecosmoslabs.com/app`.

Or run: `./eval.sh <base-url>`

## Automated checks

```bash
curl -s "$BASE/api/eval" | python3 -m json.tool
```

Every `checks[].pass` should be `true`. That endpoint documents **inputs** (archive segments, captions, detections, store) and **outputs** (complaints, offenders, tickets, scan state) with live numbers.

## UI walk (what a judge should see)

1. **Live** — camera defaults to `nyc_bike_gopro-1`. Press **Go live**. Video plays; complaints appear on the right within ~20 s. Click one → before / moment / after + caption quote + YOLO counts.
2. **Dashboard** — ~200 complaints, 3,537 segments judged, breakdown by persona and type. Not an empty shell.
3. **Search** — type `truck parked in the bike lane` (or click a chip). Ranked clips with captions. Hover to play.
4. **Interview** — Bike Lane → suggested question. Answer cites clip count and the complaint ledger; evidence videos underneath.
5. **Repeat offenders** — White USPS truck, 25×, ticket-eligible. **Escalate → ticket** drafts a 311 request with filenames. **Matches** → **Rewatch the clips** writes appearance / functionality / as-humans from those clips.

## Pass / fail rubric (Built-on tab was removed; use `/api/stack`)

| Check | Pass if |
|---|---|
| Reproducible | `/health` 200; `store` is `vastdb`; `eval.sh` exits 0 |
| Inputs documented | `/api/eval` lists segment count, caption count, detection count |
| Outputs grounded | A complaint's `evidence` is a substring of that clip's caption; stream URL plays |
| Search | `/api/search?q=truck%20in%20the%20bike%20lane` returns `results.length > 0` |
| Interview | `/api/interview` returns `answer` + `evidence[]` |
| Action | `/api/escalate` on `bike_lane` + `white usps truck` returns a ticket with `complaint_ids` |
| Sponsors | `GET /api/stack` shows VAST + NVIDIA + W&B with non-zero lifetime counters |

## What not to grade as a bug

- First interview / match briefing takes 10–25 s (live LLM).
- Session counters on **Built on** reset to 0 after a pod restart; lifetime verdicts do not.
- `i24_corridor` may have 0 complaints — the trigger is conservative on highway footage.
