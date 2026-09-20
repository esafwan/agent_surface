import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from surface.schema import init_db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Union[str, Path, sqlite3.Connection] = ":memory:"):
        if isinstance(db_path, sqlite3.Connection):
            self.conn = db_path
            self.conn.row_factory = sqlite3.Row
        else:
            self.conn = init_db(db_path)

    # -------------------------------------------------------------------------
    # Artifact Management
    # -------------------------------------------------------------------------

    def create_artifact(
        self,
        id: str,
        stage: str,
        title: str,
        status: str = "draft",
        meta: Optional[Dict[str, Any]] = None,
        locked: bool = False,
    ) -> Dict[str, Any]:
        meta_json = json.dumps(meta) if meta is not None else None
        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO artifacts (id, stage, title, status, locked, created_at, updated_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (id, stage, title, status, 1 if locked else 0, now, now, meta_json),
            )
        return self.get_artifact(id)

    def get_artifact(self, artifact_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["locked"] = bool(res["locked"])
        if res.get("meta_json"):
            res["meta"] = json.loads(res["meta_json"])
        else:
            res["meta"] = {}
        return res

    def list_artifacts(
        self, stage: Optional[str] = None, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM artifacts WHERE 1=1"
        params = []
        if stage:
            query += " AND stage = ?"
            params.append(stage)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"

        cursor = self.conn.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()
        result = []
        for r in rows:
            res = dict(r)
            res["locked"] = bool(res["locked"])
            res["meta"] = json.loads(res["meta_json"]) if res.get("meta_json") else {}
            result.append(res)
        return result

    def set_status(self, artifact_id: str, status: str) -> Dict[str, Any]:
        now = _now_iso()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE artifacts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, artifact_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Artifact {artifact_id} not found")
        return self.get_artifact(artifact_id)

    def set_lock(self, artifact_id: str, locked: bool) -> Dict[str, Any]:
        now = _now_iso()
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE artifacts SET locked = ?, updated_at = ? WHERE id = ?",
                (1 if locked else 0, now, artifact_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Artifact {artifact_id} not found")
        return self.get_artifact(artifact_id)

    # -------------------------------------------------------------------------
    # Versioning & Selection
    # -------------------------------------------------------------------------

    def put_version(
        self,
        artifact_id: str,
        content: Optional[str] = None,
        content_ref: Optional[str] = None,
        content_type: Optional[str] = None,
        prompt: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        created_by: str = "worker",
        note: Optional[str] = None,
        source_event_id: Optional[str] = None,
        select: bool = True,
    ) -> Dict[str, Any]:
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            raise ValueError(f"Artifact {artifact_id} not found")

        # Idempotency check: if source_event_id is provided, check if a version already exists for it
        if source_event_id:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM versions WHERE artifact_id = ? AND source_event_id = ?",
                (artifact_id, source_event_id),
            )
            existing = cursor.fetchone()
            if existing:
                ver_dict = dict(existing)
                # If selection is requested, ensure it's selected
                stale_descendants = []
                if select and artifact["selected_version_id"] != ver_dict["id"]:
                    sel_res = self.select_version(artifact_id, ver_dict["id"])
                    stale_descendants = sel_res.get("stale_descendants", [])
                return {
                    "ok": True,
                    "version_id": ver_dict["id"],
                    "version": ver_dict["n"],
                    "stale_descendants": stale_descendants,
                }

        # Determine next version number n
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT MAX(n) FROM versions WHERE artifact_id = ?", (artifact_id,)
        )
        max_n = cursor.fetchone()[0]
        next_n = (max_n or 0) + 1

        version_id = f"ver_{uuid.uuid4().hex[:8]}"
        params_json = json.dumps(params) if params is not None else None
        now = _now_iso()

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO versions (id, artifact_id, n, content_ref, content_type, content, prompt, params_json, created_by, note, source_event_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    artifact_id,
                    next_n,
                    content_ref,
                    content_type,
                    content,
                    prompt,
                    params_json,
                    created_by,
                    note,
                    source_event_id,
                    now,
                ),
            )

        stale_descendants = []
        if select:
            sel_res = self.select_version(artifact_id, version_id)
            stale_descendants = sel_res.get("stale_descendants", [])

        return {
            "ok": True,
            "version_id": version_id,
            "version": next_n,
            "stale_descendants": stale_descendants,
        }

    def list_versions(self, artifact_id: str) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM versions WHERE artifact_id = ? ORDER BY n ASC",
            (artifact_id,),
        )
        rows = cursor.fetchall()
        result = []
        for r in rows:
            res = dict(r)
            if res.get("params_json"):
                res["params"] = json.loads(res["params_json"])
            else:
                res["params"] = {}
            result.append(res)
        return result

    def get_version(self, version_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM versions WHERE id = ?", (version_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("params_json"):
            res["params"] = json.loads(res["params_json"])
        else:
            res["params"] = {}
        return res

    def select_version(
        self, artifact_id: str, version_id: str, expected_selected_version_id: Optional[str] = None
    ) -> Dict[str, Any]:
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            raise ValueError(f"Artifact {artifact_id} not found")

        version = self.get_version(version_id)
        if not version or version["artifact_id"] != artifact_id:
            raise ValueError(f"Version {version_id} invalid for artifact {artifact_id}")

        if expected_selected_version_id is not None:
            if artifact["selected_version_id"] != expected_selected_version_id:
                return {
                    "ok": False,
                    "error": "conflict",
                    "current_selected_version_id": artifact["selected_version_id"],
                }

        # Check if selected_version_id is actually changing
        old_selected = artifact["selected_version_id"]
        now = _now_iso()

        stale_descendants = []
        with self.conn:
            self.conn.execute(
                "UPDATE artifacts SET selected_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, artifact_id),
            )

            # If selected version changed, trigger DAG stale propagation
            if old_selected != version_id:
                stale_descendants = self._propagate_stale(artifact_id)

        return {
            "ok": True,
            "artifact_id": artifact_id,
            "selected_version_id": version_id,
            "stale_descendants": stale_descendants,
        }

    # -------------------------------------------------------------------------
    # DAG & Dependencies
    # -------------------------------------------------------------------------

    def add_dependency(
        self, upstream_artifact_id: str, downstream_artifact_id: str, kind: str = "content"
    ) -> None:
        if upstream_artifact_id == downstream_artifact_id:
            raise ValueError("Self-dependency creates a cycle")

        up = self.get_artifact(upstream_artifact_id)
        down = self.get_artifact(downstream_artifact_id)
        if not up or not down:
            raise ValueError("Upstream or downstream artifact not found")

        # Check for cycles: verify if upstream is reachable from downstream
        if self._is_reachable(downstream_artifact_id, upstream_artifact_id):
            raise ValueError(
                f"Adding dependency {upstream_artifact_id} -> {downstream_artifact_id} creates a cycle"
            )

        now = _now_iso()
        with self.conn:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO artifact_dependencies (upstream_artifact_id, downstream_artifact_id, kind, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (upstream_artifact_id, downstream_artifact_id, kind, now),
            )

    def _is_reachable(self, start_id: str, target_id: str) -> bool:
        """BFS/DFS to check if target_id is reachable from start_id following downstream edges."""
        cursor = self.conn.cursor()
        visited = set()
        queue = [start_id]
        while queue:
            curr = queue.pop(0)
            if curr == target_id:
                return True
            if curr in visited:
                continue
            visited.add(curr)
            cursor.execute(
                "SELECT downstream_artifact_id FROM artifact_dependencies WHERE upstream_artifact_id = ?",
                (curr,),
            )
            rows = cursor.fetchall()
            for r in rows:
                queue.append(r[0])
        return False

    def get_graph(self, artifact_id: str) -> Dict[str, Any]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT upstream_artifact_id, kind FROM artifact_dependencies WHERE downstream_artifact_id = ?",
            (artifact_id,),
        )
        upstreams = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            "SELECT downstream_artifact_id, kind FROM artifact_dependencies WHERE upstream_artifact_id = ?",
            (artifact_id,),
        )
        downstreams = [dict(r) for r in cursor.fetchall()]

        return {
            "artifact_id": artifact_id,
            "upstreams": upstreams,
            "downstreams": downstreams,
        }

    def _propagate_stale(self, start_artifact_id: str) -> List[str]:
        """
        Recursively traverse downstream dependent artifacts, mark them as 'stale',
        and emit 'stale' system events for affected artifacts.
        Revisited artifacts are avoided using a visited set.
        """
        cursor = self.conn.cursor()
        visited = set()
        queue = [start_artifact_id]

        # Collect all downstream descendants
        descendants = []
        while queue:
            curr = queue.pop(0)
            cursor.execute(
                "SELECT downstream_artifact_id FROM artifact_dependencies WHERE upstream_artifact_id = ?",
                (curr,),
            )
            rows = cursor.fetchall()
            for r in rows:
                down_id = r[0]
                if down_id not in visited:
                    visited.add(down_id)
                    descendants.append(down_id)
                    queue.append(down_id)

        now = _now_iso()
        stale_marked = []
        for down_id in descendants:
            # Mark artifact as stale
            cursor.execute(
                "UPDATE artifacts SET status = 'stale', updated_at = ? WHERE id = ?",
                (now, down_id),
            )
            stale_marked.append(down_id)

            # Emit stale system event if not already emitted for this down_id
            payload_str = json.dumps({"artifact_id": down_id, "cause_artifact_id": start_artifact_id})
            cursor.execute(
                "SELECT id FROM events WHERE artifact_id = ? AND type = 'stale' AND status = 'pending'",
                (down_id,),
            )
            if not cursor.fetchone():
                event_id = f"evt_{uuid.uuid4().hex[:8]}"
                cursor.execute(
                    """
                    INSERT INTO events (id, project_id, artifact_id, type, payload_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (event_id, "default", down_id, "stale", payload_str, now),
                )

        return stale_marked

    # -------------------------------------------------------------------------
    # Event Inbox
    # -------------------------------------------------------------------------

    def enqueue_event(
        self,
        type: str,
        payload: Dict[str, Any],
        artifact_id: Optional[str] = None,
        project_id: str = "default",
        dedupe_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        if dedupe_key:
            cursor = self.conn.cursor()
            cursor.execute(
                "SELECT * FROM events WHERE dedupe_key = ? AND status IN ('pending', 'processing')",
                (dedupe_key,),
            )
            existing = cursor.fetchone()
            if existing:
                res = dict(existing)
                res["payload"] = json.loads(res["payload_json"])
                return res

        event_id = f"evt_{uuid.uuid4().hex[:8]}"
        payload_json = json.dumps(payload)
        now = _now_iso()

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO events (id, project_id, artifact_id, type, payload_json, status, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (event_id, project_id, artifact_id, type, payload_json, dedupe_key, now),
            )

        return self.get_event(event_id)

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM events WHERE id = ?", (event_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["payload"] = json.loads(res["payload_json"]) if res.get("payload_json") else {}
        return res

    def claim_next_event(
        self, worker_id: str, lease_seconds: int = 60
    ) -> Optional[Dict[str, Any]]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        cursor = self.conn.cursor()

        # Step 1: Recover expired events (processing whose lease_until < now)
        cursor.execute(
            "SELECT id FROM events WHERE status = 'processing' AND lease_until IS NOT NULL AND lease_until < ?",
            (now,),
        )
        expired_events = cursor.fetchall()
        for (exp_id,) in expired_events:
            cursor.execute(
                "UPDATE events SET status = 'pending', claimed_by = NULL, claimed_at = NULL, lease_until = NULL WHERE id = ?",
                (exp_id,),
            )
            # Also clean up artifact_locks for expired lease
            cursor.execute(
                "DELETE FROM artifact_locks WHERE event_id = ?",
                (exp_id,),
            )

        # Step 2: Clean up expired artifact locks
        cursor.execute(
            "DELETE FROM artifact_locks WHERE lease_until < ?",
            (now,),
        )
        self.conn.commit()

        # Step 3: Find eligible pending events
        cursor.execute(
            """
            SELECT e.* FROM events e
            WHERE e.status = 'pending'
            ORDER BY e.created_at ASC
            """
        )
        pending_events = cursor.fetchall()

        for evt in pending_events:
            evt_id = evt["id"]
            art_id = evt["artifact_id"]

            # If event is artifact-scoped, check if artifact is locked/leased by an active run
            if art_id:
                cursor.execute(
                    "SELECT * FROM artifact_locks WHERE artifact_id = ? AND lease_until >= ?",
                    (art_id, now),
                )
                lock_row = cursor.fetchone()
                if lock_row:
                    continue  # Artifact is leased/locked by another active turn

            # Try to claim event atomically
            lease_until_dt = datetime.fromtimestamp(now_dt.timestamp() + lease_seconds, tz=timezone.utc)
            lease_until = lease_until_dt.isoformat()

            with self.conn:
                cursor.execute(
                    """
                    UPDATE events
                    SET status = 'processing',
                        claimed_by = ?,
                        claimed_at = ?,
                        lease_until = ?,
                        attempt_count = attempt_count + 1
                    WHERE id = ? AND status = 'pending'
                    """,
                    (worker_id, now, lease_until, evt_id),
                )
                if cursor.rowcount == 1:
                    # Successfully claimed event
                    if art_id:
                        cursor.execute(
                            """
                            INSERT OR REPLACE INTO artifact_locks (artifact_id, owner_worker_id, lease_until, event_id)
                            VALUES (?, ?, ?, ?)
                            """,
                            (art_id, worker_id, lease_until, evt_id),
                        )
                    return self.get_event(evt_id)

        return None

    def ack_event(self, event_id: str) -> Dict[str, Any]:
        now = _now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE events
                SET status = 'acked', acked_at = ?, lease_until = NULL
                WHERE id = ?
                """,
                (now, event_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Event {event_id} not found")

            # Release artifact processing lock
            self.conn.execute(
                "DELETE FROM artifact_locks WHERE event_id = ?",
                (event_id,),
            )
        return self.get_event(event_id)

    def fail_event(self, event_id: str, error: str) -> Dict[str, Any]:
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE events
                SET status = 'failed', error = ?, lease_until = NULL
                WHERE id = ?
                """,
                (error, event_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Event {event_id} not found")

            # Release artifact processing lock
            self.conn.execute(
                "DELETE FROM artifact_locks WHERE event_id = ?",
                (event_id,),
            )
        return self.get_event(event_id)

    # -------------------------------------------------------------------------
    # Job Store
    # -------------------------------------------------------------------------

    def create_job(
        self,
        artifact_id: str,
        provider: str,
        kind: str,
        request: Optional[Dict[str, Any]] = None,
        cost_estimate: float = 0.0,
        provider_job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        job_id = f"job_{uuid.uuid4().hex[:8]}"
        request_json = json.dumps(request) if request is not None else None
        now = _now_iso()

        with self.conn:
            self.conn.execute(
                """
                INSERT INTO jobs (id, artifact_id, provider, provider_job_id, kind, status, request_json, cost_estimate, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    artifact_id,
                    provider,
                    provider_job_id,
                    kind,
                    request_json,
                    cost_estimate,
                    now,
                    now,
                ),
            )

        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        res["cancel_requested"] = bool(res["cancel_requested"])
        res["request"] = json.loads(res["request_json"]) if res.get("request_json") else {}
        res["result"] = json.loads(res["result_json"]) if res.get("result_json") else {}
        return res

    def update_job_status(
        self,
        job_id: str,
        status: str,
        provider_job_id: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
        cost_actual: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = _now_iso()
        fields = ["status = ?", "updated_at = ?"]
        params = [status, now]

        if provider_job_id is not None:
            fields.append("provider_job_id = ?")
            params.append(provider_job_id)

        if result is not None:
            fields.append("result_json = ?")
            params.append(json.dumps(result))

        if cost_actual is not None:
            fields.append("cost_actual = ?")
            params.append(cost_actual)

        params.append(job_id)
        query = f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?"

        with self.conn:
            cursor = self.conn.execute(query, params)
            if cursor.rowcount == 0:
                raise ValueError(f"Job {job_id} not found")

        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        now = _now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE jobs
                SET cancel_requested = 1, status = 'cancelled', updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Job {job_id} not found")
        return self.get_job(job_id)

    def finish_job(
        self, job_id: str, result: Dict[str, Any], cost_actual: float = 0.0
    ) -> Dict[str, Any]:
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # If job was cancelled, do not mark as succeeded
        if job["cancel_requested"] or job["status"] == "cancelled":
            return job

        return self.update_job_status(
            job_id, status="succeeded", result=result, cost_actual=cost_actual
        )
