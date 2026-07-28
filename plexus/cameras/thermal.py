"""
Thermal camera drivers for Plexus.

Provides a hardware-agnostic interface over I2C sensors (MLX90640, MLX90641)
and USB thermal cameras (Y16 pixel format). All drivers return a unified
ThermalFrame containing a colorized JPEG image plus temperature metadata.

Usage:
    from plexus.cameras.thermal import ThermalSource

    cam = ThermalSource.open("sim")     # simulated, no hardware
    cam = ThermalSource.open("mlx90640")  # I2C MLX90640
    cam = ThermalSource.open("usb")     # USB thermal at index 0

    while True:
        px.send_thermal_frame(cam.read_frame(), camera_id="thermal")
        time.sleep(1 / 5)
"""

from __future__ import annotations

import base64
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

# Inferno is perceptually uniform and well-suited for thermal imaging.
# Swap for cv2.COLORMAP_HOT or cv2.COLORMAP_JET if preferred.
_COLORMAP = cv2.COLORMAP_INFERNO

# Sensors at or below this pixel count include the full temps array in the
# wire message. I2C sensors (32×24 = 768, 16×12 = 192) always qualify.
# USB thermal cameras (256×192 = 49 152) do not.
_TEMPS_INLINE_THRESHOLD = 4096

# Upscale sensors whose shortest side is below this threshold.
_MIN_DISPLAY_SIDE = 160


class NoCameraFound(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class ThermalCamera(ABC):
    """Hardware-agnostic thermal camera interface.

    Subclass and implement read_frame() to support any sensor.
    read_frame() must return a 2-D float32 array of temperatures in Celsius,
    shaped (height, width).
    """

    @property
    @abstractmethod
    def width(self) -> int: ...

    @property
    @abstractmethod
    def height(self) -> int: ...

    @abstractmethod
    def read_frame(self) -> np.ndarray: ...

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Unified output
# ---------------------------------------------------------------------------


@dataclass
class ThermalFrame:
    """Encoded output from a ThermalCamera, ready to send to the gateway."""

    image: np.ndarray        # uint8 BGR colorized image (display size)
    width: int               # display width (may differ from sensor if upscaled)
    height: int              # display height
    sensor_width: int        # native sensor width
    sensor_height: int       # native sensor height
    temp_min: float
    temp_max: float
    temps: np.ndarray | None  # native res; None when sensor > threshold
    timestamp_ms: int

    def to_message(
        self, camera_id: str, source_id: str | None = None, quality: int = 85
    ) -> dict[str, Any]:
        """Build the gateway `video_frame` wire message for this frame."""
        _, buf = cv2.imencode(".jpg", self.image, [cv2.IMWRITE_JPEG_QUALITY, quality])
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")

        msg: dict[str, Any] = {
            "type": "video_frame",
            "camera_id": camera_id,
            "frame": b64,
            "width": self.width,
            "height": self.height,
            "timestamp": self.timestamp_ms,
            "video_type": "thermal",
            "sensor_width": self.sensor_width,
            "sensor_height": self.sensor_height,
            "temp_min": self.temp_min,
            "temp_max": self.temp_max,
        }
        if source_id is not None:
            msg["source_id"] = source_id
        if self.temps is not None:
            msg["temps"] = self.temps.flatten().tolist()
        return msg


# ---------------------------------------------------------------------------
# Encoding — single shared path used by both encode_frame() and the SDK's
# Plexus.send_thermal_frame().
# ---------------------------------------------------------------------------


def _upscale_size(sw: int, sh: int) -> tuple[int, int]:
    if sw >= _MIN_DISPLAY_SIDE and sh >= _MIN_DISPLAY_SIDE:
        return sw, sh
    scale = max(_MIN_DISPLAY_SIDE / sw, _MIN_DISPLAY_SIDE / sh)
    return round(sw * scale), round(sh * scale)


def build_thermal_frame(
    temps: np.ndarray, timestamp_ms: int | None = None
) -> ThermalFrame:
    """Colorize a temperature array into a ThermalFrame.

    temps: 2-D float32 Celsius array, shape (height, width).
    timestamp_ms: wire timestamp; defaults to current time.
    """
    sh, sw = temps.shape[:2]

    temp_min = float(np.min(temps))
    temp_max = float(np.max(temps))
    span = temp_max - temp_min

    normalized = (
        np.zeros((sh, sw), dtype=np.uint8)
        if span < 1e-6
        else ((temps - temp_min) / span * 255).astype(np.uint8)
    )

    colored = cv2.applyColorMap(normalized, _COLORMAP)

    dw, dh = _upscale_size(sw, sh)
    if (dw, dh) != (sw, sh):
        colored = cv2.resize(colored, (dw, dh), interpolation=cv2.INTER_CUBIC)

    return ThermalFrame(
        image=colored,
        width=dw,
        height=dh,
        sensor_width=sw,
        sensor_height=sh,
        temp_min=round(temp_min, 2),
        temp_max=round(temp_max, 2),
        temps=temps if sw * sh <= _TEMPS_INLINE_THRESHOLD else None,
        timestamp_ms=timestamp_ms if timestamp_ms is not None else int(time.time() * 1000),
    )


def encode_frame(cam: ThermalCamera) -> ThermalFrame:
    """Read one frame from a camera and colorize it."""
    return build_thermal_frame(cam.read_frame())


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


class SimulatedThermalCamera(ThermalCamera):
    """Simulated thermal camera for testing without hardware.

    Produces a moving warm blob over a noisy background at 32×24.
    """

    def __init__(self, width: int = 32, height: int = 24) -> None:
        self._width = width
        self._height = height
        self._t = 0.0

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def read_frame(self) -> np.ndarray:
        self._t += 0.1
        x = np.linspace(0, 2 * np.pi, self._width)
        y = np.linspace(0, 2 * np.pi, self._height)
        xx, yy = np.meshgrid(x, y)
        base = 22.0 + 3.0 * np.sin(xx + self._t) * np.cos(yy + self._t * 0.7)
        cx = int((np.sin(self._t * 0.5) * 0.4 + 0.5) * self._width)
        cy = int((np.cos(self._t * 0.3) * 0.4 + 0.5) * self._height)
        yg, xg = np.ogrid[: self._height, : self._width]
        hotspot = 15.0 * np.exp(-((xg - cx) ** 2 + (yg - cy) ** 2) / 20.0)
        return (base + hotspot).astype(np.float32)


class USBThermalCamera(ThermalCamera):
    """USB thermal camera via V4L2/UVC in Y16 pixel format.

    Most USB thermal cameras (InfiRay, Topdon, Seek, some FLIR) present as
    standard UVC video devices outputting Y16 frames where each uint16 pixel
    encodes temperature as: celsius = (value / 100.0) - 273.15

    Requires: pip install opencv-python
    """

    _KELVIN_SCALE = 100.0
    _KELVIN_OFFSET = 273.15

    def __init__(self, device_index: int = 0) -> None:
        self._cap = cv2.VideoCapture(device_index)
        if not self._cap.isOpened():
            raise NoCameraFound(f"Cannot open video device {device_index}")
        self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"Y16 "))
        ret, frame = self._cap.read()
        if not ret or frame is None:
            self._cap.release()
            raise NoCameraFound(
                f"Device {device_index} opened but could not read a frame. "
                "Check that it supports Y16 format."
            )
        self._width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self._height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    def read_frame(self) -> np.ndarray:
        ret, raw = self._cap.read()
        if not ret or raw is None:
            raise RuntimeError("Failed to read frame from USB thermal camera")
        u16 = raw.view(np.uint16).reshape(self._height, self._width)
        return (u16.astype(np.float32) / self._KELVIN_SCALE) - self._KELVIN_OFFSET

    def close(self) -> None:
        self._cap.release()


