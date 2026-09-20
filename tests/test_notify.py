"""Unit tests for surface.notify notification adapters and functions."""

import json
import logging
import pytest

from surface.notify import (
    LogNotifier,
    WebhookNotifier,
    detect_notifications,
    dispatch_notifications,
)
from surface.store import Store


class TestLogNotifier:
    """Tests for LogNotifier."""

    def test_log_notifier_failure_event_at_warning_level(self, caplog):
        """Verify job_failed events are logged at WARNING level."""
        notifier = LogNotifier()
        event = {
            "kind": "job_failed",
            "artifact_id": "img_003",
            "message": "Provider returned failed",
        }

        with caplog.at_level(logging.WARNING):
            notifier.notify(event)

        assert any(
            record.levelname == "WARNING" and "job_failed" in record.message
            for record in caplog.records
        )
        assert any("Provider returned failed" in record.message for record in caplog.records)

    def test_log_notifier_stale_event_at_warning_level(self, caplog):
        """Verify stale events are logged at WARNING level."""
        notifier = LogNotifier()
        event = {
            "kind": "stale",
            "count": 2,
            "message": "2 artifact(s) now stale (upstream changed)",
        }

        with caplog.at_level(logging.WARNING):
            notifier.notify(event)

        assert any(
            record.levelname == "WARNING" and "stale" in record.message
            for record in caplog.records
        )

    def test_log_notifier_info_event_at_info_level(self, caplog):
        """Verify pending_review events are logged at INFO level."""
        notifier = LogNotifier()
        event = {
            "kind": "pending_review",
            "count": 3,
            "message": "3 artifact(s) waiting for review/approval",
        }

        with caplog.at_level(logging.INFO):
            notifier.notify(event)

        assert any(
            record.levelname == "INFO" and "pending_review" in record.message
            for record in caplog.records
        )

    def test_log_notifier_with_custom_logger(self, caplog):
        """Verify LogNotifier accepts custom logger."""
        custom_logger = logging.getLogger("custom.notify")
        notifier = LogNotifier(logger=custom_logger)
        event = {"kind": "pending_review", "count": 1, "message": "Test"}

        with caplog.at_level(logging.INFO):
            notifier.notify(event)

        assert any(
            record.name == "custom.notify" and "Test" in record.message
            for record in caplog.records
        )

    def test_log_notifier_handles_errors(self, caplog):
        """Verify LogNotifier catches and logs internal errors gracefully."""
        notifier = LogNotifier()

        # Simulate a dict that raises an error when accessed
        class BadEvent(dict):
            def get(self, key, default=None):
                raise RuntimeError("Simulated event error")

        event = BadEvent()

        # Should not raise; error should be logged
        with caplog.at_level(logging.ERROR):
            notifier.notify(event)  # Should catch and log, not raise

        # Verify error was logged
        assert any(
            "Error in LogNotifier" in record.message for record in caplog.records
        )


class TestWebhookNotifier:
    """Tests for WebhookNotifier."""

    def test_webhook_notifier_calls_post_fn(self):
        """Verify WebhookNotifier calls the injected post function."""
        calls = []

        def fake_post(url: str, body: bytes) -> None:
            calls.append({"url": url, "body": json.loads(body)})

        notifier = WebhookNotifier(
            url="https://example.com/notify",
            post_fn=fake_post,
        )
        event = {
            "kind": "job_failed",
            "artifact_id": "img_003",
            "message": "Provider failed",
        }

        notifier.notify(event)

        assert len(calls) == 1
        assert calls[0]["url"] == "https://example.com/notify"
        assert calls[0]["body"] == event

    def test_webhook_notifier_posts_json(self):
        """Verify event is JSON-encoded before posting."""
        calls = []

        def fake_post(url: str, body: bytes) -> None:
            calls.append(body)

        notifier = WebhookNotifier(url="http://localhost:8080", post_fn=fake_post)
        event = {
            "kind": "pending_review",
            "count": 5,
            "message": "Waiting on you",
        }

        notifier.notify(event)

        assert len(calls) == 1
        decoded = json.loads(calls[0])
        assert decoded == event

    def test_webhook_notifier_handles_post_fn_error(self, caplog):
        """Verify WebhookNotifier catches and logs post_fn errors."""

        def failing_post(url: str, body: bytes) -> None:
            raise RuntimeError("Network error")

        notifier = WebhookNotifier(
            url="https://example.com/notify",
            post_fn=failing_post,
        )
        event = {"kind": "test", "message": "Should not raise"}

        with caplog.at_level(logging.ERROR):
            notifier.notify(event)  # Should not raise

        assert any(
            "Error posting to webhook" in record.message for record in caplog.records
        )

    def test_webhook_notifier_multiple_events(self):
        """Verify WebhookNotifier can post multiple events."""
        calls = []

        def fake_post(url: str, body: bytes) -> None:
            calls.append(json.loads(body))

        notifier = WebhookNotifier(url="http://localhost:8080", post_fn=fake_post)

        events = [
            {"kind": "failed", "count": 1, "message": "One failure"},
            {"kind": "stale", "count": 2, "message": "Two stale"},
        ]

        for event in events:
            notifier.notify(event)

        assert len(calls) == 2
        assert calls[0]["kind"] == "failed"
        assert calls[1]["kind"] == "stale"

    def test_webhook_notifier_with_timeout(self):
        """Verify timeout parameter is stored."""
        notifier = WebhookNotifier(url="http://example.com", timeout=10.0)
        assert notifier.timeout == 10.0

    def test_webhook_notifier_custom_logger(self, caplog):
        """Verify WebhookNotifier accepts custom logger."""
        custom_logger = logging.getLogger("custom.webhook")

        def failing_post(url: str, body: bytes) -> None:
            raise RuntimeError("fail")

        notifier = WebhookNotifier(
            url="http://example.com",
            post_fn=failing_post,
            logger=custom_logger,
        )

        with caplog.at_level(logging.ERROR):
            notifier.notify({"kind": "test", "message": "ok"})

        assert any(
            record.name == "custom.webhook" for record in caplog.records
        )


