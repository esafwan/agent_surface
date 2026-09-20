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
from typing import Any, Dict, Optional
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
        """
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.turn_timeout = turn_timeout
        self.event_batch_size = event_batch_size
        self.recycle_after_events = recycle_after_events
        self.recycle_after_tokens = recycle_after_tokens


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
    ):
        """
        Initialize Supervisor.

        Args:
            store: Store instance for persistence.
            transport: WorkerTransport implementation (NativeStream or Resume).
            stage_config: StageConfig instance defining stages/actions/dependencies.
            config: SupervisorConfig (or None for defaults).
        """
        self.store = store
        self.transport = transport
        self.stage_config = stage_config
        self.config = config or SupervisorConfig()

        self.worker_session = None
        self.project_id = "default"  # TODO: parameterize
        self.event_count = 0

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
            self.worker_session = self.transport.start(context)
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

        # Get recent events
        cursor = self.store.conn.cursor()
        cursor.execute(
            "SELECT id FROM events WHERE status IN ('pending', 'processing') ORDER BY created_at DESC LIMIT 5"
        )
        recent_event_ids = [row[0] for row in cursor.fetchall()]

        return {
            "project_id": self.project_id,
            "stage_config_id": self.stage_config.id,
            "stage_counts": stage_counts,
            "artifact_count": len(artifacts),
            "pending_event_count": sum(1 for a in artifacts if a["status"] == "generating"),
            "recent_event_ids": recent_event_ids,
        }

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

            # Include upstream artifacts for context
            context["upstream_artifacts"] = [
                self.store.get_artifact(uid)
                for uid in context["upstream_ids"]
            ]

        return context

    def claim_and_dispatch_event(self) -> Optional[Dict[str, Any]]:
        """
        Claim the next eligible event and dispatch it to the worker.

        Per SPEC section 22: Loop steps 1-9 (claim, acquire lease, construct context,
        send event, validate outcome, ack).

        Returns:
            The claimed event dict, or None if no event available or error occurred.
        """
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
            result = self.transport.send_event(self.worker_session, event_context)

            # Step 7: Validate durable outcome
            if not result.get("ok"):
                error_msg = result.get("error", "Unknown error")
                logger.error(f"Worker turn failed for event {event['id']}: {error_msg}")
                self.store.fail_event(event["id"], error_msg)
                return event

            # Worker succeeded; step 8: ack event
            self.store.ack_event(event["id"])
            logger.info(f"Acked event: {event['id']}")

            self.event_count += 1

            # Check if worker recycle is needed
            if (
                self.config.recycle_after_events
                and self.event_count >= self.config.recycle_after_events
            ):
                logger.info(
                    f"Recycling worker after {self.event_count} events"
                )
                self.stop_worker()
                self.event_count = 0

            return event

        except Exception as e:
            logger.error(f"Error processing event {event['id']}: {e}")
            try:
                self.store.fail_event(event["id"], str(e))
            except Exception as ex:
                logger.error(f"Failed to mark event as failed: {ex}")
            return event

    def run_loop(self, max_iterations: Optional[int] = None) -> None:
        """
        Run the main supervision loop.

        Per SPEC section 22: Loop continuously (or for max_iterations).

        Args:
            max_iterations: Max number of claim/dispatch cycles (None = infinite).
        """
        iteration = 0
        try:
            while max_iterations is None or iteration < max_iterations:
                self.claim_and_dispatch_event()
                iteration += 1
        except KeyboardInterrupt:
            logger.info("Supervisor loop interrupted")
        finally:
            self.stop_worker()

    def run_once(self) -> bool:
        """
        Run one iteration of the claim/dispatch loop.

        Returns:
            True if an event was processed, False otherwise.
        """
        event = self.claim_and_dispatch_event()
        return event is not None

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
