"""Detection pipeline: segment -> persona trigger -> LLM verification (grounded) -> verdict row."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import llm
import telemetry
from store import Store
from vss import VSSClient

CONFIG = json.loads((Path(__file__).parent / "personas.json").read_text())
PERSONAS: list[dict] = CONFIG["personas"]
PERSONA_BY_ID: dict[str, dict] = {p["id"]: p for p in PERSONAS}
VIOLATIONS: dict[str, dict] = CONFIG["violation_types"]
CAMERA_TO_PERSONA: dict[str, dict] = {cam: p for p in PERSONAS for cam in p["cameras"]}

# Demo-first ordering: the most visual packs get verified first.
PRIORITY = ["nyc_bike_gopro-1", "nyc_streets_cam-1", "nyc_streets_cam-2", "sf_streets_cam-3", "sf_streets_cam-1",
            "sdg_warehouse_cam-2", "pie_cam-3", "neighborhood_cam-1", "i24_cam-1"]

SYSTEM_PROMPT = """You are a strict, evidence-bound traffic and safety auditor reviewing ONE five-second clip.
You receive: the clip caption written by a vision model, YOLO object counts, and the persona who is watching
(an inanimate piece of street infrastructure with a point of view) together with the violation types it cares about.

Rules:
1. Flag a violation ONLY if the caption explicitly describes the behavior (e.g. "a van is parked in the bike lane",
   "pedestrians cross while the signal is red", "the truck is stopped in the crosswalk", "forklift passes close to a worker").
   Vague scenery, vehicles merely present, or "no visible violations" => violation=false.
2. Pick exactly one violation_type from the persona's watch list, or null.
3. severity 1-5 (5 = someone could get hurt right now). confidence 0-1 is how sure you are the caption supports it.
4. evidence must be a short verbatim quote from the caption.
5. offender is a short visual signature of the party at fault, e.g. "white USPS truck", "yellow taxi", "pedestrian in red jacket",
   or null if nobody is identifiable.
6. complaint: 1-2 sentences, first person, in the persona's voice, grounded ONLY in the evidence. No invented details,
   no plates, no names. Never introduce yourself ("I'm a bike lane") — the reader knows who is speaking. Be specific
   about the offender and what they did; dry wit is welcome, cruelty is not. Max 240 characters. Empty string when violation=false.
Return ONLY JSON: {"violation": bool, "violation_type": str|null, "severity": int, "confidence": float,
"actors": [str], "offender": str|null, "evidence": str, "complaint": str}"""


def persona_for(segment: dict) -> dict | None:
    return CAMERA_TO_PERSONA.get(segment.get("camera_id") or "")


def triggered(segment: dict, persona: dict) -> bool:
    counts = _counts(segment)
    trig = persona.get("trigger") or {}
    any_of = trig.get("any") or []
    all_of = trig.get("all") or []
    if any_of and not any(counts.get(c, 0) > 0 for c in any_of):
        return False
    return all(counts.get(c, 0) > 0 for c in all_of)


def _counts(segment: dict) -> dict[str, int]:
    raw = segment.get("object_counts")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def verdict_id(source: str, persona_id: str) -> str:
    return hashlib.sha1(f"{persona_id}|{source}".encode()).hexdigest()[:16]


VOICE_PROMPT = """You write one short complaint (1-2 sentences, max 220 characters) spoken in first person by a piece of street
infrastructure with a personality. Stay strictly inside the evidence given: no plates, names, times, distances, counts or any
detail that is not literally in the evidence. If the evidence says "close", say "close", never invent "12 inches". Never introduce yourself or explain what you are. Name the offender and what they did. Dry, specific, quotable,
a little theatrical. Vary your openings; never start with "I'm" or "I am".

Style examples (do not copy, match the energy):
- "Another white USPS truck, parked right across my green paint. Deliveries are important. So is the cyclist who just swerved into the bus lane."
- "A Walgreens truck is sitting on my stripes while six people pick their way around its bumper. Not a crosswalk anymore, a detour."
- "Yellow taxi, lane change, inches from a cyclist's elbow. Nobody honked. Everybody noticed."