class TestDetectNotifications:
    """Tests for detect_notifications()."""

    def test_detect_no_notifications_on_first_call(self):
        """Verify first call (previous_summary=None) returns empty list."""
        store = Store(":memory:")
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 3, "review": 2},
        }

        result = detect_notifications(store, previous_summary=None, current_summary=current)
        assert result == []

    def test_detect_new_failures(self):
        """Verify detection of new failed artifacts."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 2, "review": 2, "failed": 1},
        }
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 1, "review": 2, "failed": 2},
        }

        result = detect_notifications(store, previous_summary=previous, current_summary=current)

        assert len(result) == 1
        assert result[0]["kind"] == "failed"
        assert result[0]["count"] == 1
        assert "failed status" in result[0]["message"]

    def test_detect_new_stale_artifacts(self):
        """Verify detection of stale artifacts."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 3, "review": 2, "stale": 0},
        }
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 2, "review": 2, "stale": 1},
        }

        result = detect_notifications(store, previous_summary=previous, current_summary=current)

        assert len(result) == 1
        assert result[0]["kind"] == "stale"
        assert result[0]["count"] == 1
        assert "stale" in result[0]["message"]

    def test_detect_pending_review_0_to_positive(self):
        """Verify detection of review going from 0 to positive."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 5, "review": 0},
        }
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 2, "review": 3},
        }

        result = detect_notifications(store, previous_summary=previous, current_summary=current)

        assert len(result) == 1
        assert result[0]["kind"] == "pending_review"
        assert result[0]["count"] == 3
        assert "waiting for review" in result[0]["message"]

    def test_detect_pending_review_increase(self):
        """Verify detection of review count increase from positive to higher."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 1, "review": 3, "approved": 1},
        }
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 0, "review": 4, "approved": 1},
        }

        result = detect_notifications(store, previous_summary=previous, current_summary=current)

        assert len(result) == 1
        assert result[0]["kind"] == "pending_review"
        assert result[0]["count"] == 1  # Increased by 1
        assert "more artifact(s) ready" in result[0]["message"]

    def test_detect_multiple_changes(self):
        """Verify multiple simultaneous changes are all detected."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 2, "review": 2, "approved": 1, "failed": 0, "stale": 0},
        }
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 0, "review": 1, "approved": 1, "failed": 1, "stale": 2},
        }

        result = detect_notifications(store, previous_summary=previous, current_summary=current)

        kinds = {n["kind"] for n in result}
        assert "failed" in kinds
        assert "stale" in kinds
        # review went down, so no pending_review notification
        assert len(result) == 2

    def test_detect_no_change_produces_empty_list(self):
        """Verify identical summaries produce no notifications."""
        store = Store(":memory:")
        summary = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 2, "review": 2, "approved": 1},
        }

        result = detect_notifications(store, previous_summary=summary, current_summary=summary)

        assert result == []

    def test_detect_review_count_decrease(self):
        """Verify review count decrease doesn't trigger notification."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 1, "review": 3, "approved": 1},
        }
        current = {
            "total_artifacts": 5,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 1, "review": 1, "approved": 3},
        }

        result = detect_notifications(store, previous_summary=previous, current_summary=current)

        # Review count decreased (approved increased), so no notification
        assert result == []

    def test_detect_handles_missing_status_keys(self):
        """Verify missing status keys in summaries are handled gracefully."""
        store = Store(":memory:")
        previous = {
            "total_artifacts": 3,
            "by_stage": {"script": 1},
            # "by_status" omitted
        }
        current = {
            "total_artifacts": 3,
            "by_stage": {"script": 1},
            "by_status": {"review": 1, "failed": 1},
        }

        # Should not raise; .get() defaults to 0
        result = detect_notifications(store, previous_summary=previous, current_summary=current)
        assert len(result) >= 1  # At least one notification


