"""Thin client for the team's VSS retrieval backend (JWT auth, search, segments, stream proxy)."""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

import telemetry

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


class VSSClient:
    def __init__(self) -> None:
        self.base = (os.environ.get("VSS_URL") or os.environ.get("INGRESS_URL") or "").rstrip("/")
        self.username = os.environ.get("VSS_USERNAME") or os.environ.get("USERNAME") or ""
        self.password = os.environ.get("VSS_PASSWORD") or os.environ.get("PASSWORD") or ""
        if not (self.base and self.username and self.password):
            raise RuntimeError("VSS_URL / VSS_USERNAME / VSS_PASSWORD (or INGRESS_URL/USERNAME/PASSWORD) must be set")
        self._token: str | None = None
        self._token_at = 0.0
        self._lock = threading.Lock()

    # ---- auth -------------------------------------------------------------
    def token(self, force: bool = False) -> str:
        with self._lock:
            if self._token and not force and time.time() - self._token_at < 3000:
                return self._token
            body = json.dumps({"username": self.username, "password": self.password}).encode()
            req = urllib.request.Request(
                self.base + "/api/v1/auth/login", data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
                self._token = json.load(r)["access_token"]
            self._token_at = time.time()
            return self._token

    def _call(self, method: str, path: str, body: Any = None, timeout: int = 90) -> Any:
        for attempt in range(2):
            headers = {"Authorization": "Bearer " + self.token(force=attempt == 1)}
            data = None
            if body is not None:
                headers["Content-Type"] = "application/json"
                data = json.dumps(body).encode()
            req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                    telemetry.bump("vss.calls")
                    telemetry.bump("vss." + path.split("?", 1)[0].removeprefix("/api/v1/"))
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                raise
        raise RuntimeError("unreachable")

    def get(self, path: str, **params: Any) -> Any:
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        return self._call("GET", path)

    def post(self, path: str, body: Any, timeout: int = 120) -> Any:
        return self._call("POST", path, body, timeout=timeout)

    # ---- archive ----------------------------------------------------------
    def explore_all(self) -> list[dict]:
        chunks: list[dict] = []
        offset = 0
        while True:
            page = self.get("/api/v1/videos/explore", scope="all", limit=100, offset=offset)
            batch = page.get("chunks") or []
            chunks.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(page.get("total") or 0):
                break
        return chunks

    def all_segments(self) -> list[dict]:
        """Flatten explore timelines into one row per 5s segment."""
        rows: list[dict] = []
        for chunk in self.explore_all():
            meta = {
                k: chunk.get(k)
                for k in ("original_video", "filename", "camera_id", "capture_type", "location", "upload_timestamp", "stream_id", "chunk_index")
            }
            for seg in chunk.get("timeline") or []:
                row = dict(meta)
                row.update(seg)
                row["chunk_filename"] = meta["filename"]
                row["filename"] = row["source"].rsplit("/", 1)[-1]
                rows.append(row)
        rows.sort(key=lambda r: (r.get("upload_timestamp") or "", r.get("segment_number") or 0))
        return rows

    def segments_for(self, original_video: str) -> list[dict]:
        data = self.get("/api/v1/tools/segments", original_video=original_video)
        segs = data.get("segments") if isinstance(data, dict) else data
        return sorted(segs or [], key=lambda s: s.get("segment_number") or 0)

    def search(self, query: str, top_k: int = 12, metadata_filters: dict | None = None, min_similarity: float = 0.2) -> dict:
        body: dict[str, Any] = {"query": query, "top_k": top_k, "llm_top_n": 1, "min_similarity": min_similarity}
        if metadata_filters:
            body["metadata_filters"] = metadata_filters
        return self.post("/api/v1/search", body)

    def search_and_answer(self, query: str, metadata_filters: dict | None = None, top_k: int = 10) -> dict:
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if metadata_filters:
            body["metadata_filters"] = metadata_filters
        return self.post("/api/v1/agent/search-and-answer", body, timeout=180)

    def detections(self, source: str) -> Any:
        return self.get("/api/v1/videos/detections", source=source)

    # ---- streaming proxy --------------------------------------------------
    def open_stream(self, source: str, range_header: str | None) -> tuple[int, dict, Iterator[bytes]]:
        url = self.base + "/api/v1/videos/stream?" + urllib.parse.urlencode({"source": source, "token": self.token()})
        headers = {}
        if range_header:
            headers["Range"] = range_header
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=60, context=_CTX)
        except urllib.error.HTTPError as e:
            resp = e
        status = resp.status if hasattr(resp, "status") else resp.code
        passthrough = {}
        for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            val = resp.headers.get(key)
            if val:
                passthrough[key] = val

        telemetry.bump("s3.streams")

        def body() -> Iterator[bytes]:
            try:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    telemetry.bump("s3.bytes_streamed", len(chunk))
                    yield chunk
            finally:
                resp.close()

        return status, passthrough, body()
