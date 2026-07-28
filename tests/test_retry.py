"""Tests for retry and buffering functionality."""

from unittest.mock import MagicMock, patch

import pytest

from plexus.client import AuthenticationError, Plexus, PlexusError, _ConnError, _Timeout
from plexus.config import RetryConfig


class TestRetryConfig:
    """Tests for RetryConfig class."""

    def test_default_config(self):
        """Default config should have sensible values."""
        config = RetryConfig()
        assert config.max_retries == 3
        assert config.base_delay == 1.0
        assert config.max_delay == 30.0
        assert config.exponential_base == 2.0
        assert config.jitter is True

    def test_get_delay_exponential(self):
        """Delays should increase exponentially."""
        config = RetryConfig(base_delay=1.0, exponential_base=2.0, jitter=False)

        assert config.get_delay(0) == 1.0  # 1 * 2^0
        assert config.get_delay(1) == 2.0  # 1 * 2^1
        assert config.get_delay(2) == 4.0  # 1 * 2^2
        assert config.get_delay(3) == 8.0  # 1 * 2^3

    def test_get_delay_max_cap(self):
        """Delay should be capped at max_delay."""
        config = RetryConfig(base_delay=10.0, max_delay=15.0, jitter=False)

        assert config.get_delay(0) == 10.0
        assert config.get_delay(1) == 15.0  # Capped at 15, not 20
        assert config.get_delay(2) == 15.0  # Still capped

    def test_get_delay_with_jitter(self):
        """Jitter should add randomness to delays."""
        config = RetryConfig(base_delay=1.0, jitter=True)

        # With jitter, delay should be between 0.5x and 1x of base
        delays = [config.get_delay(0) for _ in range(100)]
        assert min(delays) >= 0.5
        assert max(delays) <= 1.0
        # Should have some variation
        assert len(set(delays)) > 1


