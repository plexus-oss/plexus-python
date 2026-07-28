"""
Mac system metrics with structured event logs.

Install deps:
    pip install plexus-python psutil

Run:
    python examples/mac_metrics.py --api-key plx_xxx
    python examples/mac_metrics.py --api-key plx_xxx --interval 10
"""

import argparse
import time

import psutil

from plexus import Plexus

parser = argparse.ArgumentParser(description="Stream Mac system metrics to Plexus.")
parser.add_argument("--api-key", required=True, help="Plexus API key (plx_xxx)")
parser.add_argument("--interval", type=float, default=5.0, metavar="SECONDS",
                    help="Sampling interval in seconds (default: 5)")
parser.add_argument("--source-id", default="macbook", help="Device source ID (default: macbook)")
args = parser.parse_args()

px = Plexus(api_key=args.api_key, source_id=args.source_id)

CPU_SPIKE_THRESHOLD = 80.0   # %
MEM_PRESSURE_THRESHOLD = 85.0  # %

prev_net = psutil.net_io_counters()
prev_disk = psutil.disk_io_counters()
prev_charging = None

while True:
    net = psutil.net_io_counters()
    disk = psutil.disk_io_counters()

    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()

    # Numeric metrics
    px.send("cpu.percent",     cpu)
    px.send("memory.used_gb",  mem.used / 1e9)
    px.send("memory.percent",  mem.percent)
    px.send("net.rx_mbps",     (net.bytes_recv  - prev_net.bytes_recv)   / 1e6)
    px.send("net.tx_mbps",     (net.bytes_sent  - prev_net.bytes_sent)   / 1e6)
    px.send("disk.read_mbps",  (disk.read_bytes  - prev_disk.read_bytes)  / 1e6)
    px.send("disk.write_mbps", (disk.write_bytes - prev_disk.write_bytes) / 1e6)
    px.send("disk.free_gb",    psutil.disk_usage("/").free / 1e9)

    # Battery metrics + charge-state change event
    battery = psutil.sensors_battery()
    if battery:
        px.send("battery.percent", battery.percent)
        charging = battery.power_plugged
        if charging != prev_charging and prev_charging is not None:
            px.event("battery.state_change", {
                "charging": charging,
                "percent": battery.percent,
            })
        prev_charging = charging

    # CPU spike event — fires once per interval when threshold is crossed
    if cpu >= CPU_SPIKE_THRESHOLD:
        top = max(psutil.process_iter(["name", "cpu_percent"]), key=lambda p: p.info["cpu_percent"] or 0)
        px.event("cpu.spike", {
            "cpu_percent": cpu,
            "top_process": top.info["name"],
            "top_process_percent": top.info["cpu_percent"],
        })

    # Memory pressure event
    if mem.percent >= MEM_PRESSURE_THRESHOLD:
        top = max(psutil.process_iter(["name", "memory_percent"]), key=lambda p: p.info["memory_percent"] or 0)
        px.event("memory.pressure", {
            "memory_percent": mem.percent,
            "top_process": top.info["name"],
            "top_process_percent": round(top.info["memory_percent"], 1),
        })

    prev_net, prev_disk = net, disk
    time.sleep(args.interval)
