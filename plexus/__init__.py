"""
Plexus — thin Python SDK for sending telemetry to the Plexus gateway.

    from plexus import Plexus

    px = Plexus(api_key="plx_xxx", source_id="device-001")
    px.send("temperature", 72.5)

At bench rates, batch — `send()` is one WebSocket frame per call and the
gateway limits frames, not points:

    with px.run("hotfire-03"), px.batch(interval_ms=50) as b:
        b.send("att.rate_x", gyro.x)
"""

from plexus.batching import BatchSender
from plexus.client import (
    AuthenticationError,
    Plexus,
    PlexusError,
    RateLimitedError,
    read_mjpeg_frames,
)
from plexus.config import RetryConfig

__version__ = "0.11.1"
__all__ = [
    "AuthenticationError",
    "BatchSender",
    "Plexus",
    "PlexusError",
    "RateLimitedError",
    "RetryConfig",
    "read_mjpeg_frames",
]
