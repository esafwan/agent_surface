"""
Supervisor: event claim/dispatch loop for worker coordination.

Per SPEC section 22: Supervisor owns worker lifecycle and inbox dispatch.
Loop:
1. ensure worker exists
2. claim next eligible event
3. acquire artifact processing lease when artifact-scoped
4. construct bounded context
5. send event to worker
6. worker uses store tools
7. validate durable outcome
8. ack event
9. release lease
10. continue

Supervisor MUST be restartable and SHOULD heartbeat.
It MUST detect worker exit, transport failure, expired event lease, turn timeout,
explicit cancellation, and recycling threshold.
"""

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from surface.store import Store
from surface.stages.config import StageConfig


logger = logging.getLogger(__name__)


class SupervisorConfig:
    """Configuration for Supervisor."""

    def __init__(
        self,
        worker_id: str = "supervisor_worker",
        lease_seconds: int = 60,
        turn_timeout: int = 300,
        event_batch_size: int = 1,
        recycle_after_events: Optional[int] = None,
        recycle_after_tokens: Optional[int] = None,
        max_attempts: int = 3,
        idle_sleep_seconds: float = 0.2,
    ):
        """
        Initialize supervisor configuration.

        Args:
            worker_id: Identifier for this supervisor's worker.
            lease_seconds: Lease duration for claimed events (seconds).
            turn_timeout: Maximum time for a worker turn (seconds).
            event_batch_size: How many events to process per loop (1 = serial).
            recycle_after_events: Recycle worker after this many events.
            recycle_after_tokens: Recycle worker after this many LLM tokens.
            max_attempts: Consecutive failed dispatch attempts allowed for an
                event (tracked via the store's attempt_count) before it is
                permanently failed. Per SPEC section 40, a worker/transport
                crash before ack should let the lease expire and retry rather
                than permanently killing the event on the first failure.
            idle_sleep_seconds: Sleep duration in run_loop when the inbox is
                empty, to avoid spinning a CPU core at 100%.
        """
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.turn_timeout = turn_timeout
        self.event_batch_size = event_batch_size
        self.recycle_after_events = recycle_after_events
        self.recycle_after_tokens = recycle_after_tokens
        self.max_attempts = max_attempts
        self.idle_sleep_seconds = idle_sleep_seconds


