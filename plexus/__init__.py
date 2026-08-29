"""
Plexus — thin Python SDK for sending telemetry to the Plexus gateway.

    from plexus import Plexus

    px = Plexus(api_key="plx_xxx", source_id="device-001")
    px.send("temperature", 72.5)
"""

from plexus.client import AuthenticationError, Plexus, PlexusError, read_mjpeg_frames
from plexus.config import RetryConfig

__version__ = "0.10.0"
__all__ = ["AuthenticationError", "Plexus", "PlexusError", "RetryConfig", "read_mjpeg_frames"]