class TestRetryBehavior:
    """Tests for retry behavior on network errors."""

    @pytest.fixture
    def client(self):
        """Create a client with short retry delays for testing."""
        return Plexus(
            api_key="test_key",
            endpoint="http://localhost:9999",
            retry_config=RetryConfig(max_retries=2, base_delay=0.01, jitter=False),
            persistent_buffer=False,
        )

    def test_retry_on_timeout(self, client):
        """Should retry on request timeout."""
        with patch.object(client, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.status_code = 200

            # Timeout twice, then succeed
            mock_session.return_value.post.side_effect = [
                _Timeout(),
                _Timeout(),
                mock_response,
            ]

            result = client.send("temp", 72.5)
            assert result is True
            assert mock_session.return_value.post.call_count == 3

    def test_retry_on_connection_error(self, client):
        """Should retry on connection error."""
        with patch.object(client, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.status_code = 200

            # Connection error twice, then succeed
            mock_session.return_value.post.side_effect = [
                _ConnError("Connection refused"),
                _ConnError("Connection refused"),
                mock_response,
            ]

            result = client.send("temp", 72.5)
            assert result is True
            assert mock_session.return_value.post.call_count == 3

    def test_retry_on_500_error(self, client):
        """Should retry on server error (5xx)."""
        with patch.object(client, "_get_session") as mock_session:
            error_response = MagicMock()
            error_response.status_code = 500
            error_response.text = "Internal Server Error"

            success_response = MagicMock()
            success_response.status_code = 200

            mock_session.return_value.post.side_effect = [
                error_response,
                error_response,
                success_response,
            ]

            result = client.send("temp", 72.5)
            assert result is True
            assert mock_session.return_value.post.call_count == 3

    def test_retry_on_429_rate_limit(self, client):
        """Should retry on rate limit (429)."""
        with patch.object(client, "_get_session") as mock_session:
            rate_limit_response = MagicMock()
            rate_limit_response.status_code = 429

            success_response = MagicMock()
            success_response.status_code = 200

            mock_session.return_value.post.side_effect = [
                rate_limit_response,
                success_response,
            ]

            result = client.send("temp", 72.5)
            assert result is True
            assert mock_session.return_value.post.call_count == 2


class TestNoRetryBehavior:
    """Tests for errors that should not be retried."""

    @pytest.fixture
    def client(self):
        """Create a client for testing."""
        return Plexus(
            api_key="test_key",
            endpoint="http://localhost:9999",
            retry_config=RetryConfig(max_retries=3, base_delay=0.01, jitter=False),
        )

    def test_no_retry_on_401(self, client):
        """Should not retry on authentication error (401)."""
        with patch.object(client, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.status_code = 401

            mock_session.return_value.post.return_value = mock_response

            with pytest.raises(AuthenticationError, match="Invalid API key"):
                client.send("temp", 72.5)

            # Should only try once - no retries
            assert mock_session.return_value.post.call_count == 1

    def test_no_retry_on_403(self, client):
        """Should not retry on forbidden error (403)."""
        with patch.object(client, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.status_code = 403

            mock_session.return_value.post.return_value = mock_response

            with pytest.raises(AuthenticationError, match="write permissions"):
                client.send("temp", 72.5)

            assert mock_session.return_value.post.call_count == 1

    def test_no_retry_on_400(self, client):
        """Should not retry on bad request (400)."""
        with patch.object(client, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.text = "Bad request"

            mock_session.return_value.post.return_value = mock_response

            with pytest.raises(PlexusError, match="Bad request"):
                client.send("temp", 72.5)

            assert mock_session.return_value.post.call_count == 1

    def test_no_retry_on_422(self, client):
        """Should not retry on validation error (422)."""
        with patch.object(client, "_get_session") as mock_session:
            mock_response = MagicMock()
            mock_response.status_code = 422
            mock_response.text = "Validation failed"

            mock_session.return_value.post.return_value = mock_response

            with pytest.raises(PlexusError, match="Bad request"):
                client.send("temp", 72.5)

            assert mock_session.return_value.post.call_count == 1


class TestBuffering:
    """Tests for local buffering on failed sends."""

    @pytest.fixture
    def client(self):
        """Create a client with short retry delays for testing."""
        return Plexus(
            api_key="test_key",
            endpoint="http://localhost:9999",
            retry_config=RetryConfig(max_retries=1, base_delay=0.01, jitter=False),
            max_buffer_size=100,
            persistent_buffer=False,
        )

    def test_buffer_on_all_retries_failed(self, client):
        """Should buffer points when all retries fail."""
        with patch.object(client, "_get_session") as mock_session:
            mock_session.return_value.post.side_effect = _Timeout()

            with pytest.raises(PlexusError):
                client.send("temp", 72.5)

            # Point should be buffered
            assert client.buffer_size() == 1

    def test_buffered_points_included_in_next_send(self, client):
        """Buffered points should be included in the next send attempt."""
        with patch.object(client, "_get_session") as mock_session:
            # First send fails
            mock_session.return_value.post.side_effect = _Timeout()

            with pytest.raises(PlexusError):
                client.send("temp", 72.5)

            assert client.buffer_size() == 1

            # Reset mock for second send
            success_response = MagicMock()
            success_response.status_code = 200
            mock_session.return_value.post.side_effect = None
            mock_session.return_value.post.return_value = success_response

            # Second send succeeds
            result = client.send("humidity", 45.0)
            assert result is True

            # Buffer should be cleared
            assert client.buffer_size() == 0

            # Check that both points were sent
            call_args = mock_session.return_value.post.call_args
            # Client now sends raw bytes via data= (possibly gzipped)
            import json as _json
            body = call_args.kwargs.get("data") or call_args.kwargs.get("json")
            if isinstance(body, (bytes, bytearray)):
                try:
                    import gzip
                    body = gzip.decompress(body)
                except Exception:
                    pass
                sent = _json.loads(body)
            else:
                sent = body
            sent_points = sent["points"]
            assert len(sent_points) == 2
            assert sent_points[0]["metric"] == "temp"
            assert sent_points[1]["metric"] == "humidity"

    def test_buffer_max_size_limit(self, client):
        """Buffer should respect max size limit."""
        client.max_buffer_size = 5

        with patch.object(client, "_get_session") as mock_session:
            mock_session.return_value.post.side_effect = _Timeout()

            # Send 10 points, each will fail and buffer
            for i in range(10):
                with pytest.raises(PlexusError):
                    client.send(f"metric_{i}", float(i))

            # Buffer should be capped at 5 (oldest dropped)
            assert client.buffer_size() == 5

    def test_flush_buffer_when_empty(self, client):
        """flush_buffer should return True when buffer is empty."""
        assert client.buffer_size() == 0
        assert client.flush_buffer() is True

    def test_flush_buffer_success(self, client):
        """flush_buffer should send buffered points."""
        with patch.object(client, "_get_session") as mock_session:
            # First send fails, buffering the point
            mock_session.return_value.post.side_effect = _Timeout()

            with pytest.raises(PlexusError):
                client.send("temp", 72.5)

            assert client.buffer_size() == 1

            # Now flush succeeds
            success_response = MagicMock()
            success_response.status_code = 200
            mock_session.return_value.post.side_effect = None
            mock_session.return_value.post.return_value = success_response

            result = client.flush_buffer()
            assert result is True
            assert client.buffer_size() == 0


class TestChunkedSends:
    """Regression tests for the >10k-point wedge: sends drain in ≤5000-point
    chunks and a non-retryable 4xx buffers the points instead of losing them."""

    @pytest.fixture
    def client(self):
        return Plexus(
            api_key="test_key",
            endpoint="http://localhost:9999",
            ws_url="ws://127.0.0.1:9",  # unroutable — never touch prod
            timeout=0.2,
            retry_config=RetryConfig(max_retries=0, base_delay=0.01, jitter=False),
            max_buffer_size=20_000,
            persistent_buffer=False,
        )

    @staticmethod
    def _posted_point_counts(mock_session):
        import gzip as _gzip
        import json as _json

        counts = []
        for call in mock_session.return_value.post.call_args_list:
            body = call.kwargs.get("data")
            try:
                body = _gzip.decompress(body)
            except Exception:
                pass
            counts.append(len(_json.loads(body)["points"]))
        return counts

    def test_backlog_over_10k_drains_in_chunks(self, client):
        """A 10k backlog + new point must drain as ≤5000-point POSTs, never
        one merged request over the gateway's 10k per-batch cap."""
        client._buffer.add(
            [{"metric": "m", "value": float(i), "timestamp": i} for i in range(10_000)]
        )
        with patch.object(client, "_get_session") as mock_session:
            ok = MagicMock()
            ok.status_code = 200
            mock_session.return_value.post.return_value = ok

            assert client.send("temp", 72.5) is True

            counts = self._posted_point_counts(mock_session)
            assert sum(counts) == 10_001  # nothing lost
            assert max(counts) <= 5000  # every request under the gateway cap
            assert client.buffer_size() == 0

    def test_400_buffers_points_instead_of_losing_them(self, client):
        """A non-retryable 400 must buffer the unsent points before raising —
        previously they were dropped pre-buffer, wedging the backlog forever."""
        with patch.object(client, "_get_session") as mock_session:
            bad = MagicMock()
            bad.status_code = 400
            bad.text = "too many points"
            mock_session.return_value.post.return_value = bad

            with pytest.raises(PlexusError, match="Bad request"):
                client.send("temp", 72.5)

            assert client.buffer_size() == 1

            # And the client recovers on the next successful send.
            ok = MagicMock()
            ok.status_code = 200
            mock_session.return_value.post.return_value = ok
            assert client.send("humidity", 45.0) is True
            assert client.buffer_size() == 0


class TestWsFrameSplit:
    """The WS path must keep serialized frames under the gateway's 1MB read
    limit before treating a local socket write as delivery."""

    def test_split_respects_byte_budget_and_order(self):
        import json as _json

        from plexus.client import _WS_ENVELOPE_BYTES, _split_ws_frames

        points = [
            {"metric": "m", "value": "x" * 1000, "timestamp": i} for i in range(100)
        ]
        budget = 10_000
        chunks = _split_ws_frames(points, budget)

        assert len(chunks) > 1
        # Order preserved, nothing lost
        assert [p for c in chunks for p in c] == points
        # Every chunk fits the budget (singletons exempt by design)
        for chunk in chunks:
            if len(chunk) > 1:
                frame = _json.dumps({"type": "telemetry", "points": chunk})
                assert len(frame.encode("utf-8")) <= budget + _WS_ENVELOPE_BYTES

    def test_small_batch_single_frame(self):
        from plexus.client import _split_ws_frames

        points = [{"metric": "m", "value": 1.0, "timestamp": i} for i in range(50)]
        assert _split_ws_frames(points, 900_000) == [points]


class TestThreadSafety:
    """Tests for thread-safe buffer access."""

    def test_concurrent_sends(self):
        """Buffer should be thread-safe for concurrent sends."""
        import threading

        client = Plexus(
            api_key="test_key",
            endpoint="http://localhost:9999",
            retry_config=RetryConfig(max_retries=0, base_delay=0.01, jitter=False),
        )

        errors = []

        def send_metric(metric_id):
            try:
                try:
                    client.send(f"metric_{metric_id}", float(metric_id))
                except PlexusError:
                    pass  # Expected
            except Exception as e:
                errors.append(e)

        # Patch once outside the threads — patch.object mutates instance
        # attrs and is not thread-safe under contention. We just need every
        # call to fail so the buffer takes the write.
        mock_session = MagicMock()
        mock_session.return_value.post.side_effect = _Timeout()

        with patch.object(client, "_get_session", mock_session):
            threads = [
                threading.Thread(target=send_metric, args=(i,)) for i in range(20)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # No errors should have occurred
        assert len(errors) == 0
        # All points should be buffered (though exact count may vary due to timing)
        assert client.buffer_size() > 0