class TestDispatchNotifications:
    """Tests for dispatch_notifications()."""

    def test_dispatch_to_multiple_adapters(self):
        """Verify all adapters receive all notifications."""
        received = []

        class FakeAdapter:
            def __init__(self, name):
                self.name = name

            def notify(self, event):
                received.append((self.name, event))

        adapter1 = FakeAdapter("a1")
        adapter2 = FakeAdapter("a2")
        notifications = [
            {"kind": "failed", "count": 1},
            {"kind": "stale", "count": 2},
        ]

        dispatch_notifications(notifications, [adapter1, adapter2])

        assert len(received) == 4  # 2 adapters * 2 notifications
        assert received[0] == ("a1", {"kind": "failed", "count": 1})
        assert received[1] == ("a1", {"kind": "stale", "count": 2})
        assert received[2] == ("a2", {"kind": "failed", "count": 1})
        assert received[3] == ("a2", {"kind": "stale", "count": 2})

    def test_dispatch_empty_notifications(self):
        """Verify dispatch with empty notifications list doesn't call adapters."""
        calls = []

        class FakeAdapter:
            def notify(self, event):
                calls.append(event)

        dispatch_notifications([], [FakeAdapter()])

        assert len(calls) == 0

    def test_dispatch_empty_adapters(self):
        """Verify dispatch with no adapters is safe (no-op)."""
        notifications = [{"kind": "test", "count": 1}]
        # Should not raise
        dispatch_notifications(notifications, [])

    def test_dispatch_adapter_error_doesnt_stop_others(self, caplog):
        """Verify one adapter failing doesn't prevent others from receiving."""
        calls = []

        class FailingAdapter:
            def notify(self, event):
                raise RuntimeError("Adapter failed")

        class WorkingAdapter:
            def notify(self, event):
                calls.append(event)

        adapters = [FailingAdapter(), WorkingAdapter(), WorkingAdapter()]
        notifications = [{"kind": "test", "count": 1}]

        with caplog.at_level(logging.ERROR):
            dispatch_notifications(notifications, adapters)

        # Both WorkingAdapters should have received the notification
        assert len(calls) == 2
        # Error should be logged
        assert any("Error in adapter" in record.message for record in caplog.records)

    def test_dispatch_all_adapters_fail_logs_all_errors(self, caplog):
        """Verify all adapter errors are logged even if all fail."""

        class FailingAdapter:
            def notify(self, event):
                raise RuntimeError("Failed")

        adapters = [FailingAdapter(), FailingAdapter()]
        notifications = [{"kind": "test"}]

        with caplog.at_level(logging.ERROR):
            dispatch_notifications(notifications, adapters)

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_records) >= 2


class TestIntegration:
    """Integration tests combining detect + dispatch."""

    def test_full_notification_flow(self):
        """Verify full flow: detect notifications, dispatch to adapters."""
        store = Store(":memory:")
        logged = []
        sent_webhooks = []

        def fake_webhook(url, body):
            sent_webhooks.append(json.loads(body))

        # Create adapters
        log_adapter = LogNotifier(logger=logging.getLogger("test.notify"))
        webhook_adapter = WebhookNotifier(
            url="https://example.com/notify",
            post_fn=fake_webhook,
        )

        # Simulate state change
        previous = {
            "total_artifacts": 3,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 2, "review": 0, "approved": 1},
        }
        current = {
            "total_artifacts": 3,
            "by_stage": {"script": 1, "shots": 2},
            "by_status": {"draft": 1, "review": 2, "approved": 0},
        }

        # Detect
        notifications = detect_notifications(store, previous, current)

        # Should detect pending_review going from 0 to 2
        assert len(notifications) > 0
        assert any(n["kind"] == "pending_review" for n in notifications)

        # Dispatch (would log and webhook in real usage)
        dispatch_notifications(notifications, [log_adapter, webhook_adapter])

        # Webhook should have been called
        assert len(sent_webhooks) > 0
        for webhook_event in sent_webhooks:
            assert "kind" in webhook_event
            assert "message" in webhook_event
