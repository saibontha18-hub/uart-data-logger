#!/usr/bin/env python3
"""uart-data-logger: log newline-terminated frames from a serial port to CSV.

Reads lines from the port, stamps each with a UTC ISO-8601 timestamp, and
appends them to a CSV file. Automatically reconnects if the port drops.
"""

import argparse
import csv
import signal
import sys
import threading
import time
from datetime import datetime, timezone

DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT = 1.0
DEFAULT_RECONNECT_DELAY = 2.0

CSV_FIELDS = ["timestamp", "port", "frame"]


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Log serial-port frames to CSV with timestamps.")
    p.add_argument("--port", required=True,
                   help="Serial port, e.g. /dev/ttyUSB0 or COM3")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                   help="Baud rate (default: %(default)s)")
    p.add_argument("--output", default="uart_log.csv",
                   help="CSV output file (default: %(default)s)")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                   help="Read timeout in seconds (default: %(default)s)")
    p.add_argument("--reconnect-delay", type=float,
                   default=DEFAULT_RECONNECT_DELAY,
                   help="Seconds to wait before reconnecting "
                        "(default: %(default)s)")
    p.add_argument("--max-retries", type=int, default=0,
                   help="Max reconnect attempts, 0 = unlimited "
                        "(default: %(default)s)")
    return p.parse_args(argv)


def default_opener(port, baud, timeout):
    """Open a real serial port (pyserial)."""
    import serial
    return serial.Serial(port, baudrate=baud, timeout=timeout)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def decode_frame(raw):
    return raw.decode("utf-8", errors="replace").strip()


def read_frames(ser, writer, port, stop):
    """Read lines until stop is set or the port fails.

    Returns (clean_exit, frames_logged): clean_exit is True when stop was
    requested, False when the port raised (caller should reconnect).
    """
    count = 0
    while not stop.is_set():
        try:
            raw = ser.readline()
        except Exception:
            return False, count
        if not raw:
            continue  # read timeout, keep waiting
        frame = decode_frame(raw)
        if frame == "":
            continue
        writer.writerow({
            "timestamp": utc_now_iso(),
            "port": port,
            "frame": frame,
        })
        count += 1
    return True, count


def run(port, baud, output, timeout=DEFAULT_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY, max_retries=0,
        port_opener=default_opener, stop=None, writer=None,
        close_after=True, sleep=time.sleep):
    """Main logging loop with automatic reconnect.

    Returns the total number of frames logged.
    """
    if stop is None:
        stop = threading.Event()

    fh = None
    if writer is None:
        fh = open(output, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if fh.tell() == 0:
            writer.writeheader()

    logged = 0
    retries = 0
    try:
        while not stop.is_set():
            try:
                ser = port_opener(port, baud, timeout)
            except Exception as exc:
                retries += 1
                print(f"open failed ({exc}), retry {retries} in "
                      f"{reconnect_delay}s", file=sys.stderr)
                if max_retries and retries > max_retries:
                    print("max retries exceeded, giving up", file=sys.stderr)
                    break
                sleep(reconnect_delay)
                continue

            print(f"connected: {port} @ {baud} baud", file=sys.stderr)
            retries = 0
            try:
                clean, n = read_frames(ser, writer, port, stop)
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
            logged += n
            if clean:
                break
            print(f"port error, reconnecting in {reconnect_delay}s",
                  file=sys.stderr)
            sleep(reconnect_delay)
    finally:
        if fh is not None and close_after:
            fh.close()
    return logged


def main(argv=None):
    args = parse_args(argv)
    stop = threading.Event()

    def _handle(signum, frame):
        print("\nstopping...", file=sys.stderr)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    frames = run(args.port, args.baud, args.output, args.timeout,
                 args.reconnect_delay, args.max_retries, stop=stop)
    print(f"logged {frames} frames to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
