"""Notification adapters for Agent Surface Board.

Optional adapters that send notifications when the board needs attention.
Per SPEC section 34: "Optional adapters MAY send OS notification, webhook,
Slack, email, or host-app push. Notifications are not required for v1."

This module provides:
  * NotificationAdapter: abstract base for notification implementations
  * LogNotifier: writes formatted messages to a Python logger
  * WebhookNotifier: POSTs events as JSON to a configured URL
  * detect_notifications: compares project summaries to identify new events
  * dispatch_notifications: sends notifications to all configured adapters
"""

import json
import logging
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional


class NotificationAdapter(ABC):
    """Abstract base class for notification adapters.

    Implementations send a notification via their chosen channel (log, webhook,
    Slack, email, OS notification, etc.) when the board needs attention.
    """

    @abstractmethod
    def notify(self, event: Dict[str, Any]) -> None:
        """Send a notification for an event.

        Args:
            event: A dict describing what happened, e.g.
                   {"kind": "job_failed", "artifact_id": "img_003", "message": "..."}
                   or {"kind": "pending_review", "count": 3, "message": "..."}

        Implementations MUST catch and log (not raise) any errors so that
        a broken adapter never breaks the caller's flow.
        """
        pass


class LogNotifier(NotificationAdapter):
    """Notification adapter that writes to a Python logger.

    Safe default implementation with no external dependencies. Failures/stale
    events are logged at warning level; others at info level.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize the log notifier.

        Args:
            logger: Logger instance to use. Defaults to logging.getLogger("surface.notify").
        """
        self.logger = logger or logging.getLogger("surface.notify")

    def notify(self, event: Dict[str, Any]) -> None:
        """Write event to logger at appropriate level."""
        try:
            kind = event.get("kind", "unknown")
            message = event.get("message", "")

            # Failures and stale events warrant warning level; others are info
            if kind in ("job_failed", "stale", "failed"):
                self.logger.warning(f"[{kind}] {message}")
            else:
                self.logger.info(f"[{kind}] {message}")
        except Exception as e:
            # Log but don't raise, so adapter failure never breaks the caller
            self.logger.error(f"Error in LogNotifier: {e}")


class WebhookNotifier(NotificationAdapter):
    """Notification adapter that POSTs events as JSON to a webhook URL.

    Captures network errors gracefully so webhook failures never break the
    caller's flow. Supports dependency injection for testing via a custom
    post_fn.
    """

    def __init__(
        self,
        url: str,
        timeout: float = 5.0,
        post_fn: Optional[Callable[[str, bytes], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """Initialize the webhook notifier.

        Args:
            url: Webhook URL to POST to.
            timeout: Request timeout in seconds (default: 5s).
            post_fn: Optional injectable function for testing. If provided,
                    called instead of urllib.request (signature: (url, body) -> None).
                    Allows tests to verify what would be sent without network.
            logger: Logger for errors. Defaults to logging.getLogger("surface.notify").
        """
        self.url = url
        self.timeout = timeout
        self.post_fn = post_fn or self._default_post
        self.logger = logger or logging.getLogger("surface.notify")

    def _default_post(self, url: str, body: bytes) -> None:
        """Default POST implementation using urllib."""
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            # Read response to ensure request completes
            response.read()

    def notify(self, event: Dict[str, Any]) -> None:
        """POST event as JSON to webhook URL."""
        try:
            body = json.dumps(event).encode("utf-8")
            self.post_fn(self.url, body)
        except Exception as e:
            # Log but don't raise, so webhook failure never breaks the caller
            self.logger.error(f"Error posting to webhook {self.url}: {e}")


def detect_notifications(
    store: "Store",  # noqa: F821
    previous_summary: Optional[Dict[str, Any]],
    current_summary: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Compare project summaries and detect notification-worthy changes.

    A "previous_summary" and "current_summary" are the outputs of
    cli.project_summary() — dicts with shape:
        {
            "total_artifacts": int,
            "by_stage": {stage: count, ...},
            "by_status": {status: count, ...},
        }

    Returns a list of notification events describing what changed. An event is
    a dict like:
        {"kind": "job_failed", "artifact_id": "...", "message": "..."}
        {"kind": "stale", "artifact_ids": [...], "message": "..."}
        {"kind": "pending_review", "count": N, "message": "..."}
        {"kind": "failed", "count": N, "message": "..."}

    By comparing only counts, this function is pure computation over two dicts
    and requires no new store methods or event types — it queries the store
    only to fetch full lists when needed for detailed messages.

    Args:
        store: Store instance for querying artifact details if needed.
        previous_summary: Prior project summary (None on first call).
        current_summary: Current project summary.

    Returns:
        List of notification-worthy event dicts; empty if no changes.
    """
    notifications: List[Dict[str, Any]] = []

    # If previous_summary is None, this is the first call; don't notify on
    # baseline state (avoid spam when supervisor starts).
    if previous_summary is None:
        return notifications

    current_by_status = current_summary.get("by_status", {})
    previous_by_status = previous_summary.get("by_status", {})

    # Detect new failures: "failed" count increased
    current_failed = current_by_status.get("failed", 0)
    previous_failed = previous_by_status.get("failed", 0)
    if current_failed > previous_failed:
        new_failures = current_failed - previous_failed
        notifications.append(
            {
                "kind": "failed",
                "count": new_failures,
                "message": f"{new_failures} artifact(s) now in failed status",
            }
        )

    # Detect new stale artifacts: "stale" count increased
    current_stale = current_by_status.get("stale", 0)
    previous_stale = previous_by_status.get("stale", 0)
    if current_stale > previous_stale:
        new_stales = current_stale - previous_stale
        notifications.append(
            {
                "kind": "stale",
                "count": new_stales,
                "message": f"{new_stales} artifact(s) now stale (upstream changed)",
            }
        )

    # Detect "waiting on you": "review" status went from 0 to > 0
    current_review = current_by_status.get("review", 0)
    previous_review = previous_by_status.get("review", 0)
    if previous_review == 0 and current_review > 0:
        notifications.append(
            {
                "kind": "pending_review",
                "count": current_review,
                "message": f"{current_review} artifact(s) waiting for review/approval",
            }
        )
    elif current_review > previous_review:
        # Also notify if review count increased significantly
        new_review = current_review - previous_review
        notifications.append(
            {
                "kind": "pending_review",
                "count": new_review,
                "message": f"{new_review} more artifact(s) ready for review",
            }
        )

    return notifications


def dispatch_notifications(
    notifications: List[Dict[str, Any]],
    adapters: List[NotificationAdapter],
) -> None:
    """Send each notification to each adapter.

    If an adapter raises an error, it is caught and logged, so one broken
    adapter never prevents others from receiving their notifications.

    Args:
        notifications: List of notification dicts to send.
        adapters: List of NotificationAdapter instances to send to.
    """
    logger = logging.getLogger("surface.notify")

    for adapter in adapters:
        for notification in notifications:
            try:
                adapter.notify(notification)
            except Exception as e:
                logger.error(f"Error in adapter {adapter.__class__.__name__}: {e}")