Return JSON: {"complaint": str}"""


@telemetry.op("persona.judge_segment")
def verify(segment: dict, persona: dict) -> dict:
    """Stage 1 (fast model): grounded detection. Stage 2 (smart model): persona voice, only when something was found."""
    watch = ", ".join(f"{v} ({VIOLATIONS[v]['label']})" for v in persona["watch"] if v in VIOLATIONS)
    user = (
        f"Persona: {persona['name']} — {persona['voice']}\n"
        f"Watch list: {watch}\n"
        f"Camera: {segment.get('camera_id')} | Location: {segment.get('location')}\n"
        f"YOLO max-per-frame counts: {json.dumps(_counts(segment))}\n"
        f"Caption: {(segment.get('reasoning_content') or '')[:2200]}"
    )
    telemetry.bump("nvidia.cosmos_captions_read")
    telemetry.bump("nvidia.yolo_detections_read", sum(_counts(segment).values()))
    verdict = normalize(llm.chat_json([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}], temperature=0.0), persona)
    if verdict["violation"]:
        verdict["complaint"] = voice(verdict, persona)
    return verdict


@telemetry.op("persona.voice_complaint")
def voice(verdict: dict, persona: dict) -> str:
    label = VIOLATIONS.get(verdict["violation_type"] or "", {}).get("label", verdict["violation_type"])
    try:
        out = llm.chat_json([
            {"role": "system", "content": VOICE_PROMPT},
            {"role": "user", "content": (
                f"Speaker: {persona['name']} — {persona['voice']} ({persona['tagline']})\n"
                f"Violation: {label} | severity {verdict['severity']}/5\n"
                f"Offender: {verdict.get('offender') or 'unidentified'}\n"
                f"Evidence (verbatim from the clip caption): \"{verdict['evidence']}\"\n"
                f"Fallback draft you may improve: \"{verdict.get('complaint') or ''}\""
            )},
        ], model=llm.SMART_MODEL, temperature=0.8, max_tokens=200)
        text = str(out.get("complaint") or "").strip()
    except Exception as e:  # keep the stage-1 draft if the voice model hiccups
        print(f"[voice] fallback: {e}", flush=True)
        text = ""
    text = text or verdict.get("complaint") or f"{label}. {verdict['evidence']}"
    return _strip_self_intro(text)[:300]


def _strip_self_intro(text: str) -> str:
    import re
    return re.sub(r"^(i['’]m|i am)\s+(a|an|the)\s+[^,.;:]{2,40}[,.;:]\s*(and\s+)?", "", text.strip(), flags=re.I).strip() or text


def normalize(result: dict, persona: dict) -> dict:
    violation = bool(result.get("violation"))
    vtype = result.get("violation_type")
    if vtype not in persona["watch"]:
        vtype = None
        if violation and persona["watch"]:
            # the model picked a type outside the list; keep the complaint but mark it generic
            vtype = persona["watch"][0] if result.get("evidence") else None
    if not vtype:
        violation = False
    try:
        severity = int(result.get("severity") or VIOLATIONS.get(vtype or "", {}).get("base_severity", 2))
    except (TypeError, ValueError):
        severity = 2
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    actors = result.get("actors") or []
    if not isinstance(actors, list):
        actors = [str(actors)]
    offender = normalize_offender(result.get("offender"))
    return {
        "violation": violation and confidence >= 0.45,
        "violation_type": vtype,
        "severity": max(1, min(5, severity)),
        "confidence": round(max(0.0, min(1.0, confidence)), 3),
        "actors": [str(a)[:60] for a in actors][:6],
        "offender": offender,
        "evidence": str(result.get("evidence") or "")[:400],
        "complaint": str(result.get("complaint") or "")[:300] if violation else "",
    }


_OFFENDER_NOISE = {"a", "an", "the", "delivery", "vehicle", "parked", "stopped", "driver", "of", "with", "its", "their"}
_OFFENDER_ALIAS = {"usps": "usps", "u.s.": "us", "postal": "usps", "service": "", "taxi": "taxi", "cab": "taxi", "suv": "suv"}


def normalize_offender(raw: Any) -> str | None:
    """Collapse 'White USPS delivery truck' / 'white USPS truck' into one signature."""
    if not raw or str(raw).strip().lower() in ("null", "none", "unknown", "n/a"):
        return None
    words = [w.strip(".,;:()\"'") for w in str(raw).lower().replace("-", " ").split()]
    out: list[str] = []
    for w in words:
        w = _OFFENDER_ALIAS.get(w, w)
        if w and w not in _OFFENDER_NOISE and w not in out:
            out.append(w)
    sig = " ".join(out).strip()
    return sig[:80] or None


def build_row(segment: dict, persona: dict, verdict: dict) -> dict:
    return {
        "id": verdict_id(segment["source"], persona["id"]),
        "source": segment["source"],
        "original_video": segment.get("original_video") or "",
        "filename": segment.get("filename") or segment["source"].rsplit("/", 1)[-1],
        "persona": persona["id"],
        "camera_id": segment.get("camera_id") or "",
        "location": segment.get("location") or "",
        "segment_start": float(segment.get("segment_start_sec") or 0.0),
        "segment_end": float(segment.get("segment_end_sec") or 0.0),
        "upload_timestamp": segment.get("upload_timestamp") or "",
        "object_counts": json.dumps(_counts(segment)),
        "caption": (segment.get("reasoning_content") or "")[:2200],
        "model": llm.FAST_MODEL,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **verdict,
    }


def judge_segment(segment: dict, store: Store | None = None) -> dict | None:
    """Verify one segment on demand (used by live replay). Returns the verdict row or None if no persona."""
    persona = persona_for(segment)
    if not persona:
        return None
    vid = verdict_id(segment["source"], persona["id"])
    if store and store.has(vid):
        return store.verdicts[vid]
    if not triggered(segment, persona):
        verdict = normalize({"violation": False}, persona)
    else:
        verdict = verify(segment, persona)
    row = build_row(segment, persona, verdict)
    if store:
        store.add_verdicts([row])
    return row


class Scanner:
    """Background scan over the whole archive with progress reporting."""

    def __init__(self, vss: VSSClient, store: Store, workers: int = 10) -> None:
        self.vss = vss
        self.store = store
        self.workers = workers
        self.status: dict[str, Any] = {"state": "idle", "total": 0, "done": 0, "complaints": 0, "skipped": 0, "errors": 0, "started_at": None, "finished_at": None, "current_camera": None}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.segments: list[dict] = []

    def start(self, limit: int | None = None, cameras: list[str] | None = None) -> dict:
        if self._thread and self._thread.is_alive():
            return self.status
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, args=(limit, cameras), daemon=True)
        self._thread.start()
        return self.status

    def stop(self) -> None:
        self._stop.set()

    def _run(self, limit: int | None, cameras: list[str] | None) -> None:
        self.status.update(state="loading", started_at=time.time(), finished_at=None, done=0, errors=0, skipped=0)
        try:
            segments = self.vss.all_segments()
        except Exception as e:
            self.status.update(state="error", error=str(e))
            return
        self.segments = segments
        rank = {cam: i for i, cam in enumerate(PRIORITY)}
        todo = [s for s in segments if persona_for(s) and (not cameras or s.get("camera_id") in cameras)]
        todo.sort(key=lambda s: (rank.get(s.get("camera_id"), 99), s.get("upload_timestamp") or "", s.get("segment_number") or 0))
        todo = [s for s in todo if not self.store.has(verdict_id(s["source"], persona_for(s)["id"]))]
        if limit:
            todo = todo[:limit]
        self.status.update(state="running", total=len(todo), complaints=len(self.store.complaints()))
        pending: list[dict] = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            for s in todo:
                if self._stop.is_set():
                    break
                p = persona_for(s)
                if not triggered(s, p):
                    pending.append(build_row(s, p, normalize({"violation": False}, p)))
                    self.status["skipped"] += 1
                    self.status["done"] += 1
                    continue
                futures[pool.submit(verify, s, p)] = (s, p)
            for fut in as_completed(futures):
                s, p = futures[fut]
                self.status["current_camera"] = s.get("camera_id")
                try:
                    pending.append(build_row(s, p, fut.result()))
                except Exception as e:
                    self.status["errors"] += 1
                    print(f"[scan] verify failed for {s.get('filename')}: {e}", flush=True)
                self.status["done"] += 1
                if len(pending) >= 25:
                    self._flush(pending)
                if self._stop.is_set():
                    for f in futures:
                        f.cancel()
                    break
        self._flush(pending)
        self.status.update(state="stopped" if self._stop.is_set() else "done", finished_at=time.time(), complaints=len(self.store.complaints()))

    def _flush(self, pending: list[dict]) -> None:
        if pending:
            self.store.add_verdicts(list(pending))
            self.status["complaints"] = len(self.store.complaints())
            pending.clear()
