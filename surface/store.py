import functools
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from surface.schema import init_db


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _synchronized(cls):
    """Class decorator: wrap every public (non-underscore) method so it
    acquires the instance's lock for its whole duration.

    Why this exists: a Store's single sqlite3.Connection is created once
    (e.g. by cli.py's `_launch_board`) and then reused across every Gradio
    board callback. Gradio/Starlette dispatches each synchronous callback to
    a threadpool worker thread, so the SAME Connection object is used from
    MANY different OS threads over its lifetime. schema.py's init_db()
    already passes check_same_thread=False so that cross-thread reuse (one
    thread at a time) doesn't raise sqlite3.ProgrammingError -- but per the
    sqlite3 docs, check_same_thread=False only disables that ownership
    check; it does NOT make one Connection object safe for two threads to
    call into it AT THE SAME TIME. A threadpool-backed web server can
    genuinely have two callbacks mid-execution in overlapping windows (e.g.
    two artifact-card buttons clicked in quick succession, or Refresh racing
    a Revise submission), so this lock provides the actual mutual exclusion
    sqlite3 does not.

    An RLock (not a plain Lock) is required because several public methods
    call other public methods on `self` internally (create_artifact ->
    get_artifact, put_version -> get_artifact/select_version, select_version
    -> get_artifact/get_version, add_dependency -> get_artifact, finish_job
    -> get_job/update_job_status). A plain Lock would deadlock the very
    first time one of those nested calls re-entered on the same thread.
    """
    for name, value in list(vars(cls).items()):
        if name.startswith("_") or not callable(value):
            continue

        @functools.wraps(value)
        def wrapper(self, *args, _fn=value, **kwargs):
            with self._lock:
                return _fn(self, *args, **kwargs)

        setattr(cls, name, wrapper)
    return cls