class MLX90640Camera(ThermalCamera):
    """MLX90640 32×24 thermal array via I2C.

    Requires: pip install adafruit-circuitpython-mlx90640
    Wiring:   SCL → GPIO 3, SDA → GPIO 2, 3.3V power (Raspberry Pi).
    """

    def __init__(self) -> None:
        import adafruit_mlx90640
        import board
        import busio

        i2c = busio.I2C(board.SCL, board.SDA, frequency=800_000)
        self._mlx = adafruit_mlx90640.MLX90640(i2c)
        self._mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_4_HZ
        self._buf = [0.0] * 768

    @property
    def width(self) -> int:
        return 32

    @property
    def height(self) -> int:
        return 24

    def read_frame(self) -> np.ndarray:
        self._mlx.getFrame(self._buf)
        return np.array(self._buf, dtype=np.float32).reshape(24, 32)


class MLX90641Camera(ThermalCamera):
    """MLX90641 16×12 thermal array via I2C.

    Requires: pip install adafruit-circuitpython-mlx90641
    Wiring:   SCL → GPIO 3, SDA → GPIO 2, 3.3V power (Raspberry Pi).
    """

    def __init__(self) -> None:
        import adafruit_mlx90641
        import board
        import busio

        i2c = busio.I2C(board.SCL, board.SDA, frequency=400_000)
        self._mlx = adafruit_mlx90641.MLX90641(i2c)
        self._mlx.refresh_rate = adafruit_mlx90641.RefreshRate.REFRESH_4_HZ
        self._buf = [0.0] * 192

    @property
    def width(self) -> int:
        return 16

    @property
    def height(self) -> int:
        return 12

    def read_frame(self) -> np.ndarray:
        self._mlx.getFrame(self._buf)
        return np.array(self._buf, dtype=np.float32).reshape(12, 16)


# ---------------------------------------------------------------------------
# Auto-detection factory
# ---------------------------------------------------------------------------


class ThermalSource:
    """Factory for opening a thermal camera by type.

    A camera type must be specified explicitly — auto-detection is not supported
    because USB thermal cameras and regular webcams are indistinguishable at the
    OS level without knowing what you're looking for.

    Args:
        hint: Which camera to open. One of:
            "sim"      — simulated 32×24 camera, no hardware required
            "mlx90640" — MLX90640 32×24 I2C sensor (Raspberry Pi, 3.3V)
            "mlx90641" — MLX90641 16×12 I2C sensor (Raspberry Pi, 3.3V)
            "usb"      — USB thermal camera at device index 0
            <int>      — USB thermal camera at a specific device index

    Raises:
        ValueError: If hint is None or unrecognised.
        NoCameraFound: If the specified device cannot be opened.

    Examples:
        cam = ThermalSource.open("sim")        # no hardware
        cam = ThermalSource.open("mlx90640")   # I2C MLX90640
        cam = ThermalSource.open("usb")        # USB at index 0
        cam = ThermalSource.open(2)            # USB at index 2
    """

    @staticmethod
    def open(hint: str | int) -> ThermalCamera:
        if hint == "sim":
            return SimulatedThermalCamera()
        if hint == "mlx90640":
            return MLX90640Camera()
        if hint == "mlx90641":
            return MLX90641Camera()
        if hint == "usb" or isinstance(hint, int):
            return USBThermalCamera(0 if hint == "usb" else hint)
        raise ValueError(
            f"Unknown camera hint {hint!r}. "
            "Valid options: 'sim', 'mlx90640', 'mlx90641', 'usb', or a device index (int)."
        )
