"""Tests for video frame encoding and streaming helpers."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest

from plexus.client import Plexus, read_mjpeg_frames

# ---------------------------------------------------------------------------
# Minimal JPEG bytes fixture (SOI + EOI, valid enough for passthrough tests)
# ---------------------------------------------------------------------------

_TINY_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12 + b"\xff\xd9"


def _bare_client():
    """Instantiate Plexus with just enough state to call _encode_frame."""
    px = Plexus.__new__(Plexus)
    px._cv2 = None
    px._pil_image = None
    px._fit_warned = False
    return px


# ---------------------------------------------------------------------------
# _encode_frame — JPEG bytes passthrough
# ---------------------------------------------------------------------------

class TestEncodeFrameJpegPassthrough:
    def test_jpeg_bytes_returned_unchanged(self):
        px = _bare_client()
        with patch.object(px, "_get_pil") as mock_pil:
            # Simulate Pillow unavailable so we hit the zero-dimension fallback
            mock_pil.return_value = None
            # Pillow open will fail → fallback path
            result, w, h = px._encode_frame(_TINY_JPEG, quality=85)
        assert result == _TINY_JPEG

    def test_jpeg_bytes_with_pillow_returns_dimensions(self):
        px = _bare_client()
        pil_mock = MagicMock()
        img_mock = MagicMock()
        img_mock.width = 320
        img_mock.height = 240
        pil_mock.open.return_value = img_mock

        with patch.object(px, "_get_pil", return_value=pil_mock):
            result, w, h = px._encode_frame(_TINY_JPEG, quality=85)

        assert result == _TINY_JPEG
        assert w == 320
        assert h == 240


# ---------------------------------------------------------------------------
# _encode_frame — numpy array input (mocked OpenCV)
# ---------------------------------------------------------------------------

class TestEncodeFrameNumpy:
    def test_numpy_array_encoded_via_cv2(self):
        np = pytest.importorskip("numpy")
        px = _bare_client()

        cv2_mock = MagicMock()
        fake_jpeg = _TINY_JPEG
        cv2_mock.imencode.return_value = (True, MagicMock(__bytes__=lambda s: fake_jpeg))
        cv2_mock.IMWRITE_JPEG_QUALITY = 1
        px._cv2 = cv2_mock

        arr = np.zeros((480, 640, 3), dtype=np.uint8)
        _, w, h = px._encode_frame(arr, quality=85)
        assert w == 640
        assert h == 480
        cv2_mock.imencode.assert_called_once()


# ---------------------------------------------------------------------------
# _encode_frame — unsupported type
# ---------------------------------------------------------------------------

class TestEncodeFrameUnsupported:
    def test_raises_value_error_for_string(self):
        px = _bare_client()
        with pytest.raises(ValueError, match="Unsupported frame type"):
            px._encode_frame("not-a-frame", quality=85)

    def test_raises_value_error_for_int(self):
        px = _bare_client()
        with pytest.raises(ValueError, match="Unsupported frame type"):
            px._encode_frame(42, quality=85)

    def test_pil_image_raises_value_error(self):
        pil = pytest.importorskip("PIL.Image")
        img = pil.new("RGB", (64, 48))
        px = _bare_client()
        with pytest.raises(ValueError, match="Unsupported frame type"):
            px._encode_frame(img, quality=85)


# ---------------------------------------------------------------------------
# read_mjpeg_frames — boundary parsing
# ---------------------------------------------------------------------------

def _make_pipe(data: bytes):
    return io.BytesIO(data)


class TestReadMjpegFrames:
    def test_single_frame(self):
        frames = list(read_mjpeg_frames(_make_pipe(_TINY_JPEG)))
        assert frames == [_TINY_JPEG]

    def test_two_frames(self):
        stream = _TINY_JPEG + _TINY_JPEG
        frames = list(read_mjpeg_frames(_make_pipe(stream)))
        assert len(frames) == 2
        assert all(f == _TINY_JPEG for f in frames)

    def test_garbage_before_soi_ignored(self):
        stream = b"\x00\x01\x02" + _TINY_JPEG
        frames = list(read_mjpeg_frames(_make_pipe(stream)))
        assert frames == [_TINY_JPEG]

    def test_incomplete_frame_not_yielded(self):
        # Stream ends mid-frame (no EOI)
        stream = b"\xff\xd8\xff\xe0" + b"\x00" * 4
        frames = list(read_mjpeg_frames(_make_pipe(stream)))
        assert frames == []

    def test_many_frames(self):
        stream = _TINY_JPEG * 100
        frames = list(read_mjpeg_frames(_make_pipe(stream), chunk=32))
        assert len(frames) == 100


# ---------------------------------------------------------------------------
# _fit_to_wire — adaptive quality downsampling
# ---------------------------------------------------------------------------

class TestFitToWire:
    def _make_oversized_jpeg(self) -> bytes:
        """Return a real JPEG large enough to exceed _FRAME_JPEG_MAX at quality=95."""
        pil = pytest.importorskip("PIL.Image")
        import os
        img = pil.frombytes("RGB", (1920, 1080), os.urandom(1920 * 1080 * 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    def test_small_frame_returned_unchanged(self):
        pil = pytest.importorskip("PIL.Image")
        import io
        img = pil.new("RGB", (64, 48), color=(100, 150, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        jpeg = buf.getvalue()
        assert len(jpeg) < 750_000

        px = _bare_client()
        result = px._fit_to_wire(jpeg, requested_quality=85)
        assert result == jpeg

    def test_oversized_frame_is_reduced(self):
        from plexus.client import _FRAME_JPEG_MAX
        jpeg = self._make_oversized_jpeg()
        if len(jpeg) <= _FRAME_JPEG_MAX:
            pytest.skip("test image didn't produce an oversized JPEG at this quality")

        px = _bare_client()
        result = px._fit_to_wire(jpeg, requested_quality=95)
        assert len(result) <= _FRAME_JPEG_MAX
        assert result[:2] == b"\xff\xd8"

    def test_warns_only_once(self):
        from plexus.client import _FRAME_JPEG_MAX
        pil = pytest.importorskip("PIL.Image")
        import os
        img = pil.frombytes("RGB", (1920, 1080), os.urandom(1920 * 1080 * 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        jpeg = buf.getvalue()
        if len(jpeg) <= _FRAME_JPEG_MAX:
            pytest.skip("test image not oversized enough")

        px = _bare_client()
        warnings = []
        with patch("plexus.client._say", side_effect=lambda msg: warnings.append(msg)):
            px._fit_to_wire(jpeg, requested_quality=95)
            px._fit_to_wire(jpeg, requested_quality=95)
            px._fit_to_wire(jpeg, requested_quality=95)
        assert len(warnings) == 1

    def test_oversized_without_pillow_returns_original(self):
        from plexus.client import _FRAME_JPEG_MAX
        # Fake an oversized JPEG blob
        fake = b"\xff\xd8" + b"\x00" * (_FRAME_JPEG_MAX + 1) + b"\xff\xd9"
        px = _bare_client()
        px._pil_image = None  # ensure no cached PIL

        with pytest.MonkeyPatch().context() as mp:
            mp.setitem(__import__("sys").modules, "PIL", None)
            mp.setitem(__import__("sys").modules, "PIL.Image", None)
            result = px._fit_to_wire(fake, requested_quality=95)
        assert result is fake
