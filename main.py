"""The Street Talks Back — infrastructure that files grounded complaints about what the cameras see.

Stdlib-only HTTP server: dashboard API, live replay (SSE), persona interviews, escalation tickets, video proxy.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import llm
import pipeline
import telemetry
from store import Store
from vss import VSSClient

ROOT = Path(__file__).parent
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")

vss = VSSClient()
store = Store()
scanner = pipeline.Scanner(vss, store, workers=int(os.environ.get("SCAN_WORKERS", "10")))

SEGMENTS: list[dict] = []
SEG_BY_SOURCE: dict[str, dict] = {}
SEG_BY_VIDEO: dict[str, list[dict]] = defaultdict(list)
_seg_lock = threading.Lock()


def load_segments(force: bool = False) -> list[dict]:
    global SEGMENTS
    with _seg_lock:
        if SEGMENTS and not force:
            return SEGMENTS
        segs = scanner.segments or vss.all_segments()
        SEGMENTS = segs
        SEG_BY_SOURCE.clear()
        SEG_BY_VIDEO.clear()
        for s in segs:
            SEG_BY_SOURCE[s["source"]] = s
            SEG_BY_VIDEO[s["original_video"]].append(s)
        for v in SEG_BY_VIDEO.values():
            v.sort(key=lambda s: s.get("segment_number") or 0)
        return SEGMENTS


# ---------------------------------------------------------------- live replay (SSE)
class Broadcaster:
    def __init__(self) -> None:
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.history: list[dict] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    def publish(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        with self.lock:
            self.history = (self.history + [event])[-60:]
            for q in list(self.clients):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass


bus = Broadcaster()


class LiveReplay:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state: dict = {"running": False, "camera_id": None, "speed": 1.0, "index": 0, "total": 0, "current": None}

    def start(self, camera_id: str, speed: float = 1.0, start_at: int = 0) -> dict:
        self.stop()
        segs = [s for s in load_segments() if s.get("camera_id") == camera_id]
        segs.sort(key=lambda s: (s.get("upload_timestamp") or "", s.get("segment_number") or 0))
        self.stop_flag = threading.Event()
        self.state.update(running=True, camera_id=camera_id, speed=speed, index=start_at, total=len(segs), current=None)
        self.thread = threading.Thread(target=self._run, args=(segs, speed, start_at, self.stop_flag), daemon=True)
        self.thread.start()
        return self.state

    def stop(self) -> None:
        if self.thread and self.thread.is_alive():
            self.stop_flag.set()
            self.thread.join(timeout=2)
        self.state["running"] = False

    def _run(self, segs: list[dict], speed: float, start_at: int, flag: threading.Event) -> None:
        persona = pipeline.CAMERA_TO_PERSONA.get(self.state["camera_id"] or "")
        bus.publish({"type": "live_started", "camera_id": self.state["camera_id"], "persona": persona["id"] if persona else None, "total": len(segs)})
        for i in range(start_at, len(segs)):
            if flag.is_set():
                break
            s = segs[i]
            self.state.update(index=i, current=s["source"])
            bus.publish({"type": "segment", "index": i, "total": len(segs), "segment": public_segment(s)})
            t0 = time.time()
            try:
                row = pipeline.judge_segment(s, store)
            except Exception as e:
                row = None
                bus.publish({"type": "error", "message": str(e)})
            if row and row.get("violation"):
                bus.publish({"type": "complaint", "complaint": decorate(row)})
            # keep real-time pacing: 5 s per segment divided by speed, minus inference time
            remaining = max(0.0, (float(s.get("duration") or 5.0) / max(speed, 0.1)) - (time.time() - t0))
            if flag.wait(remaining):
                break
        self.state["running"] = False
        bus.publish({"type": "live_stopped"})


live = LiveReplay()


# ---------------------------------------------------------------- helpers
def public_segment(s: dict) -> dict:
    return {
        "source": s["source"], "filename": s.get("filename"), "original_video": s.get("original_video"),
        "camera_id": s.get("camera_id"), "location": s.get("location"), "segment_number": s.get("segment_number"),
        "total_segments": s.get("total_segments"), "start": s.get("segment_start_sec"), "end": s.get("segment_end_sec"),
        "caption": s.get("reasoning_content"), "object_counts": pipeline._counts(s),
        "persona": (pipeline.persona_for(s) or {}).get("id"),
    }


def decorate(row: dict) -> dict:
    p = pipeline.PERSONA_BY_ID.get(row["persona"], {})
    vt = pipeline.VIOLATIONS.get(row.get("violation_type") or "", {})
    out = dict(row)
    out["persona_name"] = p.get("name")
    out["persona_emoji"] = p.get("emoji")
    out["violation_label"] = vt.get("label") or row.get("violation_type")
    out["rule"] = vt.get("rule")
    out["stream_url"] = "/api/stream?" + urllib.parse.urlencode({"source": row["source"]})
    out["level"] = escalation_level(row)
    return out


def escalation_level(row: dict) -> dict:
    """Ladder: grumble (1-2) -> formal complaint (3-5) -> ticket-eligible (6+) for same persona+type, or persona+offender."""
    comps = store.complaints()
    same_type = sum(1 for c in comps if c["persona"] == row["persona"] and c.get("violation_type") == row.get("violation_type"))
    same_offender = sum(1 for c in comps if row.get("offender") and c["persona"] == row["persona"] and c.get("offender") == row.get("offender"))
    n = max(same_type, same_offender)
    name = "grumble" if n < 3 else ("formal" if n < 6 else "ticket")
    return {"name": name, "count": n, "same_type": same_type, "same_offender": same_offender}


def dashboard() -> dict:
    comps = store.complaints()
    by_persona: dict[str, dict] = {}
    for p in pipeline.PERSONAS:
        rows = [c for c in comps if c["persona"] == p["id"]]
        by_persona[p["id"]] = {
            "id": p["id"], "name": p["name"], "emoji": p["emoji"], "tagline": p["tagline"], "cameras": p["cameras"],
            "count": len(rows), "avg_severity": round(sum(c["severity"] for c in rows) / len(rows), 2) if rows else 0,
            "high": sum(1 for c in rows if c["severity"] >= 4),
            "types": Counter(c["violation_type"] for c in rows).most_common(5),
            "scanned": sum(1 for v in store.verdicts.values() if v["persona"] == p["id"]),
        }
    by_type = Counter(c["violation_type"] for c in comps)
    offenders = top_offenders(comps)
    tickets = sorted(store.tickets.values(), key=lambda t: t["created_at"], reverse=True)
    return {
        "totals": {
            "segments_scanned": len(store.verdicts), "complaints": len(comps),
            "high_severity": sum(1 for c in comps if c["severity"] >= 4),
            "offenders": len(offenders), "tickets": len(tickets),
            "ticket_eligible": sum(1 for o in offenders if o["count"] >= 6),
            "archive_segments": len(SEGMENTS) or None,
        },
        "by_persona": list(by_persona.values()),
        "by_type": [{"type": t, "label": pipeline.VIOLATIONS.get(t, {}).get("label", t), "count": n, "rule": pipeline.VIOLATIONS.get(t, {}).get("rule")} for t, n in by_type.most_common()],
        "by_severity": [sum(1 for c in comps if c["severity"] == s) for s in range(1, 6)],
        "by_camera": Counter(c["camera_id"] for c in comps).most_common(),
        "offenders": offenders[:12],
        "tickets": tickets[:10],
        "scan": scanner.status,
        "live": live.state,
        "store_backend": store.backend,
    }


def eval_report() -> dict:
    """Machine-readable inputs / outputs / pass-fail for a judge. No secrets."""
    load_segments()
    d = dashboard()
    t = d["totals"]
    caps = sum(1 for s in SEGMENTS if s.get("reasoning_content"))
    yolo = sum(sum(pipeline._counts(s).values()) for s in SEGMENTS)
    checks = [
        {"id": "health", "name": "Process is up", "pass": True, "got": "ok"},
        {"id": "store", "name": "VastDB persistence", "pass": store.backend == "vastdb", "got": store.backend},
        {"id": "segments", "name": "Archive segments loaded", "pass": (len(SEGMENTS) or t["segments_scanned"] or 0) > 0,
         "got": len(SEGMENTS) or t.get("archive_segments")},
        {"id": "captions", "name": "Cosmos Reason captions present", "pass": caps > 0, "got": caps},
        {"id": "detections", "name": "YOLO detections present", "pass": yolo > 0, "got": yolo},
        {"id": "complaints", "name": "Grounded complaints filed", "pass": t["complaints"] > 0, "got": t["complaints"]},
        {"id": "offenders", "name": "Repeat-offender grouping", "pass": t["offenders"] > 0, "got": t["offenders"]},
        {"id": "scan", "name": "Archive scan finished or idle with cache",
         "pass": scanner.status["state"] in ("done", "idle") or t["segments_scanned"] > 0,
         "got": scanner.status["state"]},
    ]
    return {
        "ok": all(c["pass"] for c in checks),
        "inputs": {
            "archive_segments": len(SEGMENTS),
            "cosmos_reason_captions": caps,
            "yolo_detections": yolo,
            "personas": len(pipeline.PERSONAS),
            "cameras": sorted({s.get("camera_id") for s in SEGMENTS if s.get("camera_id")}),
            "store": store.backend,
        },
        "outputs": {
            "verdicts": len(store.verdicts),
            "complaints": t["complaints"],
            "high_severity": t["high_severity"],
            "offenders": t["offenders"],
            "tickets": t["tickets"],
            "ticket_eligible": t["ticket_eligible"],
        },
        "checks": checks,
        "eval_steps": [
            "GET /health → ok",
            "GET /api/eval → all checks pass",
            "UI Live: Go live on nyc_bike_gopro-1, click a complaint, confirm caption quote + video",
            "GET /api/search?q=truck+in+the+bike+lane → results.length > 0",
            "POST /api/interview {persona:bike_lane, question} → answer + evidence",
            "POST /api/escalate {persona:bike_lane, offender:white usps truck} → ticket with complaint_ids",
            "UI Matches: rewatch clips → appearance, functionality, as-humans briefing",
        ],
    }


def stack() -> dict:
    """What this app pulls from each part of the sponsor stack, with live counters."""
    t = telemetry.snapshot()
    c = t["counters"]
    with _seg_lock:
        segs = list(SEGMENTS)
    captions = sum(1 for s in segs if s.get("reasoning_content"))
    yolo_total = sum(sum(pipeline._counts(s).values()) for s in segs)
    yolo_classes = Counter()
    for s in segs:
        for k, v in pipeline._counts(s).items():
            yolo_classes[k] += v
    cameras = len({s.get("camera_id") for s in segs if s.get("camera_id")})
    model_calls = {k.removeprefix("wandb.calls."): v for k, v in c.items() if k.startswith("wandb.calls.")}
    return {
        "uptime_sec": t["uptime_sec"],
        "sponsors": [
            {
                "id": "vast", "name": "VAST Data", "role": "Storage, database, and the retrieval API",
                "facts": [
                    {"label": "VastDB rows written", "value": len(store.verdicts) + len(store.tickets), "note": f"schema street_talks_back · tables verdicts, tickets ({store.backend})"},
                    {"label": "Archive segments read from vss-collection", "value": len(segs), "note": f"{cameras} cameras"},
                    {"label": "Clips streamed from S3", "value": c.get("s3.streams", 0), "note": f"{c.get('s3.bytes_streamed', 0) / 1e6:.1f} MB via Range proxy"},
                    {"label": "VSS API calls", "value": c.get("vss.calls", 0), "note": ", ".join(f"{k.removeprefix('vss.')} ×{v}" for k, v in sorted(c.items()) if k.startswith("vss.") and k != "vss.calls")},
                ],
            },
            {
                "id": "nvidia", "name": "NVIDIA", "role": "Cosmos Reason captions, Cosmos Embed search, YOLO detections (VSS Blueprint)",
                "facts": [
                    {"label": "Cosmos Reason captions in evidence", "value": captions, "note": f"{c.get('nvidia.cosmos_captions_read', 0)} fed to the judge this session"},
                    {"label": "YOLO detections across archive", "value": yolo_total, "note": ", ".join(f"{k} {v}" for k, v in yolo_classes.most_common(5))},
                    {"label": "Cosmos Embed semantic searches", "value": c.get("vss.search", 0) + c.get("vss.agent/search-and-answer", 0), "note": "search + interview"},
                ],
            },
            {
                "id": "wandb", "name": "Weights & Biases by CoreWeave", "role": "Serverless inference for judging, voice, interviews, tickets; Weave tracing",
                "facts": [
                    {"label": "LLM verdicts stored (all time)", "value": len(store.verdicts), "note": f"{len(store.complaints())} voiced complaints · {len(store.tickets)} tickets drafted · models {llm.FAST_MODEL} + {llm.SMART_MODEL}"},
                    {"label": "LLM calls this session", "value": c.get("wandb.calls", 0), "note": ", ".join(f"{k} ×{v}" for k, v in sorted(model_calls.items(), key=lambda kv: -kv[1]))},
                    {"label": "Tokens", "value": c.get("wandb.prompt_tokens", 0) + c.get("wandb.completion_tokens", 0), "note": f"{c.get('wandb.prompt_tokens', 0)} in / {c.get('wandb.completion_tokens', 0)} out"},
                    {"label": "Weave traces", "value": "on" if t["weave"]["enabled"] else "off", "note": "ops: wandb.chat_completion, persona.judge_segment, persona.voice_complaint, persona.interview, persona.escalate_ticket", "url": t["weave"]["url"]},
                ],
            },
            {
                "id": "cursor", "name": "Cursor", "role": "Built end to end in Cursor on the workshop skills",
                "facts": [
                    {"label": "Skills used", "value": 8, "note": "retrieval/login, search, agent-qa, videos, dashboard, vastdb-read, vastdb-write, deployment/deploy-app-no-registry"},
                ],
            },
        ],
    }


def top_offenders(comps: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for c in comps:
        if c.get("offender"):
            groups[(c["persona"], c["offender"])].append(c)
    out = []
    for (pid, off), rows in groups.items():
        p = pipeline.PERSONA_BY_ID[pid]
        out.append({
            "persona": pid, "persona_name": p["name"], "persona_emoji": p["emoji"], "offender": off,
            "count": len(rows), "max_severity": max(r["severity"] for r in rows),
            "types": Counter(r["violation_type"] for r in rows).most_common(3),
            "complaint_ids": [r["id"] for r in sorted(rows, key=lambda r: -r["severity"])][:12],
            "level": "ticket" if len(rows) >= 6 else ("formal" if len(rows) >= 3 else "grumble"),
            "ticketed": any(set(r["id"] for r in rows) & set(t["complaint_ids"]) for t in store.tickets.values()),
        })
    out.sort(key=lambda o: (-o["count"], -o["max_severity"]))
    return out


def neighbors(row: dict) -> dict:
    load_segments()
    segs = SEG_BY_VIDEO.get(row["original_video"], [])
    idx = next((i for i, s in enumerate(segs) if s["source"] == row["source"]), None)
    if idx is None:
        return {"before": None, "after": None}
    return {
        "before": public_segment(segs[idx - 1]) if idx > 0 else None,
        "after": public_segment(segs[idx + 1]) if idx + 1 < len(segs) else None,
    }


@telemetry.op("persona.interview")
def interview(persona_id: str, question: str) -> dict:
    p = pipeline.PERSONA_BY_ID[persona_id]
    filters = {"camera_id": p["cameras"][0]} if len(p["cameras"]) == 1 else {"location": p["location"]}
    answer = vss.search_and_answer(question, metadata_filters=filters, top_k=10)
    evidence = (answer.get("evidence") or {})
    chunks = evidence.get("chunks") or []
    own = [c for c in store.complaints() if c["persona"] == persona_id]
    related = [c for c in own if any(c["original_video"] == ch.get("original_video") for ch in chunks)][:6]
    ledger = "\n".join(f"- [{c['violation_type']}] sev {c['severity']}: {c['evidence']}" for c in related) or "- (no filed complaints on these clips yet)"
    type_tally = ", ".join(f"{t} x{n}" for t, n in Counter(c["violation_type"] for c in own).most_common(5)) or "none"
    offender_tally = ", ".join(f"{o} x{n}" for o, n in Counter(c["offender"] for c in own if c.get("offender")).most_common(5)) or "none"
    voiced = llm.chat([
        {"role": "system", "content": f"You are {p['name']}, {p['voice']} Answer the question in first person using ONLY the facts below. If the facts don't cover it, say so plainly. 2-4 sentences, max 600 characters. Cite how many clips you looked at and, when relevant, how many complaints you have filed."},
        {"role": "user", "content": f"Question: {question}\n\nWhat the archive search found (grounded answer from {len(chunks)} clips):\n{answer.get('answer') or (answer.get('llm_synthesis') or {}).get('response') or 'nothing'}\n\nMy full complaint ledger so far: {len(own)} complaints filed. Violation types: {type_tally}. Repeat offenders: {offender_tally}.\n\nComplaints I have already filed on the clips the search returned:\n{ledger}"},
    ], model=llm.SMART_MODEL, temperature=0.6, max_tokens=400)
    return {
        "persona": persona_id, "question": question, "answer": voiced.strip(),
        "grounded_answer": answer.get("answer"),
        "evidence": [{
            "original_video": ch.get("original_video"), "filename": (ch.get("original_video") or "").rsplit("/", 1)[-1],
            "source": ch.get("preview_source"), "start": ch.get("best_match_start_sec"), "end": ch.get("best_match_end_sec"),
            "score": ch.get("similarity_score"), "caption": ch.get("reasoning_content"),
            "stream_url": "/api/stream?" + urllib.parse.urlencode({"source": ch.get("preview_source") or ""}),
        } for ch in chunks[:8]],
        "related_complaints": [decorate(c) for c in related],
    }


@telemetry.op("persona.escalate_ticket")
def escalate(persona_id: str, offender: str | None, violation_type: str | None, complaint_ids: list[str] | None) -> dict:
    p = pipeline.PERSONA_BY_ID[persona_id]
    comps = [c for c in store.complaints() if c["persona"] == persona_id]
    if complaint_ids:
        comps = [c for c in comps if c["id"] in set(complaint_ids)]
    elif offender:
        comps = [c for c in comps if c.get("offender") == offender]
    elif violation_type:
        comps = [c for c in comps if c.get("violation_type") == violation_type]
    comps = sorted(comps, key=lambda c: -c["severity"])[:12]
    if not comps:
        raise ValueError("no complaints match that escalation")
    vt = violation_type or Counter(c["violation_type"] for c in comps).most_common(1)[0][0]
    rule = pipeline.VIOLATIONS.get(vt, {}).get("rule", "")
    evidence = "\n".join(
        f"- {c['filename']} @ {c['segment_start']:.0f}-{c['segment_end']:.0f}s (cam {c['camera_id']}, sev {c['severity']}, conf {c['confidence']}): \"{c['evidence']}\""
        for c in comps
    )
    draft = llm.chat_json([
        {"role": "system", "content": "You draft municipal 311-style service requests from video evidence. Formal, factual, no speculation beyond the evidence lines. Return JSON {\"title\": str, \"body\": str, \"requested_action\": str}. body is plain text with sections: Summary, Location/Camera, Evidence (one line per clip, keep filenames), Pattern, Complainant statement (one sentence quoted in the persona's voice)."},
        {"role": "user", "content": f"Complainant persona: {p['name']} ({p['tagline']}). Voice: {p['voice']}\nViolation category: {pipeline.VIOLATIONS.get(vt, {}).get('label', vt)} — reference {rule}\nOffender signature: {offender or 'multiple / unidentified'}\nCameras: {', '.join(sorted(set(c['camera_id'] for c in comps)))}\nOccurrences in evidence: {len(comps)}\n\nEvidence:\n{evidence}"},
    ], model=llm.SMART_MODEL, temperature=0.3, max_tokens=900)
    ticket = {
        "id": "T-" + uuid.uuid4().hex[:8].upper(), "persona": persona_id, "offender": offender or "", "violation_type": vt,
        "complaint_ids": [c["id"] for c in comps], "title": str(draft.get("title") or f"{p['name']}: {vt}")[:200],
        "body": str(draft.get("body") or draft.get("_raw") or "")[:6000] + ("\n\nRequested action: " + str(draft["requested_action"]) if draft.get("requested_action") else ""),
        "severity": max(c["severity"] for c in comps), "webhook_status": "not configured",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if WEBHOOK_URL:
        try:
            payload = json.dumps({"text": f"*{ticket['title']}*\n{ticket['body'][:2500]}", "ticket": ticket}).encode()
            req = urllib.request.Request(WEBHOOK_URL, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                ticket["webhook_status"] = f"sent ({r.status})"
        except Exception as e:
            ticket["webhook_status"] = f"failed: {e}"[:120]
    store.add_ticket(ticket)
    bus.publish({"type": "ticket", "ticket": ticket})
    return ticket


_MATCH_CACHE: dict[tuple[str, str], dict] = {}
_VEHICLE_CACHE: dict[tuple[str, str], dict] = {}

MATCHMAKER_VOICE = (
    "You ARE a cute moe anime girl — tsundere gossip-club president in a black t-shirt that says VEHICLE MATCHMAKER. "
    "EVERY field is you talking out loud. First person. A dating-show recap, not a report, not Wikipedia, not a cop. "
    "Cute AND sarcastic. Tease them. A little mean, never cruel. Funny. Dramatic sighs in words ('...yeah.'). "
    "Voice ticks — use 2-4 per briefing, not all at once: okay so / listen / I'm sorry but / nuh-uh / ugh / wait. wait. / "
    "I can't even / like, / honestly? / don't look at me like that. "
    "Do NOT narrate ('The vehicle appears to be'). Do this: \"Okay so this SUV walked in like it pays rent here.\" "
    "NEVER write 'Vehicle A' or 'Vehicle B' — use the type, badge, or a job/behavior nickname. "
    "NEVER write 'If they were human beings'. "
    "English only. Zero or one emoji total. No uwu, no nya, no Japanese filler. No municipal, no corporate, no 'in conclusion'. "
    "Facts about colour, body, what they DID still have to come from the captions/detections. Invent personality, not pixels. "
    "PAINT IS PAINT, NEVER RACE. No Blackie, Whitey, Darkie, or any colour-as-identity nickname. "
    "Do not cast a vehicle as a race, ethnicity, nationality, or skin tone. Do not write 'the black guy', 'the white girl', "
    "or date-cast them as Black/White/Asian/Latino/etc. Colour words only describe the vehicle: 'the black SUV', 'the white mail truck'. "
    "Nicknames come from JOB, body style, or what they DID (Mail Truck, Yellow-light bandit, Sir Honks-a-Lot) — never from colour-as-a-person. "
    "When you pretend they are people, give them personalities and jobs, not races. "
    "Example vibe (do not copy the facts): headline \"Mail Truck vs SUV: I ship it and I hate that I ship it\". "
    "appearance \"Okay so the SUV is tinted windows and main-character energy, like it practiced that parked-in-the-lane pose in a mirror.\" "
    "as_humans \"Okay so if these two were people? The SUV texts 'you up?' at a red light. Mail Truck is the overachiever in a uniform who parks on YOUR plans. He texts first. I'd give it three dates. Maybe.\" "
    "verdict \"I'm rooting for them. I'm also taking notes for the breakup.\""
)

_RACE_BAD = re.compile(
    r"\b(?:blackie|whitey|darkie|"
    r"(?:black|white|brown)\s+(?:guy|girl|dude|man|woman|boy|lady|person|date|boyfriend|girlfriend)|"
    r"african[-\s]?american|caucasian|hispanic|latino|latina|"
    r"skin\s*tone|complexion|\bethnic(?:ity)?\b|\bracial\b|"
    r"if\s+they\s+were\s+(?:a\s+)?(?:black|white|asian|hispanic|latino))\b",
    re.I,
)


def _looks_racialized(blob: str) -> bool:
    return bool(_RACE_BAD.search(blob or ""))


def _matchmaker_json(system: str, user: str, *, temperature: float, max_tokens: int) -> dict:
    out = llm.chat_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        model=llm.SMART_MODEL, temperature=temperature, max_tokens=max_tokens,
    )
    if not _looks_racialized(json.dumps(out, default=str)):
        return out
    return llm.chat_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": (
                user + "\n\nREWRITE. Last draft used paint colour as a racial nickname or cast a vehicle as a race. "
                "Same facts. Still a cute sarcastic anime girl talking. Nicknames from job/behavior only. Paint is paint."
            )},
        ],
        model=llm.SMART_MODEL, temperature=0.45, max_tokens=max_tokens,
    )


def _clip_from_complaint(c: dict, n: int = 0, who: str = "") -> dict:
    load_segments()
    seg = SEG_BY_SOURCE.get(c["source"], {})
    counts = pipeline._counts(seg) if seg else {}
    return {
        "n": n, "who": who, "id": c.get("id"), "source": c["source"], "filename": c.get("filename"),
        "start": c.get("segment_start") or c.get("segment_start_sec") or 0,
        "end": c.get("segment_end") or c.get("segment_end_sec") or 0,
        "camera_id": c.get("camera_id"), "severity": c.get("severity") or 0,
        "violation": pipeline.VIOLATIONS.get(c.get("violation_type") or "", {}).get("label", c.get("violation_type") or ""),
        "caption": (seg.get("reasoning_content") or c.get("evidence") or c.get("caption") or "")[:900],
        "counts": ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:6]),
        "stream_url": "/api/stream?" + urllib.parse.urlencode({"source": c["source"]}),
        "kind": "complaint",
    }


def _clip_from_search(r: dict, n: int = 0, who: str = "") -> dict:
    return {
        "n": n, "who": who, "id": None, "source": r.get("source"), "filename": r.get("filename"),
        "start": r.get("segment_start_sec") or 0, "end": r.get("segment_end_sec") or 0,
        "camera_id": r.get("camera_id"), "severity": 0, "violation": "",
        "caption": (r.get("reasoning_content") or "")[:900], "counts": "",
        "stream_url": "/api/stream?" + urllib.parse.urlencode({"source": r.get("source") or ""}),
        "kind": "search", "score": round(float(r.get("similarity_score") or 0), 3),
    }


def _pick_rows(rows: list[dict], shared: set, n: int = 5) -> list[dict]:
    shared_rows = [r for r in rows if r.get("original_video") in shared]
    rest = [r for r in rows if r.get("original_video") not in shared]
    ordered = sorted(shared_rows, key=lambda r: -r.get("severity", 0)) + sorted(rest, key=lambda r: -r.get("severity", 0))
    seen: set[str] = set()
    out: list[dict] = []
    for r in ordered:
        if r["source"] in seen:
            continue
        seen.add(r["source"])
        out.append(r)
        if len(out) >= n:
            break
    return out


_NOT_VEHICLE = (
    "pedestrian", "person", "people", "camera", "crowd", "worker", "cyclist",
    "barricade", "barrier", "cone", "barrel", "stroller", "handbag",
)
_VEHICLE_HINT = (
    "truck", "van", "suv", "sedan", "taxi", "cab", "bus", "car", "pickup",
    "forklift", "waymo", "usps", "ups", "motorcycle", "scooter", "bicycle",
    "shuttle", "lorry", "semi", "coupe", "hatchback", "minivan",
)
_COLORS = ("black", "white", "yellow", "red", "blue", "silver", "gray", "grey", "green", "orange", "brown")


def _is_vehicle(name: str) -> bool:
    n = (name or "").lower().strip()
    if not n or n in ("unidentified", "unknown", "n/a"):
        return False
    if any(w in n for w in _NOT_VEHICLE):
        return False
    return any(w in n for w in _VEHICLE_HINT)


def _split_vehicles(name: str) -> list[str]:
    """'yellow taxi and white truck' → two vehicles when both sides look like vehicles."""
    n = (name or "").strip()
    if not n:
        return []
    parts = [p.strip() for p in re.split(r"\s+(?:and|&|/)\s+", n, flags=re.I) if p.strip()]
    if len(parts) >= 2 and all(_is_vehicle(p) for p in parts):
        return parts
    return [n] if _is_vehicle(n) else []


def _first_color(n: str) -> str:
    for c in _COLORS:
        if re.search(rf"\b{c}\b", n):
            return "gray" if c == "grey" else c
    return ""


def _vehicle_key(name: str) -> str | None:
    """Collapse 'white united states usps truck' / 'two white USPS trucks' into one dating profile."""
    n = re.sub(r"[^a-z0-9\s]", " ", (name or "").lower())
    n = n.replace("dark colored", "black").replace("light colored", "white").replace("dark coloured", "black")
    n = re.sub(r"\b(two|three|four|several|multiple|\d+)\s+(riders?|people|persons?)\b", " ", n)
    n = re.sub(r"\b(two|three|four|several|multiple)\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n.startswith("dark "):
        n = "black " + n[5:]
    if not n or not _is_vehicle(n):
        return None
    if "usps" in n or "postal" in n or "mail truck" in n:
        return "usps truck"
    if re.search(r"\bups\b", n):
        return "ups truck"
    if "waymo" in n:
        return "waymo"
    if "taxi" in n or re.search(r"\bcab\b", n):
        color = _first_color(n)
        return f"{color} taxi".strip() if color else "taxi"
    if "forklift" in n:
        if "atlas" in n:
            return "atlas forklift"
        color = _first_color(n)
        return f"{color} forklift".strip() if color else "forklift"
    if "shuttle" in n:
        color = _first_color(n)
        return f"{color} shuttle".strip() if color else "shuttle"
    body = next((b for b in (
        "box truck", "utility truck", "pickup", "motorcycle", "bicycle", "scooter",
        "suv", "sedan", "van", "bus", "truck", "car",
    ) if b in n), None)
    if not body:
        return n[:60]
    if body == "pickup":
        body = "pickup truck"
    color = _first_color(n)
    key = f"{color} {body}".strip() if color else body
    if key in {"truck", "car", "bus", "van", "sedan"}:
        return None  # unnamed body type — too vague to pair
    return key


def _actors_list(c: dict) -> list[str]:
    a = c.get("actors") or []
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except json.JSONDecodeError:
            a = [a]
    return [str(x) for x in a if x]


def _vehicle_names_on(c: dict) -> list[str]:
    names: list[str] = []
    for raw in [c.get("offender"), *_actors_list(c)]:
        names.extend(_split_vehicles(str(raw or "")))
    # unique, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        k = n.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def _pretty_name(key: str, rows: list[dict]) -> str:
    labels = []
    for c in rows:
        off = (c.get("offender") or "").strip()
        if _vehicle_key(off) == key:
            labels.append(off)
        for n in _vehicle_names_on(c):
            if _vehicle_key(n) == key:
                labels.append(n)
    if labels:
        return Counter(labels).most_common(1)[0][0]
    return key


def _complaints_for_vehicle(name: str) -> list[dict]:
    key = _vehicle_key(name) or (name or "").lower().strip()
    own, extra = [], []
    for c in store.complaints():
        if _vehicle_key(c.get("offender") or "") == key:
            own.append(c)
        elif any(_vehicle_key(n) == key for n in _vehicle_names_on(c)):
            extra.append(c)
    return own or extra


def vehicle_pairs(limit: int = 36) -> list[dict]:
    """Pair vehicles that co-star in a clip, share a parent video, share a camera, or both have a ledger."""
    comps = store.complaints()
    by_key: dict[str, list[dict]] = defaultdict(list)
    by_vid_keys: dict[str, set[str]] = defaultdict(set)
    costars: dict[tuple[str, str], int] = defaultdict(int)
    for c in comps:
        keys: list[str] = []
        for n in _vehicle_names_on(c):
            k = _vehicle_key(n)
            if k:
                keys.append(k)
        keys = list(dict.fromkeys(keys))
        for k in keys:
            by_key[k].append(c)
        vid = c.get("original_video") or ""
        if vid:
            by_vid_keys[vid].update(keys)
        for a, b in combinations(sorted(keys), 2):
            costars[(a, b)] += 1
    if not by_key:
        return []

    meta: dict[tuple[str, str], dict] = {}

    def bucket(a: str, b: str) -> dict:
        key = (a, b) if a < b else (b, a)
        return meta.setdefault(key, {"shared": set(), "cameras": set(), "kind": "citywide", "costars": 0})

    for vid, keys in by_vid_keys.items():
        for a, b in combinations(sorted(keys), 2):
            m = bucket(a, b)
            m["shared"].add(vid)
            m["kind"] = "met"

    for (a, b), n in costars.items():
        m = bucket(a, b)
        m["costars"] = n
        if m["kind"] != "met":
            m["kind"] = "met"

    # same camera, no top-N cap
    by_cam: dict[str, set[str]] = defaultdict(set)
    for k, rows in by_key.items():
        for r in rows:
            cam = r.get("camera_id") or ""
            if cam:
                by_cam[cam].add(k)
    for cam, keys in by_cam.items():
        for a, b in combinations(sorted(keys), 2):
            m = bucket(a, b)
            m["cameras"].add(cam)
            if m["kind"] == "citywide":
                m["kind"] = "same_block"

    # remaining frequent vehicles still get a citywide card so the board isn't empty
    frequent = [k for k, rows in sorted(by_key.items(), key=lambda kv: -len(kv[1])) if len(rows) >= 2][:16]
    for a, b in combinations(frequent, 2):
        bucket(a, b)

    out = []
    for (a, b), m in meta.items():
        ra, rb = by_key.get(a) or [], by_key.get(b) or []
        if not ra or not rb:
            continue
        pa = sorted(ra, key=lambda r: -r.get("severity", 0))[0]
        pb = sorted(rb, key=lambda r: -r.get("severity", 0))[0]
        shared_n = len(m["shared"])
        da, db = _pretty_name(a, ra), _pretty_name(b, rb)
        if da.lower() == db.lower():
            continue
        out.append({
            "a": da, "b": db, "kind": m["kind"], "shared_videos": shared_n,
            "count_a": len(ra), "count_b": len(rb),
            "cameras": sorted(c for c in m["cameras"] if c),
            "score": shared_n * 20 + m["costars"] * 12 + min(len(ra), len(rb)) * 6 + (len(ra) + len(rb)),
            "preview_a": decorate(pa), "preview_b": decorate(pb),
        })
    out.sort(key=lambda x: (-x["score"], -x["shared_videos"], -(x["count_a"] + x["count_b"])))
    return out[:limit]


@telemetry.op("persona.vehicle_match")
def vehicle_briefing(a: str, b: str, refresh: bool = False) -> dict:
    """Dating briefing for two vehicles. The LLM re-reads Cosmos captions of each clip."""
    a, b = sorted((a.strip(), b.strip()))
    key = (a, b)
    if not refresh and key in _VEHICLE_CACHE:
        return _VEHICLE_CACHE[key]
    ca = _complaints_for_vehicle(a)
    cb = _complaints_for_vehicle(b)
    if not ca or not cb:
        raise ValueError("need filed complaints on both vehicles to write a match")
    load_segments()
    vids_a = {c["original_video"] for c in ca}
    vids_b = {c["original_video"] for c in cb}
    shared = vids_a & vids_b
    rows_a, rows_b = _pick_rows(ca, shared, 5), _pick_rows(cb, shared, 5)
    extra_a = extra_b = []
    try:
        extra_a = (vss.search(a, top_k=5, min_similarity=0.18).get("results") or [])[:4]
    except Exception as e:
        print(f"[match] search {a}: {e}", flush=True)
    try:
        extra_b = (vss.search(b, top_k=5, min_similarity=0.18).get("results") or [])[:4]
    except Exception as e:
        print(f"[match] search {b}: {e}", flush=True)
    telemetry.bump("nvidia.cosmos_captions_read", len(rows_a) + len(rows_b) + len(extra_a) + len(extra_b))

    def pack(label: str, rows: list[dict], extras: list[dict]) -> str:
        parts = []
        for i, c in enumerate(rows, 1):
            cl = _clip_from_complaint(c, i, label)
            parts.append(
                f"{label} clip {i} — {cl['filename']} @ {cl['start']:.0f}-{cl['end']:.0f}s · {cl['violation']} · sev {cl['severity']}/5\n"
                f"Caption: {cl['caption']}\nDetections: {cl['counts'] or 'n/a'}"
            )
        seen = {c["source"] for c in rows}
        k = 1
        for r in extras:
            if r.get("source") in seen:
                continue
            parts.append(
                f"{label} extra search hit {k} (similarity {round(float(r.get('similarity_score') or 0), 2)}): "
                f"{(r.get('reasoning_content') or '')[:700]}"
            )
            k += 1
        return "\n\n".join(parts)

    out = _matchmaker_json(
        MATCHMAKER_VOICE
        + " You have WATCHED every clip below — captions are Cosmos Reason, detections are YOLO. "
        "Every JSON string is spoken in your cute sarcastic girl voice — no field may sound like a police report. "
        "clips MUST include one sarcastic-cute whisper for every A clip N and every B clip N, in order. "
        "appearance_a/b: 2-3 spoken sentences on how that vehicle LOOKS as a vehicle (paint, badge, body) — never as human skin. "
        "functionality: what each one DOES, teased. why_match: why you're shipping them anyway, even if you roll your eyes. "
        "as_humans: 3-5 sentences casting them as people on a date by personality and job, never by race. "
        "red_flags/green_flags: short girl-talk bullets, still grounded. "
        "Return JSON exactly: {\"headline\": str (<=90 chars, a sassy tagline), \"compatibility\": int 0-100, "
        "\"appearance_a\": str, \"appearance_b\": str, \"appearance_together\": str, "
        "\"functionality\": str, \"why_match\": str, \"as_humans\": str, "
        "\"red_flags\": [str,...], \"green_flags\": [str,...], "
        "\"clips\": [{\"who\": \"A\"|\"B\", \"n\": int, \"note\": str (<=140 chars)}], "
        "\"verdict\": str}",
        (
            f"Vehicle A: \"{a}\" — {len(ca)} complaints across {len(vids_a)} videos.\n"
            f"Vehicle B: \"{b}\" — {len(cb)} complaints across {len(vids_b)} videos.\n"
            f"They share {len(shared)} parent videos (same stretch of footage).\n\n"
            f"--- VEHICLE A CLIPS ---\n{pack('A', rows_a, extra_a)}\n\n"
            f"--- VEHICLE B CLIPS ---\n{pack('B', rows_b, extra_b)}"
        ),
        temperature=0.92, max_tokens=1600,
    )

    notes = {(str(e.get("who") or "").upper(), int(e.get("n") or 0)): str(e.get("note") or "")
             for e in (out.get("clips") or []) if isinstance(e, dict)}
    clips_a = [_clip_from_complaint(c, i, "A") for i, c in enumerate(rows_a, 1)]
    clips_b = [_clip_from_complaint(c, i, "B") for i, c in enumerate(rows_b, 1)]
    extras = []
    seen = {c["source"] for c in rows_a + rows_b}
    for who, hits in (("A", extra_a), ("B", extra_b)):
        n = 1
        for r in hits:
            if not r.get("source") or r["source"] in seen:
                continue
            extras.append(_clip_from_search(r, n, who))
            seen.add(r["source"])
            n += 1
    for cl in clips_a:
        cl["note"] = notes.get(("A", cl["n"])) or (cl["caption"][:140] if cl["caption"] else cl["violation"])
    for cl in clips_b:
        cl["note"] = notes.get(("B", cl["n"])) or (cl["caption"][:140] if cl["caption"] else cl["violation"])
    result = {
        "a": a, "b": b, "shared_videos": len(shared),
        "count_a": len(ca), "count_b": len(cb),
        "headline": str(out.get("headline") or f"{a} × {b}")[:160],
        "compatibility": max(0, min(100, int(out.get("compatibility") or 0))),
        "appearance_a": str(out.get("appearance_a") or ""), "appearance_b": str(out.get("appearance_b") or ""),
        "appearance_together": str(out.get("appearance_together") or ""),
        "functionality": str(out.get("functionality") or ""), "why_match": str(out.get("why_match") or ""),
        "as_humans": str(out.get("as_humans") or ""), "verdict": str(out.get("verdict") or ""),
        "red_flags": [str(x) for x in (out.get("red_flags") or [])][:5],
        "green_flags": [str(x) for x in (out.get("green_flags") or [])][:4],
        "clips_a": clips_a, "clips_b": clips_b, "extras": extras[:6],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _VEHICLE_CACHE[key] = result
    return result



@telemetry.op("persona.match_briefing")
def match_briefing(persona_id: str, offender: str, refresh: bool = False) -> dict:
    """Dating-profile style compatibility briefing for a persona x repeat-offender pair.

    The AI reads every encounter clip (Cosmos Reason caption + YOLO counts) and writes
    appearance / functionality / as-humans commentary plus a clip-by-clip log.
    """
    key = (persona_id, offender)
    if not refresh and key in _MATCH_CACHE:
        return _MATCH_CACHE[key]
    p = pipeline.PERSONA_BY_ID[persona_id]
    comps = sorted([c for c in store.complaints() if c["persona"] == persona_id and c.get("offender") == offender],
                   key=lambda c: (c.get("filename") or "", c.get("segment_start") or 0))
    if not comps:
        raise ValueError("no encounters between that persona and offender")
    load_segments()
    clips = []
    for i, c in enumerate(comps[:14], 1):
        seg = SEG_BY_SOURCE.get(c["source"], {})
        counts = pipeline._counts(seg) if seg else {}
        clips.append({
            "n": i, "id": c["id"], "source": c["source"], "filename": c["filename"], "start": c["segment_start"], "end": c["segment_end"],
            "severity": c["severity"], "violation": pipeline.VIOLATIONS.get(c["violation_type"], {}).get("label", c["violation_type"]),
            "caption": (seg.get("reasoning_content") or c.get("evidence") or "")[:900],
            "counts": ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:6]),
            "stream_url": "/api/stream?" + urllib.parse.urlencode({"source": c["source"]}),
        })
    dossier = "\n\n".join(
        f"Encounter {cl['n']} — {cl['filename']} @ {cl['start']:.0f}-{cl['end']:.0f}s · {cl['violation']} · severity {cl['severity']}/5\n"
        f"What the camera saw: {cl['caption']}\nDetections: {cl['counts'] or 'n/a'}"
        for cl in clips
    )
    out = _matchmaker_json(
        MATCHMAKER_VOICE
        + " This pairing is a piece of street infrastructure vs a vehicle that will not leave it alone. "
        "Every JSON string is spoken in your cute sarcastic girl voice — no field may sound like a police report. "
        "The encounters array MUST contain exactly one sassy one-liner per encounter number, in order. "
        "Appearance is the vehicle as a vehicle, never human skin or race. "
        "Return JSON exactly: {\"headline\": str (<=90 chars), \"compatibility\": int 0-100, "
        "\"appearance\": str, \"functionality\": str, \"as_humans\": str, "
        "\"red_flags\": [str, ...], \"green_flags\": [str, ...], "
        "\"encounters\": [{\"n\": int, \"note\": str (<=140 chars)}], "
        "\"verdict\": str}",
        (
            f"Profile A: {p['name']} — {p['tagline']} Voice: {p['voice']} Location: {p['location']}. Cameras: {', '.join(p['cameras'])}.\n"
            f"Profile B: \"{offender}\" — has appeared on Profile A {len(comps)} times; complaints filed each time.\n\n"
            f"Encounter dossier ({len(clips)} clips, each 5 seconds, read all of them):\n\n{dossier}"
        ),
        temperature=0.92, max_tokens=1400,
    )
    notes = {int(e.get("n", 0)): str(e.get("note") or "") for e in (out.get("encounters") or []) if isinstance(e, dict)}
    by_id = {c["id"]: c for c in comps}
    for cl in clips:
        cl["note"] = notes.get(cl["n"]) or f"On record: {by_id[cl['id']].get('evidence') or cl['violation']}"
    result = {
        "persona": persona_id, "persona_name": p["name"], "persona_emoji": p["emoji"], "offender": offender,
        "encounter_count": len(comps), "clips_read": len(clips),
        "headline": str(out.get("headline") or f"{p['name']} × {offender}")[:160],
        "compatibility": max(0, min(100, int(out.get("compatibility") or 0))),
        "appearance": str(out.get("appearance") or ""), "functionality": str(out.get("functionality") or ""),
        "as_humans": str(out.get("as_humans") or ""), "verdict": str(out.get("verdict") or ""),
        "red_flags": [str(x) for x in (out.get("red_flags") or [])][:5], "green_flags": [str(x) for x in (out.get("green_flags") or [])][:4],
        "clips": clips, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _MATCH_CACHE[key] = result
    return result


def search(q: str, persona_id: str | None) -> dict:
    filters = None
    if persona_id and persona_id in pipeline.PERSONA_BY_ID:
        p = pipeline.PERSONA_BY_ID[persona_id]
        filters = {"camera_id": p["cameras"][0]} if len(p["cameras"]) == 1 else {"location": p["location"]}
    res = vss.search(q, top_k=16, metadata_filters=filters)
    out = []
    for r in res.get("results") or []:
        persona = pipeline.CAMERA_TO_PERSONA.get(r.get("camera_id") or "")
        vid = pipeline.verdict_id(r["source"], persona["id"]) if persona else None
        verdict = store.verdicts.get(vid) if vid else None
        out.append({
            "source": r["source"], "filename": r.get("filename"), "camera_id": r.get("camera_id"), "location": r.get("location"),
            "score": round(float(r.get("similarity_score") or 0), 3), "start": r.get("segment_start_sec"), "end": r.get("segment_end_sec"),
            "caption": r.get("reasoning_content"), "persona": persona["id"] if persona else None,
            "stream_url": "/api/stream?" + urllib.parse.urlencode({"source": r["source"]}),
            "complaint": decorate(verdict) if verdict and verdict.get("violation") else None,
            "scanned": verdict is not None,
        })
    return {"query": q, "results": out, "total": res.get("total"), "search_time_ms": res.get("search_time_ms")}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "StreetTalksBack/1.0"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        if "/api/events" in str(args[0] if args else "") or "/api/stream" in str(args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    # -- utilities
    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _static(self, name: str) -> None:
        path = ROOT / name
        if not path.is_file() or path.suffix not in (".html", ".js", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".md"):
            self._json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/markdown; charset=utf-8" if path.suffix == ".md" else (mimetypes.guess_type(name)[0] or "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- GET
    def do_GET(self) -> None:  # noqa: N802
        try:
            url = urllib.parse.urlparse(self.path)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            path = url.path.rstrip("/") or "/"
            if path == "/":
                return self._static("index.html")
            if path in ("/app.js", "/style.css", "/personas.json", "/README.md", "/EVAL.md", "/matchmaker-avatar.jpg"):
                return self._static(path.lstrip("/"))
            if path == "/health":
                return self._json({"ok": True, "store": store.backend, "verdicts": len(store.verdicts), "scan": scanner.status["state"]})
            if path == "/api/dashboard":
                return self._json(dashboard())
            if path == "/api/stack":
                return self._json(stack())
            if path == "/api/eval":
                return self._json(eval_report())
            if path == "/api/personas":
                return self._json({"personas": pipeline.PERSONAS, "violation_types": pipeline.VIOLATIONS})
            if path == "/api/complaints":
                return self._json(self._complaints(qs))
            if path.startswith("/api/complaints/"):
                row = store.verdicts.get(path.rsplit("/", 1)[-1])
                if not row:
                    return self._json({"error": "not found"}, 404)
                return self._json({"complaint": decorate(row), **neighbors(row)})
            if path == "/api/offenders":
                return self._json({"offenders": top_offenders(store.complaints())})
            if path == "/api/matches":
                return self._json({"matches": vehicle_pairs()})
            if path == "/api/tickets":
                return self._json({"tickets": sorted(store.tickets.values(), key=lambda t: t["created_at"], reverse=True)})
            if path == "/api/search":
                return self._json(search(qs.get("q", ""), qs.get("persona")))
            if path == "/api/scan/status":
                return self._json(scanner.status)
            if path == "/api/live/status":
                return self._json(live.state)
            if path == "/api/cameras":
                load_segments()
                return self._json({"cameras": [{"camera_id": cam, "segments": n, "persona": (pipeline.CAMERA_TO_PERSONA.get(cam) or {}).get("id")} for cam, n in Counter(s.get("camera_id") for s in SEGMENTS).most_common()]})
            if path == "/api/events":
                return self._sse()
            if path == "/api/stream":
                return self._stream(qs.get("source", ""))
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def _complaints(self, qs: dict) -> dict:
        rows = store.complaints()
        if qs.get("persona"):
            rows = [r for r in rows if r["persona"] == qs["persona"]]
        if qs.get("type"):
            rows = [r for r in rows if r.get("violation_type") == qs["type"]]
        if qs.get("offender"):
            rows = [r for r in rows if r.get("offender") == qs["offender"]]
        if qs.get("camera_id"):
            rows = [r for r in rows if r.get("camera_id") == qs["camera_id"]]
        if qs.get("min_severity"):
            rows = [r for r in rows if r["severity"] >= int(qs["min_severity"])]
        sort = qs.get("sort", "severity")
        rows.sort(key=(lambda r: (-r["severity"], -r["confidence"])) if sort == "severity" else (lambda r: r["created_at"]), reverse=(sort != "severity"))
        limit = int(qs.get("limit", "60"))
        offset = int(qs.get("offset", "0"))
        return {"total": len(rows), "complaints": [decorate(r) for r in rows[offset:offset + limit]]}

    def _sse(self) -> None:
        q = bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            for ev in bus.history[-10:]:
                self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(ev, default=str)}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            bus.unsubscribe(q)

    def _stream(self, source: str) -> None:
        if not source.startswith("s3://"):
            return self._json({"error": "bad source"}, 400)
        status, headers, body = vss.open_stream(source, self.headers.get("Range"))
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        try:
            for chunk in body:
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- POST
    def do_POST(self) -> None:  # noqa: N802
        try:
            path = urllib.parse.urlparse(self.path).path.rstrip("/")
            body = self._body()
            if path == "/api/scan/start":
                return self._json(scanner.start(limit=body.get("limit"), cameras=body.get("cameras")))
            if path == "/api/scan/stop":
                scanner.stop()
                return self._json(scanner.status)
            if path == "/api/live/start":
                cam = body.get("camera_id") or "nyc_bike_gopro-1"
                return self._json(live.start(cam, speed=float(body.get("speed") or 1.0), start_at=int(body.get("start_at") or 0)))
            if path == "/api/live/stop":
                live.stop()
                return self._json(live.state)
            if path == "/api/interview":
                return self._json(interview(body["persona"], body["question"]))
            if path == "/api/escalate":
                return self._json({"ticket": escalate(body["persona"], body.get("offender"), body.get("violation_type"), body.get("complaint_ids"))})
            if path == "/api/match":
                return self._json({"match": match_briefing(body["persona"], body["offender"], bool(body.get("refresh")))})
            if path == "/api/matches/briefing":
                return self._json({"match": vehicle_briefing(body["a"], body["b"], bool(body.get("refresh")))})
            if path == "/api/judge":
                seg = SEG_BY_SOURCE.get(body.get("source", "")) if load_segments() else None
                if not seg:
                    return self._json({"error": "unknown source"}, 404)
                row = pipeline.judge_segment(seg, store)
                return self._json({"verdict": decorate(row) if row else None})
            return self._json({"error": "not found"}, 404)
        except (KeyError, ValueError) as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)


def main() -> None:
    def warm() -> None:
        try:
            load_segments()
            print(f"[boot] {len(SEGMENTS)} segments in archive; {len(store.verdicts)} verdicts cached", flush=True)
        except Exception as e:
            print(f"[boot] segment preload failed: {e}", flush=True)
        if os.environ.get("AUTO_SCAN", "1") == "1":
            scanner.start()

    telemetry.init()
    threading.Thread(target=warm, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"[boot] The Street Talks Back listening on :{PORT} (store={store.backend})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
