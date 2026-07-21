"""
Thermal camera streaming to Plexus.

Reads from any supported thermal camera and sends frames to Plexus using
the SDK's send_thermal_frame() method. The gateway relays colorized JPEG
frames to the app, along with temperature range and (for small sensors)
per-pixel temperature data.

Supported hardware (--camera argument):
    sim         Simulated camera — no hardware needed (default)
    mlx90640    MLX90640 32×24 I2C sensor (pip install adafruit-circuitpython-mlx90640)
    mlx90641    MLX90641 16×12 I2C sensor (pip install adafruit-circuitpython-mlx90641)
    usb         USB thermal camera in Y16 format (InfiRay, Topdon, Seek, etc.)

Run:
    export PLEXUS_API_KEY=plx_xxx
    python thermal_camera.py
    python thermal_camera.py --camera mlx90640
    python thermal_camera.py --camera usb --device 2
"""

import argparse
import sys
import time

from plexus import Plexus
from plexus.cameras.thermal import NoCameraFound, ThermalSource

CAMERA_ID = "thermal"
FPS = 5


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream thermal camera to Plexus.")
    parser.add_argument(
        "--camera",
        choices=["sim", "mlx90640", "mlx90641", "usb"],
        default=None,
        help="Camera driver (default: auto-detect)",
    )
    parser.add_argument(
        "--device",
        type=int,
        default=0,
        help="USB video device index (default: 0)",
    )
    args = parser.parse_args()

    hint = args.device if args.camera == "usb" else args.camera
    try:
        cam = ThermalSource.open(hint)
    except NoCameraFound as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    px = Plexus()

    interval = 1.0 / FPS
    frame_count = 0
    print(f"Streaming {cam.width}×{cam.height} thermal at {FPS} fps — Ctrl-C to stop")

    try:
        while True:
            t0 = time.time()

            temps = cam.read_frame()
            px.send_thermal_frame(temps, camera_id=CAMERA_ID)

            frame_count += 1
            if frame_count % 50 == 0:
                print(f"  {frame_count} frames sent")

            elapsed = time.time() - t0
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

    except KeyboardInterrupt:
        pass
    finally:
        cam.close()
        px.stop()
        print(f"Done. {frame_count} frames sent.")


if __name__ == "__main__":
    main()