class Supervisor:
    """
    Supervisor: orchestrates event claim, dispatch to worker, and result persistence.

    Restartable from SQLite store; no in-memory-only state.
    """

    def __init__(
        self,
        store: Store,
        transport: Any,
        stage_config: StageConfig,
        config: Optional[SupervisorConfig] = None,
        project_id: str = "default",
        pool_size: int = 1,
    ):
        """
        Initialize Supervisor.

        Args:
            store: Store instance for persistence.
            transport: WorkerTransport implementation (NativeStream or Resume).
            stage_config: StageConfig instance defining stages/actions/dependencies.
            config: SupervisorConfig (or None for defaults).
            project_id: Identifier for the project this supervisor drives.
            pool_size: Number of concurrent worker sessions (SPEC section 25).
                The default of 1 preserves the exact serial Phase 0 behaviour:
                every claim/dispatch/ack happens inline on the calling thread.
                With pool_size > 1 the supervisor keeps up to pool_size worker
                sessions and dispatches events for DIFFERENT artifacts to them
                concurrently; same-artifact events remain strictly serialized
                by the store's artifact_locks table (see `run_pool_once`).
        """
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")

        self.store = store
        self.transport = transport
        self.stage_config = stage_config
        self.config = config or SupervisorConfig()

        self.worker_session = None
        self.project_id = project_id
        self.event_count = 0
        # Accumulated LLM token usage reported by callers of the dispatch
        # methods. See `claim_and_dispatch_event`'s `token_count` argument:
        # nothing in this codebase reports real token usage yet, so this
        # counter stays at 0 and `recycle_after_tokens` is wired-but-inert
        # until a worker/transport starts reporting usage.
        self.token_count = 0
        self._last_session_ref: Optional[str] = None

        self.pool_size = pool_size
        # Slots 1..pool_size-1; slot 0 is `self.worker_session` so that the
        # serial path (and every existing caller/test) is untouched.
        self._pool_slots: Dict[int, Dict[str, Any]] = {
            i: {"session": None, "ref": None} for i in range(1, pool_size)
        }

        # Guards the interrupt bookkeeping below, which is read/written from
        # both the caller's thread (request_worker_interrupt) and pool worker
        # threads (dispatch completion).
        self._state_lock = threading.RLock()
        # session_id -> event_id currently being dispatched on that session.
        self._in_flight: Dict[str, str] = {}
        # session_ids whose in-flight turn was interrupted; consumed by the
        # dispatch path so the turn is never acked as a success.
        self._interrupted_sessions: set = set()

    def start_worker(self, project_context: Optional[Dict[str, Any]] = None) -> bool:
        """
        Ensure a worker session exists.

        Args:
            project_context: Optional context dict for the project.

        Returns:
            True if worker started successfully, False otherwise.
        """
        if self.worker_session and self.transport.is_alive(self.worker_session):
            return True

        try:
            context = project_context or {
                "project_id": self.project_id,
                "stage_config": self.stage_config.raw,
            }
            if self._last_session_ref is not None:
                # Per SPEC section 21/23: prefer resuming a prior session over a
                # cold start so a recycled/restarted supervisor rehydrates rather
                # than losing worker continuity.
                self.worker_session = self.transport.resume(
                    self._last_session_ref, context
                )
            else:
                self.worker_session = self.transport.start(context)
            self._last_session_ref = self.worker_session.session_id
            logger.info(f"Worker started: {self.worker_session.session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to start worker: {e}")
            return False

    def stop_worker(self) -> None:
        """Stop the worker session."""
        if self.worker_session:
            try:
                self.transport.close(self.worker_session)
                logger.info(f"Worker stopped: {self.worker_session.session_id}")
            except Exception as e:
                logger.error(f"Error stopping worker: {e}")
            finally:
                self.worker_session = None

    def _build_rehydration_summary(self) -> Dict[str, Any]:
        """
        Build a compact rehydration summary from store.

        Per SPEC section 23: A new/recycled worker MUST receive a compact summary
        derived from store, not replayed chat history.

        Returns:
            Rehydration summary dict.
        """
        artifacts = self.store.list_artifacts()
        stage_counts = {}

        for stage_id in self.stage_config.stage_order:
            stage_artifacts = [a for a in artifacts if a["stage"] == stage_id]
            stage_counts[stage_id] = {
                "total": len(stage_artifacts),
                "draft": len([a for a in stage_artifacts if a["status"] == "draft"]),
                "review": len([a for a in stage_artifacts if a["status"] == "review"]),
                "approved": len([a for a in stage_artifacts if a["status"] == "approved"]),
                "stale": len([a for a in stage_artifacts if a["status"] == "stale"]),
                "generating": len([a for a in stage_artifacts if a["status"] == "generating"]),
            }

        # Read-only introspection queries. These use the store's connection
        # directly (the same pattern already used elsewhere in this module)
        # rather than adding read-only helpers to store.py.
        cursor = self.store.conn.cursor()

        # `recent_event_ids` must reflect genuinely RECENT activity, not just
        # the not-yet-finished queue: acked/failed events that just completed
        # are the most useful thing for a rehydrating worker to know about.
        # Tie-break on rowid so events created within the same timestamp
        # resolution still come back newest-first and deterministically.
        cursor.execute(
            "SELECT id FROM events ORDER BY created_at DESC, rowid DESC LIMIT 10"
        )
        recent_event_ids = [row[0] for row in cursor.fetchall()]

        # Actual event-queue depth. This used to be mislabeled: it counted
        # artifacts with status == 'generating', which is an artifact count,
        # not an event count.
        cursor.execute("SELECT status, COUNT(*) FROM events GROUP BY status")
        event_status_counts = {row[0]: row[1] for row in cursor.fetchall()}

        summary: Dict[str, Any] = {
            "project_id": self.project_id,
            "stage_config_id": self.stage_config.id,
            "stage_counts": stage_counts,
            "artifact_count": len(artifacts),
            "pending_event_count": event_status_counts.get("pending", 0),
            "processing_event_count": event_status_counts.get("processing", 0),
            "failed_event_count": event_status_counts.get("failed", 0),
            "generating_artifact_count": sum(
                1 for a in artifacts if a["status"] == "generating"
            ),
            "recent_event_ids": recent_event_ids,
        }

        # SPEC section 23 lists `important_constraints` in its example summary
        # shape. The only place this architecture lets anyone DECLARE a
        # constraint is the stage config, which carries `budget` limits and
        # `completion` requirements. Those are surfaced here verbatim. There is
        # deliberately no free-text constraints source: nothing in the current
        # architecture lets a worker or operator declare one, and inventing a
        # fake source would be worse than omitting the key, so when the stage
        # config declares neither, the key is omitted entirely.
        constraints: Dict[str, Any] = {}
        if self.stage_config.budget:
            constraints["budget"] = self.stage_config.budget
        if self.stage_config.completion:
            constraints["completion"] = self.stage_config.completion
        if constraints:
            summary["important_constraints"] = constraints

        return summary

    def _build_event_context(
        self,
        event: Dict[str, Any],
        artifact: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Build a bounded event context for the worker.

        Per SPEC section 23: Include project ID/config, event, current artifact,
        directly relevant upstream/downstream IDs, and bounded project summary.

        Args:
            event: Event from store.
            artifact: Artifact associated with event (optional).

        Returns:
            Event context dict for the worker.
        """
        context = {
            "event_id": event["id"],
            "type": event["type"],
            "project_id": event["project_id"],
            "payload": event.get("payload", {}),
            "artifact_id": event.get("artifact_id"),
            "config": self.stage_config.raw,
            "summary": self._build_rehydration_summary(),
        }

        if artifact:
            context["artifact"] = artifact
            context["artifact_versions"] = self.store.list_versions(artifact["id"])

            # Include directly relevant upstream/downstream IDs
            graph = self.store.get_graph(artifact["id"])
            context["upstream_ids"] = [
                u["upstream_artifact_id"] for u in graph["upstreams"]
            ]
            context["downstream_ids"] = [
                d["downstream_artifact_id"] for d in graph["downstreams"]
            ]

            # Include upstream artifacts for context, filtering out any ids that
            # no longer resolve (e.g. deleted between graph read and lookup).
            context["upstream_artifacts"] = [
                a for a in (
                    self.store.get_artifact(uid)
                    for uid in context["upstream_ids"]
                ) if a is not None
            ]

        return context

    # -------------------------------------------------------------------------
    # Recycling (SPEC section 22: "recycling threshold")
    # -------------------------------------------------------------------------

    def _recycle_thresholds_reached(self) -> Optional[str]:
        """
        Return a human-readable reason if a recycle threshold is reached.

        Returns None when no threshold is configured or reached.
        """
        cfg = self.config
        if cfg.recycle_after_events and self.event_count >= cfg.recycle_after_events:
            return f"{self.event_count} events >= recycle_after_events={cfg.recycle_after_events}"
        if cfg.recycle_after_tokens and self.token_count >= cfg.recycle_after_tokens:
            return f"{self.token_count} tokens >= recycle_after_tokens={cfg.recycle_after_tokens}"
        return None

    def _maybe_recycle(self) -> bool:
        """
        Recycle worker session(s) if a threshold is reached.

        Recycling happens BETWEEN turns (before the next event is claimed),
        never mid-turn, so an in-flight worker turn is never torn down under
        itself. The next `start_worker()` recreates (or resumes) the session.

        Returns:
            True if a recycle was performed.
        """
        reason = self._recycle_thresholds_reached()
        if not reason:
            return False

        logger.info(f"Recycling worker session(s): {reason}")
        self.stop_all_workers()
        self.event_count = 0
        self.token_count = 0
        return True

    def stop_all_workers(self) -> None:
        """Stop the primary worker session and every pooled session."""
        self.stop_worker()
        for idx, slot in self._pool_slots.items():
            session = slot["session"]
            if session is None:
                continue
            try:
                self.transport.close(session)
            except Exception as e:
                logger.error(f"Error stopping pooled worker {idx}: {e}")
            finally:
                slot["session"] = None

    def _ensure_slot_session(self, idx: int) -> Optional[Any]:
        """
        Ensure the worker session for pool slot `idx` exists and is alive.

        Slot 0 is the primary session (`self.worker_session`) so the serial
        path is unchanged. Slots >= 1 are pool-only sessions, each its own
        `transport.start()` / `transport.resume()` call.

        A poisoned session (e.g. one killed by NativeStreamTransport after a
        turn timeout) reports `is_alive() == False` and is therefore replaced
        here rather than reused.
        """
        if idx == 0:
            return self.worker_session if self.start_worker() else None

        slot = self._pool_slots[idx]
        session = slot["session"]
        if session is not None and self.transport.is_alive(session):
            return session

        context = {
            "project_id": self.project_id,
            "stage_config": self.stage_config.raw,
            "pool_slot": idx,
        }
        try:
            if slot["ref"] is not None:
                session = self.transport.resume(slot["ref"], context)
            else:
                session = self.transport.start(context)
        except Exception as e:
            logger.error(f"Failed to start pooled worker {idx}: {e}")
            return None

        slot["session"] = session
        slot["ref"] = session.session_id
        logger.info(f"Pooled worker {idx} started: {session.session_id}")
        return session

    # -------------------------------------------------------------------------
    # Dispatch
    # -------------------------------------------------------------------------

    def _send_with_interrupt_tracking(
        self, session: Any, event: Dict[str, Any], event_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Run one worker turn, tracking it so it can be interrupted on demand.

        If `request_worker_interrupt()` fires against this session while the
        turn is in flight, the turn's result is forced to a failure result even
        if the worker happened to reply `ok: True`. The caller then routes it
        through `_handle_dispatch_failure`, so the event is NOT acked and is
        retried via the normal bounded-retry / lease-expiry path.
        """
        session_id = getattr(session, "session_id", None)
        with self._state_lock:
            if session_id is not None:
                self._in_flight[session_id] = event["id"]
                self._interrupted_sessions.discard(session_id)

        try:
            result = self.transport.send_event(session, event_context)
        finally:
            with self._state_lock:
                if session_id is not None:
                    self._in_flight.pop(session_id, None)
                    was_interrupted = session_id in self._interrupted_sessions
                    self._interrupted_sessions.discard(session_id)
                else:
                    was_interrupted = False

        if was_interrupted:
            logger.warning(
                f"Turn for event {event['id']} was interrupted; "
                "treating as a dispatch failure (not acking)"
            )
            return {"ok": False, "error": "interrupted", "interrupted": True}
        return result

    def claim_and_dispatch_event(self, token_count: int = 0) -> Optional[Dict[str, Any]]:
        """
        Claim the next eligible event and dispatch it to the worker.

        Per SPEC section 22: Loop steps 1-9 (claim, acquire lease, construct context,
        send event, validate outcome, ack).

        Args:
            token_count: Optional LLM token usage to attribute to this turn,
                accumulated into `self.token_count` and compared against
                `SupervisorConfig.recycle_after_tokens`. NOTE: no caller in
                this codebase supplies a real value today — no worker or
                transport reports token usage anywhere — so the default of 0
                means "unknown/not tracked" and `recycle_after_tokens` is
                wired but inert until a worker starts reporting usage. This is
                an intentional, documented state, not a bug.

        Returns:
            The claimed event dict, or None if no event available or error occurred.
        """
        # Recycle BETWEEN turns, before claiming the next event, so a session
        # is never torn down mid-turn.
        self._maybe_recycle()

        # Ensure worker exists
        if not self.start_worker():
            logger.error("Worker failed to start; skipping dispatch")
            return None

        # Step 2: Claim next eligible event
        event = self.store.claim_next_event(
            self.config.worker_id,
            lease_seconds=self.config.lease_seconds,
        )
        if not event:
            logger.debug("No eligible event to claim")
            return None

        logger.info(f"Claimed event: {event['id']} (type={event['type']})")

        # Get associated artifact if any
        artifact = None
        if event["artifact_id"]:
            artifact = self.store.get_artifact(event["artifact_id"])

        try:
            # Step 4: Construct bounded event context
            event_context = self._build_event_context(event, artifact)

            # Step 5: Send event to worker
            result = self._send_with_interrupt_tracking(
                self.worker_session, event, event_context
            )

            # Step 7: Validate durable outcome
            if not result.get("ok"):
                error_msg = result.get("error", "Unknown error")
                logger.error(f"Worker turn failed for event {event['id']}: {error_msg}")
                self._handle_dispatch_failure(event, error_msg)
                return event

            # Worker succeeded; step 8: ack event
            self.store.ack_event(event["id"])
            logger.info(f"Acked event: {event['id']}")

            self.event_count += 1
            self.token_count += max(0, int(token_count or 0))

            return event

        except Exception as e:
            logger.error(f"Error processing event {event['id']}: {e}")
            self._handle_dispatch_failure(event, str(e))
            return event

    def _handle_dispatch_failure(self, event: Dict[str, Any], error_msg: str) -> None:
        """
        Handle a failed dispatch (transport error or worker ok:false).

        Per SPEC section 40: "worker crash before ack: lease expires; event
        retries." A single transient failure MUST NOT permanently kill the
        event. `claim_next_event` already incremented `attempt_count` for
        this claim, so once that count reaches `config.max_attempts` we treat
        the failure as permanent and call `store.fail_event`. Below that
        threshold, the store has no public "return to pending now" API, so we
        deliberately leave the event in its claimed `processing` state with
        its lease intact: the lease will expire naturally and
        `claim_next_event` will reset it to `pending` for a future claim,
        which is the retry path.

        Args:
            event: The event dict as returned by claim_next_event (contains
                the post-increment attempt_count).
            error_msg: Error description for logging/eventual fail_event call.
        """
        attempt_count = event.get("attempt_count") or 0
        if attempt_count >= self.config.max_attempts:
            logger.error(
                f"Event {event['id']} failed {attempt_count} times "
                f"(max_attempts={self.config.max_attempts}); permanently failing"
            )
            try:
                self.store.fail_event(event["id"], error_msg)
            except Exception as ex:
                logger.error(f"Failed to mark event as failed: {ex}")
        else:
            logger.warning(
                f"Event {event['id']} failed (attempt {attempt_count}/"
                f"{self.config.max_attempts}); leaving lease to expire for retry"
            )

    # -------------------------------------------------------------------------
    # Bounded per-artifact worker pool (SPEC section 25)
    # -------------------------------------------------------------------------

    def run_pool_once(self, token_counts: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
        """
        Claim up to `pool_size` events and dispatch them concurrently.

        Concurrency model (deliberate, and the reason this is not "N
        independent loops"):

        * The `Store` wraps a single `sqlite3.Connection` created with the
          stdlib default `check_same_thread=True` (surface/schema.py
          `init_db`), so it is NOT safe to call from multiple threads. Every
          store call here therefore happens on THIS thread: claim, context
          build, ack and fail are all serialized.
        * Only `transport.send_event` — the slow, I/O-bound worker turn — runs
          on pool threads, one per session. That is where the real concurrency
          gain is.
        * Same-artifact serialization is enforced by the store, not by luck:
          `Store.claim_next_event` skips any pending event whose `artifact_id`
          has a live row in `artifact_locks`, and inserts such a row as part
          of the same claim. The lock is only removed by `ack_event`,
          `fail_event`, or lease expiry. So while artifact X's event is in
          flight, a second claim in the SAME batch cannot pick up another
          event for X — it is simply skipped and left pending.
        * Unrelated artifacts run concurrently; events with no artifact_id take
          no lock and may run concurrently with anything.

        Args:
            token_counts: Optional map of event_id -> token usage. As with
                `claim_and_dispatch_event`, nothing reports real token usage
                today, so this is normally None.

        Returns:
            The list of events that were claimed and dispatched this batch.
        """
        self._maybe_recycle()

        claimed: List[tuple] = []  # (slot_idx, session, event, context)
        for idx in range(self.pool_size):
            session = self._ensure_slot_session(idx)
            if session is None:
                logger.error(f"Pool slot {idx} has no worker; skipping")
                continue

            event = self.store.claim_next_event(
                # Distinct worker ids per slot make the lock owner legible in
                # the store; the lock itself is keyed by artifact_id.
                f"{self.config.worker_id}#{idx}" if idx else self.config.worker_id,
                lease_seconds=self.config.lease_seconds,
            )
            if not event:
                break  # nothing (more) eligible right now

            artifact = None
            if event["artifact_id"]:
                artifact = self.store.get_artifact(event["artifact_id"])
            try:
                context = self._build_event_context(event, artifact)
            except Exception as e:
                logger.error(f"Error building context for {event['id']}: {e}")
                self._handle_dispatch_failure(event, str(e))
                continue
            claimed.append((idx, session, event, context))

        if not claimed:
            return []

        results: Dict[str, Dict[str, Any]] = {}
        if len(claimed) == 1:
            _, session, event, context = claimed[0]
            results[event["id"]] = self._safe_send(session, event, context)
        else:
            with ThreadPoolExecutor(max_workers=len(claimed)) as pool:
                futures = {
                    pool.submit(self._safe_send, session, event, context): event
                    for (_idx, session, event, context) in claimed
                }
                for future, event in futures.items():
                    results[event["id"]] = future.result()

        # All store mutations back on this thread.
        dispatched = []
        for (_idx, _session, event, _context) in claimed:
            result = results[event["id"]]
            if result.get("ok"):
                try:
                    self.store.ack_event(event["id"])
                    self.event_count += 1
                    if token_counts:
                        self.token_count += max(0, int(token_counts.get(event["id"], 0)))
                    logger.info(f"Acked event: {event['id']}")
                except Exception as e:
                    logger.error(f"Failed to ack {event['id']}: {e}")
            else:
                self._handle_dispatch_failure(
                    event, result.get("error", "Unknown error")
                )
            dispatched.append(event)
        return dispatched

    def _safe_send(
        self, session: Any, event: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Run one worker turn on a pool thread, never raising."""
        try:
            return self._send_with_interrupt_tracking(session, event, context)
        except Exception as e:
            logger.error(f"Error dispatching event {event['id']}: {e}")
            return {"ok": False, "error": str(e)}

    def run_loop(self, max_iterations: Optional[int] = None) -> None:
        """
        Run the main supervision loop.

        Per SPEC section 22: Loop continuously (or for max_iterations).

        Args:
            max_iterations: Max number of claim/dispatch cycles (None = infinite).
        """
        import time

        iteration = 0
        try:
            while max_iterations is None or iteration < max_iterations:
                if self.pool_size > 1:
                    event = self.run_pool_once() or None
                else:
                    event = self.claim_and_dispatch_event()
                if event is None:
                    # Inbox empty (or worker failed to start): avoid spinning
                    # a CPU core at 100% while idle.
                    time.sleep(self.config.idle_sleep_seconds)
                iteration += 1
        except KeyboardInterrupt:
            logger.info("Supervisor loop interrupted")
        finally:
            self.stop_all_workers()

    def run_once(self) -> bool:
        """
        Run one iteration of the claim/dispatch loop.

        Returns:
            True if an event was processed, False otherwise.
        """
        if self.pool_size > 1:
            return bool(self.run_pool_once())
        event = self.claim_and_dispatch_event()
        return event is not None

    def request_worker_interrupt(
        self, session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Public "cancel whatever the worker is doing right now" entry point.

        This is worker-TURN cancellation (SPEC section 28), distinct from job
        cancellation (poller.py). It is safe to call from another thread while
        a dispatch is in flight.

        Semantics:
        * If no worker session exists, this is NOT an error: it returns
          `{"ok": True, "interrupted": False, "message": "nothing to interrupt"}`.
        * Otherwise the transport's `interrupt()` is called for each targeted
          session. Any session that had an in-flight turn is recorded, and when
          that turn returns, `_send_with_interrupt_tracking` converts its result
          into a failure result. The event is therefore NEVER acked; it flows
          through `_handle_dispatch_failure`, i.e. it is retried via the
          existing bounded-retry / lease-expiry mechanism and only permanently
          failed once `attempt_count` reaches `config.max_attempts`.
        * Durable writes the worker already committed remain, per SPEC 28.

        Args:
            session_id: Optionally target one session; default is all active
                sessions (the primary plus any pooled ones).

        Returns:
            Result dict: ok, interrupted (bool), sessions (list of per-session
            results), and for the no-session case a "message".
        """
        sessions = []
        if self.worker_session is not None:
            sessions.append(self.worker_session)
        for slot in self._pool_slots.values():
            if slot["session"] is not None:
                sessions.append(slot["session"])

        if session_id is not None:
            sessions = [
                s for s in sessions if getattr(s, "session_id", None) == session_id
            ]

        if not sessions:
            logger.info("request_worker_interrupt: no active worker session")
            return {
                "ok": True,
                "interrupted": False,
                "message": "nothing to interrupt",
                "sessions": [],
            }

        per_session = []
        any_interrupted = False
        for session in sessions:
            sid = getattr(session, "session_id", None)
            # Mark BEFORE interrupting so a turn that unblocks immediately as a
            # result of the interrupt still sees the flag.
            with self._state_lock:
                in_flight_event = self._in_flight.get(sid)
                if in_flight_event is not None:
                    self._interrupted_sessions.add(sid)

            try:
                result = self.transport.interrupt(session)
            except Exception as e:
                result = {"ok": False, "error": f"interrupt raised: {e}"}

            ok = bool(result.get("ok"))
            if not ok and in_flight_event is not None:
                # The interrupt did not actually land; don't poison the turn.
                with self._state_lock:
                    self._interrupted_sessions.discard(sid)
            if ok:
                any_interrupted = True

            per_session.append(
                {
                    "session_id": sid,
                    "ok": ok,
                    "error": result.get("error"),
                    "in_flight_event_id": in_flight_event,
                }
            )

        return {
            "ok": any_interrupted,
            "interrupted": any_interrupted,
            "sessions": per_session,
        }

    def interrupt_worker(self) -> bool:
        """
        Interrupt the current worker operation.

        Per SPEC section 28: Worker-turn cancel SHOULD call transport interrupt.

        Returns:
            True if interrupt succeeded, False otherwise.
        """
        if not self.worker_session:
            logger.warning("No active worker session to interrupt")
            return False

        result = self.transport.interrupt(self.worker_session)
        if result.get("ok"):
            logger.info("Worker interrupted")
            return True
        else:
            logger.error(f"Worker interrupt failed: {result.get('error')}")
            return False
