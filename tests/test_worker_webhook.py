"""Worker webhook dispatcher and timeout-guard unit tests.

Covers the resilient webhook contract (3 attempts, exponential backoff with
jitter, HMAC signatures) and the SIGALRM timeout guard plumbing from
Challenge #3. qiskit-aer is required transitively (``worker.worker`` imports
the simulator factory), so the module skips when it is absent.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

pytest.importorskip("qiskit_aer", reason="qiskit-aer not installed")

import httpx

from worker import worker as quac_worker


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch):
    """Shrink retry delays so retry-loop tests run in milliseconds."""
    monkeypatch.setattr(quac_worker, "WEBHOOK_BASE_DELAY_SECONDS", 0.001)
    monkeypatch.setattr(quac_worker, "WEBHOOK_MAX_DELAY_SECONDS", 0.005)
    yield


@pytest.fixture(autouse=True)
def _webhook_secret(monkeypatch: pytest.MonkeyPatch) -> str:
    secret = "quac-test-secret"
    monkeypatch.setattr(quac_worker, "WEBHOOK_SECRET", secret)
    return secret


def _client_factory(handler, capture: dict):
    """Patch httpx.AsyncClient with a MockTransport-backed factory."""
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("timeout", None)
        capture.setdefault("count", 0)

        def route(request: httpx.Request) -> httpx.Response:
            capture["count"] += 1
            capture["request"] = request
            return handler(request, capture["count"])

        return real_client(transport=httpx.MockTransport(route), timeout=5.0)

    return factory


class TestWebhookDelivery:
    def test_success_first_attempt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture: dict = {}
        monkeypatch.setattr(
            httpx, "AsyncClient", _client_factory(lambda req, n: httpx.Response(200), capture)
        )
        delivered = asyncio.run(
            quac_worker._post_webhook_async(
                "https://consumer.example.com/hook", {"job_id": "j-1", "status": "completed"}
            )
        )
        assert delivered is True
        assert capture["count"] == 1

    def test_retries_on_5xx_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture: dict = {}
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _client_factory(
                lambda req, n: httpx.Response(500 if n < 3 else 202), capture
            ),
        )
        delivered = asyncio.run(
            quac_worker._post_webhook_async("https://consumer.example.com/hook", {"job_id": "j-2"})
        )
        assert delivered is True
        assert capture["count"] == 3  # exactly the 3-attempt ceiling

    def test_exhausts_attempts_on_persistent_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture: dict = {}
        monkeypatch.setattr(
            httpx, "AsyncClient", _client_factory(lambda req, n: httpx.Response(503), capture)
        )
        delivered = asyncio.run(
            quac_worker._post_webhook_async("https://consumer.example.com/hook", {"job_id": "j-3"})
        )
        assert delivered is False
        assert capture["count"] == 3

    def test_4xx_fails_fast_without_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture: dict = {}
        monkeypatch.setattr(
            httpx, "AsyncClient", _client_factory(lambda req, n: httpx.Response(400), capture)
        )
        delivered = asyncio.run(
            quac_worker._post_webhook_async("https://consumer.example.com/hook", {"job_id": "j-4"})
        )
        assert delivered is False
        assert capture["count"] == 1  # client errors are not retried

    def test_network_error_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        capture: dict = {}

        def failing_handler(request: httpx.Request, attempt: int) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch.setattr(httpx, "AsyncClient", _client_factory(failing_handler, capture))
        delivered = asyncio.run(
            quac_worker._post_webhook_async("https://consumer.example.com/hook", {"job_id": "j-5"})
        )
        assert delivered is False
        assert capture["count"] == 3

    def test_payload_is_json_and_signed(
        self, monkeypatch: pytest.MonkeyPatch, _webhook_secret: str
    ) -> None:
        capture: dict = {}
        monkeypatch.setattr(
            httpx, "AsyncClient", _client_factory(lambda req, n: httpx.Response(200), capture)
        )
        payload = {"job_id": "j-6", "status": "completed"}
        asyncio.run(quac_worker._post_webhook_async("https://consumer.example.com/hook", payload))

        request = capture["request"]
        body = bytes(request.content)
        assert json.loads(body) == payload
        expected = "sha256=" + hmac.new(
            _webhook_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        assert request.headers["X-Quac-Signature"] == expected
        assert request.headers["X-Quac-Job-Id"] == "j-6"
        assert request.headers["Content-Type"] == "application/json"


class TestBackoffPolicy:
    def test_backoff_grows_exponentially(self) -> None:
        delays = [quac_worker._backoff_delay(i) for i in range(6)]
        # Each ceiling doubles; jitter keeps every sample above 0.
        assert all(delay > 0 for delay in delays)
        assert delays[2] > delays[0]
        assert delays[5] <= quac_worker.WEBHOOK_MAX_DELAY_SECONDS * 1.5

    def test_backoff_is_bounded_by_ceiling(self) -> None:
        for attempt in range(10):
            ceiling = min(
                quac_worker.WEBHOOK_BASE_DELAY_SECONDS * (2**attempt),
                quac_worker.WEBHOOK_MAX_DELAY_SECONDS,
            )
            max_allowed = ceiling + quac_worker.WEBHOOK_JITTER_RATIO * ceiling
            assert quac_worker._backoff_delay(attempt) <= max_allowed + 1e-9

    def test_backoff_has_jitter(self) -> None:
        samples = {round(quac_worker._backoff_delay(3), 9) for _ in range(20)}
        assert len(samples) > 1  # randomised, not deterministic


class TestTimeoutGuard:
    def test_guard_constant_precedes_lambda_hard_timeout(self) -> None:
        assert quac_worker.TIMEOUT_GUARD_SECONDS == 570  # 9:30
        assert quac_worker.TIMEOUT_GUARD_SECONDS < 600  # Lambda's 10:00 maximum
        assert 600 - quac_worker.TIMEOUT_GUARD_SECONDS == quac_worker.LAMBDA_SAFETY_MARGIN_SECONDS

    def test_budget_uses_lambda_context(self) -> None:
        class Context:
            @staticmethod
            def get_remaining_time_in_millis() -> int:
                return 600_000  # fresh 10-minute invocation

        budget = quac_worker._remaining_budget_seconds(Context())
        assert budget == pytest.approx(570.0)

    def test_budget_floor_and_guard_cap(self) -> None:
        class NearlyDone:
            @staticmethod
            def get_remaining_time_in_millis() -> int:
                return 10_000  # 10 s left -> below margin -> floored at 1 s

        assert quac_worker._remaining_budget_seconds(NearlyDone()) == 1.0
        assert quac_worker._remaining_budget_seconds(None) == float(
            quac_worker.TIMEOUT_GUARD_SECONDS
        )

    def test_timeout_error_is_timeout_family(self) -> None:
        assert issubclass(quac_worker.SimulationTimeoutError, TimeoutError)

    @pytest.mark.skipif(
        not hasattr(__import__("signal"), "SIGALRM"),
        reason="SIGALRM is POSIX-only (Lambda runs on Linux)",
    )
    def test_arm_and_disarm_cycle(self) -> None:
        quac_worker._arm_timeout_guard(60)
        try:
            import signal

            assert signal.getitimer(signal.ITIMER_REAL)[0] == pytest.approx(60.0)
        finally:
            quac_worker._disarm_timeout_guard()
        import signal

        assert signal.getitimer(signal.ITIMER_REAL)[0] == 0.0

    def test_disarm_is_safe_on_this_platform(self) -> None:
        # Must never raise, whatever platform the suite runs on.
        quac_worker._disarm_timeout_guard()


class TestBatchReporting:
    def test_malformed_envelope_is_acknowledged_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Business failures complete the record — no perpetual SQS retries."""
        updates: list[tuple[str, str]] = []

        def capture_update(job_id, status, **kwargs):
            updates.append((job_id, status))

        monkeypatch.setattr(quac_worker, "_update_job_status", capture_update)
        event = {
            "Records": [
                {"messageId": "m-1", "body": "this is not json"},
                {"messageId": "m-2", "body": json.dumps({"other": "shape"})},
            ]
        }
        result = quac_worker.lambda_handler(event, None)
        assert result == {"batchItemFailures": []}
        assert updates  # failures were written to the job store

    def test_infrastructure_failure_requests_redelivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def exploding_update(job_id, status, **kwargs):
            raise quac_worker.InfrastructureError("dynamodb unreachable")

        monkeypatch.setattr(quac_worker, "_update_job_status", exploding_update)
        event = {
            "Records": [
                {
                    "messageId": "m-9",
                    "body": json.dumps(
                        {
                            "job_id": "job-9",
                            "circuit_data": "OPENQASM 3.0;",
                            "format": "qasm3",
                            "shots": 10,
                        }
                    ),
                }
            ]
        }
        result = quac_worker.lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "m-9"}]}

    def test_empty_event_is_a_noop(self) -> None:
        assert quac_worker.lambda_handler({}, None) == {"batchItemFailures": []}