@_synchronized
class Store:
    def __init__(self, db_path: Union[str, Path, sqlite3.Connection] = ":memory:"):
        self._lock = threading.RLock()
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
        res["freshness"] = self.compute_freshness(artifact_id)
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
            res["freshness"] = self.compute_freshness(res["id"])
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
        inputs: Optional[Dict[str, str]] = None,
        expected_selected_version_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new version for an artifact.

        `inputs`, if given, is an explicit `{upstream_artifact_id:
        upstream_version_id}` mapping declaring exactly what upstream
        versions this new version was built from; it is written verbatim to
        `version_inputs` and used later by `compute_freshness`. Passing an
        explicit `inputs` (even one captured earlier, e.g. at job-dispatch
        time and therefore already stale by the time this call lands) is
        never overridden with "current" values -- that is the whole point:
        it lets an async worker record what it actually saw, not what
        happened to be selected when its result finally arrived.

        When `inputs` is omitted, it is auto-captured from the *current*
        `artifact_dependencies` graph: for every upstream of this artifact,
        whatever that upstream's `selected_version_id` is right now is
        recorded. This is the common synchronous-generation case.
        """
        artifact = self.get_artifact(artifact_id)
        if not artifact:
            raise ValueError(f"Artifact {artifact_id} not found")

        # Validate explicit inputs, if given. Auto-captured inputs (inputs is
        # None) are always internally consistent since they're read straight
        # from artifact_dependencies + current selected_version_id, so this
        # only applies to the explicit-snapshot path.
        if inputs is not None:
            cursor = self.conn.cursor()
            for up_artifact_id, up_version_id in inputs.items():
                up_artifact = self.get_artifact(up_artifact_id)
                if not up_artifact:
                    raise ValueError(
                        f"inputs: upstream artifact {up_artifact_id!r} does not exist"
                    )
                up_version = self.get_version(up_version_id)
                if not up_version or up_version["artifact_id"] != up_artifact_id:
                    raise ValueError(
                        f"inputs: upstream version {up_version_id!r} does not exist or "
                        f"does not belong to artifact {up_artifact_id!r}"
                    )
                cursor.execute(
                    "SELECT 1 FROM artifact_dependencies WHERE upstream_artifact_id = ? AND downstream_artifact_id = ?",
                    (up_artifact_id, artifact_id),
                )
                if not cursor.fetchone():
                    raise ValueError(
                        f"inputs: {up_artifact_id!r} is not a declared dependency of "
                        f"artifact {artifact_id!r}"
                    )

        # The idempotency check (does a version for this source_event_id
        # already exist?) and computing the next version number `n` are both
        # check-then-write: a Python-level SELECT followed, several lines
        # later, by an INSERT. Like select_version's conflict check (see its
        # comment), that is only atomic within this one process's lock -- a
        # second OS process (CLI vs board server, two worker subprocesses)
        # can interleave between the SELECT and the INSERT here. Both races
        # are real:
        #   - two processes both read MAX(n)=k and both try to INSERT n=k+1,
        #     the loser hitting the UNIQUE(artifact_id, n) constraint;
        #   - the same event delivered to two workers both find no existing
        #     row for source_event_id and both insert a version for it.
        # BEGIN IMMEDIATE grabs SQLite's write lock before either read runs,
        # so a concurrent writer blocks (then retries its own read-after-
        # lock, so it correctly sees the loser's committed row) instead of
        # silently racing past it.
        cursor = self.conn.cursor()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if source_event_id:
                cursor.execute(
                    "SELECT * FROM versions WHERE artifact_id = ? AND source_event_id = ?",
                    (artifact_id, source_event_id),
                )
                existing = cursor.fetchone()
                if existing:
                    self.conn.commit()
                    ver_dict = dict(existing)
                    # If selection is requested, ensure it's selected
                    stale_descendants = []
                    if select and artifact["selected_version_id"] != ver_dict["id"]:
                        sel_res = self.select_version(
                            artifact_id, ver_dict["id"],
                            expected_selected_version_id=expected_selected_version_id,
                        )
                        if not sel_res.get("ok", True):
                            return sel_res
                        stale_descendants = sel_res.get("stale_descendants", [])
                    return {
                        "ok": True,
                        "version_id": ver_dict["id"],
                        "version": ver_dict["n"],
                        "stale_descendants": stale_descendants,
                    }

            # Determine next version number n
            cursor.execute(
                "SELECT MAX(n) FROM versions WHERE artifact_id = ?", (artifact_id,)
            )
            max_n = cursor.fetchone()[0]
            next_n = (max_n or 0) + 1

            version_id = f"ver_{uuid.uuid4().hex[:8]}"
            params_json = json.dumps(params) if params is not None else None
            now = _now_iso()

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

            # Record provenance for this version -- who it was built from.
            if inputs is not None:
                input_rows = dict(inputs)
            else:
                cursor.execute(
                    "SELECT upstream_artifact_id FROM artifact_dependencies WHERE downstream_artifact_id = ?",
                    (artifact_id,),
                )
                dep_rows = cursor.fetchall()
                input_rows = {}
                for dep_row in dep_rows:
                    up_id = dep_row[0]
                    cursor.execute(
                        "SELECT selected_version_id FROM artifacts WHERE id = ?", (up_id,)
                    )
                    up_row = cursor.fetchone()
                    if up_row and up_row[0]:
                        input_rows[up_id] = up_row[0]

            for up_artifact_id, up_version_id in input_rows.items():
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO version_inputs (version_id, upstream_artifact_id, upstream_version_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (version_id, up_artifact_id, up_version_id, now),
                )

            self.conn.commit()
        except sqlite3.IntegrityError:
            # Lost the (artifact_id, n) or source_event_id race despite
            # BEGIN IMMEDIATE -- shouldn't happen given the write lock is
            # held for the whole check-then-write, but if it ever does,
            # roll back and surface a clear conflict rather than a raw
            # constraint-violation traceback.
            self.conn.rollback()
            return {"ok": False, "error": "conflict", "detail": "version write race"}
        except Exception:
            self.conn.rollback()
            raise

        stale_descendants = []
        if select:
            sel_res = self.select_version(
                artifact_id, version_id,
                expected_selected_version_id=expected_selected_version_id,
            )
            if not sel_res.get("ok", True):
                # The version itself was already created and committed above;
                # only the "make it current" step lost the race. Report the
                # conflict; the version exists and the caller can retry the
                # selection (or accept the version un-selected) rather than
                # having a create call silently discard a concurrent
                # revision.
                sel_res["version_id"] = version_id
                sel_res["version"] = next_n
                return sel_res
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

        old_selected = artifact["selected_version_id"]
        now = _now_iso()

        # The conflict check and the write must be one atomic statement, not a
        # Python-level read (get_artifact above) followed by a separate write.
        # Two separate statements are only safe within a single Python-level
        # critical section (this class's @_synchronized lock, which only
        # guards this one process); a second OS process (the CLI and the
        # board server are separate processes sharing one sqlite file) can
        # write between the read and the write here, and the second writer's
        # UPDATE would silently overwrite the first with no error. Folding
        # the check into the UPDATE's WHERE clause makes SQLite's own
        # cross-connection write serialization do the atomicity, not our
        # Python code.
        stale_descendants = []
        with self.conn:
            if expected_selected_version_id is not None:
                # NULL can't be compared with '=', so branch on IS vs = ?.
                if expected_selected_version_id == "":
                    where_clause = "id = ? AND selected_version_id IS NULL"
                    params: tuple = (artifact_id,)
                else:
                    where_clause = "id = ? AND selected_version_id = ?"
                    params = (artifact_id, expected_selected_version_id)
                cursor = self.conn.execute(
                    f"UPDATE artifacts SET selected_version_id = ?, updated_at = ? WHERE {where_clause}",
                    (version_id, now, *params),
                )
                if cursor.rowcount == 0:
                    # Either the artifact vanished (already checked above,
                    # so not that) or selected_version_id no longer matches
                    # what the caller expected -- re-read to report the
                    # actual current value to the caller.
                    current = self.get_artifact(artifact_id)
                    return {
                        "ok": False,
                        "error": "conflict",
                        "current_selected_version_id": current["selected_version_id"] if current else None,
                    }
            else:
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
        Recursively traverse downstream dependent artifacts and emit 'stale'
        system events for affected artifacts, notifying that something
        upstream changed.

        This does NOT mutate any artifact's `status` column -- lifecycle
        status (draft/in_review/approved/...) is a human/worker decision and
        is never touched by staleness propagation. Whether an artifact is
        actually stale is a computed fact, derived on read by
        `compute_freshness` from `version_inputs`, not stored here. This
        method only fans out a notification so downstream consumers (e.g. a
        worker that wants to re-run) know something upstream moved.

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

    def compute_freshness(
        self, artifact_id: str, _cache: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Read-only, computed-on-every-call freshness check.

        An artifact is fresh iff, for every CURRENT real dependency it has
        (`artifact_dependencies` naming it as downstream) -- not merely
        whatever happens to be recorded in `version_inputs` -- there is a
        `version_inputs` row for that upstream on the selected version, that
        row's recorded upstream version id still matches the upstream's
        *current* `selected_version_id`, AND that upstream artifact is
        itself fresh (transitive staleness). A real dependency with NO
        `version_inputs` row at all (missing provenance -- e.g. the version
        predates input capture, or was created with `inputs={}`) is treated
        as NOT fresh: "no record" is never silently read as "known fresh".
        An artifact with no current dependencies has nothing to be stale
        against and stays fresh.

        Nothing here is stored or cached; it is purely derived from
        `artifact_dependencies` + `version_inputs` + `selected_version_id`
        at call time, so reverting an upstream selection back to a recorded
        version id makes downstream fresh again with no extra bookkeeping.

        `stale_because` is a list of `{"upstream_artifact_id": ..., "reason":
        "changed" | "no_record"}` dicts -- `"changed"` means a
        `version_inputs` row exists but no longer matches (directly or
        transitively); `"no_record"` means no `version_inputs` row exists
        for a real current dependency at all.

        `_cache` memoizes each artifact's result within one top-level call --
        both so two sibling branches that share an ancestor (A depends on B
        and C, both depend on D) each see D's real computed result instead
        of the second visitor treating an in-progress/already-seen node as
        trivially fresh, and as a defensive guard against runaway recursion
        if a cycle ever slipped past `add_dependency`'s edge-level cycle
        check. A node still being evaluated higher up the call chain (a true
        cycle) is reported fresh for that one recursive edge only, to break
        the loop -- it is not cached, so a later sibling re-evaluates it
        properly.
        """
        if _cache is None:
            _cache = {}
        if artifact_id in _cache:
            cached = _cache[artifact_id]
            if cached is None:
                # A true cycle: this artifact is still being evaluated higher
                # up this same call chain. Report fresh for this one
                # recursive edge only (not cached) so the loop terminates,
                # without poisoning the real result for this artifact.
                return {"artifact_id": artifact_id, "fresh": True, "stale_because": []}
            return cached
        _cache[artifact_id] = None  # mark in-progress for cycle detection

        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT selected_version_id FROM artifacts WHERE id = ?", (artifact_id,)
        )
        row = cursor.fetchone()
        if not row or not row["selected_version_id"]:
            result = {"artifact_id": artifact_id, "fresh": True, "stale_because": []}
            _cache[artifact_id] = result
            return result

        selected_version_id = row["selected_version_id"]

        # Current real dependency edges -- the source of truth for what
        # SHOULD have a provenance record, independent of what actually got
        # recorded in version_inputs.
        cursor.execute(
            "SELECT upstream_artifact_id FROM artifact_dependencies WHERE downstream_artifact_id = ?",
            (artifact_id,),
        )
        current_upstream_ids = [r["upstream_artifact_id"] for r in cursor.fetchall()]

        if not current_upstream_ids:
            result = {"artifact_id": artifact_id, "fresh": True, "stale_because": []}
            _cache[artifact_id] = result
            return result

        cursor.execute(
            "SELECT upstream_artifact_id, upstream_version_id FROM version_inputs WHERE version_id = ?",
            (selected_version_id,),
        )
        recorded = {
            r["upstream_artifact_id"]: r["upstream_version_id"]
            for r in cursor.fetchall()
        }

        stale_because: List[Dict[str, str]] = []
        for up_artifact_id in current_upstream_ids:
            if up_artifact_id not in recorded:
                # Real dependency, but no provenance record for it at all --
                # this is Gap 1: missing record must not read as fresh.
                stale_because.append(
                    {"upstream_artifact_id": up_artifact_id, "reason": "no_record"}
                )
                continue

            recorded_upstream_version_id = recorded[up_artifact_id]

            cursor.execute(
                "SELECT selected_version_id FROM artifacts WHERE id = ?",
                (up_artifact_id,),
            )
            up_row = cursor.fetchone()
            current_upstream_version_id = up_row["selected_version_id"] if up_row else None

            direct_stale = current_upstream_version_id != recorded_upstream_version_id
            upstream_freshness = self.compute_freshness(up_artifact_id, _cache=_cache)

            if direct_stale or not upstream_freshness["fresh"]:
                stale_because.append(
                    {"upstream_artifact_id": up_artifact_id, "reason": "changed"}
                )

        result = {
            "artifact_id": artifact_id,
            "fresh": len(stale_because) == 0,
            "stale_because": stale_because,
        }
        _cache[artifact_id] = result
        return result

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

    ACTIVE_EVENT_STATUSES = ("pending", "processing")

    def list_active_events(
        self, artifact_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Every event that has been accepted but not yet resolved.

        "Active" means `status IN ('pending', 'processing')`: the event is
        either sitting in the inbox waiting for a worker to claim it, or a
        worker has claimed it and is mid-turn. Either way the user's click
        has landed in the store but its effect is not visible yet, which is
        exactly the state a renderer has to be able to show ("2 queued"
        rather than a static "Ready").

        The store owns this query because the store is the source of truth
        for the inbox (SPEC principle 1); a renderer must not reach into
        `store.conn` to derive it.

        Args:
            artifact_id: restrict to events referencing this artifact. Pass
                None for every active event in the project, including the
                artifact-less ones.

        Returns:
            Event dicts (same shape as `get_event`, payload decoded), oldest
            first, so `[0]` is the longest-waiting item.
        """
        cursor = self.conn.cursor()
        placeholders = ",".join("?" for _ in self.ACTIVE_EVENT_STATUSES)
        params: List[Any] = list(self.ACTIVE_EVENT_STATUSES)
        sql = f"SELECT * FROM events WHERE status IN ({placeholders})"
        if artifact_id is not None:
            sql += " AND artifact_id = ?"
            params.append(artifact_id)
        sql += " ORDER BY created_at ASC, id ASC"
        cursor.execute(sql, params)

        events = []
        for row in cursor.fetchall():
            res = dict(row)
            res["payload"] = (
                json.loads(res["payload_json"]) if res.get("payload_json") else {}
            )
            events.append(res)
        return events

    def count_active_events(
        self, artifact_id: Optional[str] = None
    ) -> Dict[str, int]:
        """Counts of unresolved inbox work, split by status.

        Returns a dict with `pending`, `processing` and `total` keys (always
        present, zero when empty) so callers can render "3 queued, 1 working"
        without post-processing a list.
        """
        counts = {"pending": 0, "processing": 0, "total": 0}
        cursor = self.conn.cursor()
        placeholders = ",".join("?" for _ in self.ACTIVE_EVENT_STATUSES)
        params: List[Any] = list(self.ACTIVE_EVENT_STATUSES)
        sql = (
            f"SELECT status, COUNT(*) FROM events WHERE status IN ({placeholders})"
        )
        if artifact_id is not None:
            sql += " AND artifact_id = ?"
            params.append(artifact_id)
        sql += " GROUP BY status"
        cursor.execute(sql, params)
        for status, count in cursor.fetchall():
            counts[status] = count
            counts["total"] += count
        return counts

    def reclaim_expired_leases(self) -> List[str]:
        """Return processing events with an expired lease to 'pending' and
        release their artifact locks, WITHOUT claiming a new event.

        This is the same lease-recovery side effect claim_next_event()
        performs as its first step, exposed standalone so a caller (e.g. a
        recovery/diagnostic CLI command) can trigger lease reclamation
        without also claiming and leasing an unrelated healthy event as a
        side effect of calling claim_next_event() purely for its cleanup
        behavior.

        Returns:
            List of event ids that were reclaimed.
        """
        now = datetime.now(timezone.utc).isoformat()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id FROM events WHERE status = 'processing' AND lease_until IS NOT NULL AND lease_until < ?",
            (now,),
        )
        expired_events = cursor.fetchall()
        reclaimed = []
        for (exp_id,) in expired_events:
            cursor.execute(
                "UPDATE events SET status = 'pending', claimed_by = NULL, claimed_at = NULL, lease_until = NULL WHERE id = ?",
                (exp_id,),
            )
            cursor.execute(
                "DELETE FROM artifact_locks WHERE event_id = ?",
                (exp_id,),
            )
            reclaimed.append(exp_id)
        cursor.execute(
            "DELETE FROM artifact_locks WHERE lease_until < ?",
            (now,),
        )
        self.conn.commit()
        return reclaimed

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

    def list_jobs_by_status(self, status: str) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE status = ?", (status,))
        rows = cursor.fetchall()
        results = []
        for row in rows:
            res = dict(row)
            res["cancel_requested"] = bool(res["cancel_requested"])
            res["request"] = json.loads(res["request_json"]) if res.get("request_json") else {}
            res["result"] = json.loads(res["result_json"]) if res.get("result_json") else {}
            results.append(res)
        return results

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
        """Request cancellation of a job.

        Only sets cancel_requested here; status transitions to 'cancelled' once
        the poller has actually told the provider to cancel (SPEC section 28).
        Setting status='cancelled' immediately would remove the job from the
        poller's queued/running scan before provider.cancel() is ever called.
        """
        now = _now_iso()
        with self.conn:
            cursor = self.conn.execute(
                """
                UPDATE jobs
                SET cancel_requested = 1, updated_at = ?
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
