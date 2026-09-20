"""Unit tests for provider webhook ingestion (surface/webhook.py)."""

import pytest
from surface.providers.image import MockImageProvider
from surface.providers.video import MockVideoProvider
from surface.store import Store
from surface.webhook import handle_webhook_payload


class TestWebhookSuccess:
    def test_webhook_drives_known_job_to_succeeded(self):
        """A webhook payload with status=succeeded for a known in-flight
        job should apply the same completion policy as a poll would:
        create a version, move artifact to review, mark job succeeded."""
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        provider = MockImageProvider(polls_to_success=1)
        provider_job_id = provider.submit({"prompt": "A sunset"})

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "A sunset"},
            provider_job_id=provider_job_id,
        )
        store.update_job_status(job["id"], "running", provider_job_id=provider_job_id)

        # Drive the mock provider to "succeeded": polls_to_success=1 means
        # the first status() call transitions queued->running, the second
        # reaches succeeded.
        provider.status(provider_job_id)
        provider.status(provider_job_id)

        result = handle_webhook_payload(
            {"provider_job_id": provider_job_id, "status": "succeeded"},
            store,
            {"image": provider},
        )

        assert result["ok"] is True
        assert result["job_id"] == job["id"]
        assert result["status"] == "succeeded"

        job_after = store.get_job(job["id"])
        assert job_after["status"] == "succeeded"
        assert store.get_artifact("img_1")["status"] == "review"
        assert len(store.list_versions("img_1")) == 1


class TestWebhookFailure:
    def test_webhook_drives_known_job_to_failed(self):
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        provider = MockImageProvider(polls_to_success=5)
        provider_job_id = provider.submit({"prompt": "test"})

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
            provider_job_id=provider_job_id,
        )
        store.update_job_status(job["id"], "running", provider_job_id=provider_job_id)

        result = handle_webhook_payload(
            {"provider_job_id": provider_job_id, "status": "failed"},
            store,
            {"image": provider},
        )

        assert result["ok"] is True
        assert result["status"] == "failed"
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "failed"
        assert store.get_artifact("img_1")["status"] == "failed"


class TestWebhookNotFound:
    def test_unknown_provider_job_id_returns_not_found(self):
        store = Store(":memory:")
        provider = MockImageProvider()

        result = handle_webhook_payload(
            {"provider_job_id": "img_doesnotexist_p1", "status": "succeeded"},
            store,
            {"image": provider},
        )

        assert result["ok"] is False
        assert "no in-flight job found" in result["error"]

    def test_missing_provider_job_id_returns_error(self):
        store = Store(":memory:")
        result = handle_webhook_payload({"status": "succeeded"}, store, {})
        assert result["ok"] is False
        assert "provider_job_id" in result["error"]

    def test_missing_status_returns_error(self):
        store = Store(":memory:")
        result = handle_webhook_payload({"provider_job_id": "x"}, store, {})
        assert result["ok"] is False
        assert "status" in result["error"]


class TestWebhookCancellationSafety:
    def test_cancelled_job_rejects_late_success_webhook(self):
        """A cancelled job's late webhook success must be rejected exactly
        like a late poll success would be (SPEC section 28)."""
        store = Store(":memory:")
        store.create_artifact(id="vid_1", stage="clips", title="Video")

        provider = MockVideoProvider(polls_to_success=10)
        provider_job_id = provider.submit({"prompt": "dance", "duration": 5})

        job = store.create_job(
            artifact_id="vid_1",
            provider="video",
            kind="video",
            request={"prompt": "dance", "duration": 5},
            provider_job_id=provider_job_id,
        )
        store.update_job_status(job["id"], "running", provider_job_id=provider_job_id)

        # Request cancellation. Per store.cancel_job's contract, this only
        # sets cancel_requested=True; status transitions to "cancelled" once
        # something (poller or, here, the webhook handler) actually applies
        # it. This mirrors the real race: a cancellation is requested, and a
        # late success webhook for the same job arrives before that
        # cancellation has been fully applied.
        store.cancel_job(job["id"])

        result = handle_webhook_payload(
            {"provider_job_id": provider_job_id, "status": "succeeded"},
            store,
            {"video": provider},
        )

        assert result["ok"] is False
        assert "cancelled" in result["error"]

        # No version should have been created for the cancelled job.
        assert len(store.list_versions("vid_1")) == 0
        job_after = store.get_job(job["id"])
        assert job_after["cancel_requested"] is True
        assert job_after["status"] != "succeeded"

    def test_unregistered_provider_marks_job_failed(self):
        store = Store(":memory:")
        store.create_artifact(id="img_1", stage="keyframes", title="Image")

        job = store.create_job(
            artifact_id="img_1",
            provider="image",
            kind="image",
            request={"prompt": "test"},
            provider_job_id="img_abc123_p1",
        )
        store.update_job_status(job["id"], "running", provider_job_id="img_abc123_p1")

        result = handle_webhook_payload(
            {"provider_job_id": "img_abc123_p1", "status": "succeeded"},
            store,
            {},  # no providers registered
        )

        assert result["ok"] is False
        assert "unknown provider" in result["error"]
        job_after = store.get_job(job["id"])
        assert job_after["status"] == "failed"
