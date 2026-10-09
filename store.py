"""Persistence for verdicts and tickets: VastDB (team bucket, schema `street_talks_back`) with a JSON-file fallback."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

SCHEMA_NAME = os.environ.get("STB_SCHEMA", "street_talks_back")
VERDICTS_TABLE = "verdicts"
TICKETS_TABLE = "tickets"
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/street-talks-back"))

VERDICT_FIELDS: list[tuple[str, str]] = [
    ("id", "utf8"), ("source", "utf8"), ("original_video", "utf8"), ("filename", "utf8"),
    ("persona", "utf8"), ("camera_id", "utf8"), ("location", "utf8"),
    ("segment_start", "float32"), ("segment_end", "float32"), ("upload_timestamp", "utf8"),
    ("violation", "bool"), ("violation_type", "utf8"), ("severity", "int32"), ("confidence", "float32"),
    ("actors", "utf8"), ("offender", "utf8"), ("evidence", "utf8"), ("complaint", "utf8"),
    ("object_counts", "utf8"), ("caption", "utf8"), ("model", "utf8"), ("created_at", "utf8"),
]
TICKET_FIELDS: list[tuple[str, str]] = [
    ("id", "utf8"), ("persona", "utf8"), ("offender", "utf8"), ("violation_type", "utf8"),
    ("complaint_ids", "utf8"), ("title", "utf8"), ("body", "utf8"), ("severity", "int32"),
    ("webhook_status", "utf8"), ("created_at", "utf8"),
]


def _pa_type(name: str):
    import pyarrow as pa
    return {"utf8": pa.utf8(), "float32": pa.float32(), "int32": pa.int32(), "bool": pa.bool_()}[name]


class Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.verdicts: dict[str, dict] = {}
        self.tickets: dict[str, dict] = {}
        self.backend = "json"
        self._session = None
        self._bucket = os.environ.get("VASTDB_BUCKET", "")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._try_vastdb()
        self.load()

    # ---- backends ---------------------------------------------------------
    def _try_vastdb(self) -> None:
        endpoint = os.environ.get("VDB_ENDPOINT") or os.environ.get("S3_ENDPOINT", "")
        access = os.environ.get("VAST_ACCESS_KEY") or os.environ.get("ACCESS_KEY", "")
        secret = os.environ.get("VAST_SECRET_KEY") or os.environ.get("SECRET_KEY", "")
        if not (endpoint and access and secret and self._bucket) or os.environ.get("STB_DISABLE_VASTDB"):
            return
        try:
            import warnings
            import pyarrow as pa
            import vastdb
            try:
                import urllib3
                urllib3.disable_warnings()
            except Exception:
                pass
            warnings.filterwarnings("ignore")
            import logging
            logging.getLogger("vastdb").setLevel(logging.ERROR)
            if not endpoint.startswith(("http://", "https://")):
                endpoint = "http://" + endpoint
            self._session = vastdb.connect(endpoint=endpoint, access=access, secret=secret, ssl_verify=False)
            with self._session.transaction() as tx:
                bucket = tx.bucket(self._bucket)
                schema = bucket.schema(SCHEMA_NAME, fail_if_missing=False) or bucket.create_schema(SCHEMA_NAME, fail_if_exists=False)
                for name, fields in ((VERDICTS_TABLE, VERDICT_FIELDS), (TICKETS_TABLE, TICKET_FIELDS)):
                    if schema.table(name, fail_if_missing=False) is None:
                        schema.create_table(name, columns=pa.schema([(f, _pa_type(t)) for f, t in fields]), fail_if_exists=False)
            self.backend = "vastdb"
        except Exception as e:  # fall back silently but loudly in logs
            print(f"[store] VastDB unavailable, using JSON files: {type(e).__name__}: {e}", flush=True)
            self._session = None

    def _vdb_read(self, table: str, fields: list[tuple[str, str]]) -> list[dict]:
        with self._session.transaction() as tx:
            t = tx.bucket(self._bucket).schema(SCHEMA_NAME).table(table)
            rows: list[dict] = []
            for batch in t.select(columns=[f for f, _ in fields]):
                rows.extend(batch.to_pylist())
            return rows

    def _vdb_insert(self, table: str, fields: list[tuple[str, str]], rows: list[dict]) -> None:
        import pyarrow as pa
        schema = pa.schema([(f, _pa_type(t)) for f, t in fields])
        cols = {f: [r.get(f) for r in rows] for f, _ in fields}
        with self._session.transaction() as tx:
            tx.bucket(self._bucket).schema(SCHEMA_NAME).table(table).insert(pa.table(cols, schema=schema))

    # ---- load / save ------------------------------------------------------
    def load(self) -> None:
        if self.backend == "vastdb":
            try:
                for r in self._vdb_read(VERDICTS_TABLE, VERDICT_FIELDS):
                    self.verdicts[r["id"]] = self._inflate(r)
                for r in self._vdb_read(TICKETS_TABLE, TICKET_FIELDS):
                    r["complaint_ids"] = json.loads(r.get("complaint_ids") or "[]")
                    self.tickets[r["id"]] = r
                print(f"[store] loaded {len(self.verdicts)} verdicts, {len(self.tickets)} tickets from VastDB", flush=True)
                return
            except Exception as e:
                print(f"[store] VastDB load failed ({e}); falling back to JSON", flush=True)
                self.backend = "json"
        for name, target in (("verdicts", self.verdicts), ("tickets", self.tickets)):
            p = DATA_DIR / f"{name}.json"
            if p.exists():
                for r in json.loads(p.read_text()):
                    target[r["id"]] = r
        print(f"[store] loaded {len(self.verdicts)} verdicts, {len(self.tickets)} tickets from JSON", flush=True)

    @staticmethod
    def _inflate(r: dict) -> dict:
        r = dict(r)
        r["actors"] = json.loads(r.get("actors") or "[]")
        return r

    def _save_json(self) -> None:
        (DATA_DIR / "verdicts.json").write_text(json.dumps(list(self.verdicts.values())))
        (DATA_DIR / "tickets.json").write_text(json.dumps(list(self.tickets.values())))

    # ---- public API -------------------------------------------------------
    def has(self, verdict_id: str) -> bool:
        return verdict_id in self.verdicts

    def add_verdicts(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.lock:
            for r in rows:
                self.verdicts[r["id"]] = r
            if self.backend == "vastdb":
                flat = [dict(r, actors=json.dumps(r.get("actors") or [])) for r in rows]
                try:
                    self._vdb_insert(VERDICTS_TABLE, VERDICT_FIELDS, flat)
                except Exception as e:
                    print(f"[store] VastDB insert failed ({e}); keeping JSON copy", flush=True)
            self._save_json()

    def add_ticket(self, ticket: dict) -> None:
        with self.lock:
            self.tickets[ticket["id"]] = ticket
            if self.backend == "vastdb":
                try:
                    self._vdb_insert(TICKETS_TABLE, TICKET_FIELDS, [dict(ticket, complaint_ids=json.dumps(ticket["complaint_ids"]))])
                except Exception as e:
                    print(f"[store] VastDB ticket insert failed ({e})", flush=True)
            self._save_json()

    def complaints(self) -> list[dict]:
        return [v for v in self.verdicts.values() if v.get("violation")]
