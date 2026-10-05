#!/usr/bin/env python3
"""uart-data-logger: log frames from a serial port to CSV.

Reads frames from the port, stamps each with a UTC ISO-8601 timestamp, and
appends them to a CSV file. Automatically reconnects if the port drops.

Optional extras: --stats prints live throughput to stderr once per second,
--max-size rotates the CSV file when it grows past a byte limit, and
--frame enables fixed-length checksum-validated binary frames.
"""

import argparse
import csv
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT = 1.0
DEFAULT_RECONNECT_DELAY = 2.0
STATS_INTERVAL = 1.0  # seconds between --stats reports

CSV_FIELDS = ["timestamp", "port", "frame"]


class LinkStats:
    """Byte/frame/error counters with per-second rate snapshots.

    `clock` defaults to time.monotonic, but tests inject a fake one so
    the reporting interval stays deterministic.
    """

    def __init__(self, clock=time.monotonic):
        self.frames = 0
        self.bytes = 0
        self.errors = 0
        self._clock = clock
        now = clock()
        self._last_t = now
        self._last_report = now
        self._last_frames = 0
        self._last_bytes = 0

    def record_rx(self, nbytes):
        self.bytes += nbytes

    def record_frame(self):
        self.frames += 1

    def record_error(self):
        self.errors += 1

    def report_due(self, interval=STATS_INTERVAL):
        return (self._clock() - self._last_report) >= interval

    def snapshot(self):
        """Return (frames/s, bytes/s, total errors) since last snapshot."""
        now = self._clock()
        dt = now - self._last_t
        if dt > 0:
            fps = (self.frames - self._last_frames) / dt
            bps = (self.bytes - self._last_bytes) / dt
        else:
            fps = bps = 0.0
        self._last_t, self._last_frames, self._last_bytes = (
            now, self.frames, self.bytes)
        self._last_report = now
        return fps, bps, self.errors


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
    p.add_argument("--stats", action="store_true",
                   help="Print frames/s, bytes/s and error count to stderr "
                        "once per second while logging")
    p.add_argument("--max-size", type=int, default=0, metavar="BYTES",
                   help="Rotate to a new timestamped CSV file when the "
                        "current one exceeds BYTES (0 = no rotation)")
    p.add_argument("--frame", nargs=2, metavar=("SYNC", "LEN"),
                   help="Fixed-frame mode: only log LEN-byte frames that "
                        "start with sync byte SYNC and carry a valid "
                        "trailing XOR checksum (e.g. --frame 0xAA 8)")
    return p.parse_args(argv)


def parse_frame_spec(spec):
    """Parse --frame SYNC LEN into (sync_byte, length).

    Accepts decimal or 0x-prefixed hex; raises ValueError on junk.
    """
    if spec is None:
        return None
    try:
        sync = int(spec[0], 0)
        length = int(spec[1], 0)
    except (ValueError, TypeError):
        raise ValueError(f"invalid --frame SYNC LEN: {spec!r}")
    if not 0 <= sync <= 255:
        raise ValueError(f"SYNC byte out of range 0-255: {spec[0]!r}")
    if length < 2:
        raise ValueError(
            f"frame length must be >= 2 (sync + checksum), got {length}")
    return sync, length


def xor_checksum(data):
    """XOR of all bytes (the checksum scheme used by --frame)."""
    cs = 0
    for b in data:
        cs ^= b
    return cs


def validate_frame(raw, sync, length):
    """True if raw is a valid fixed frame: [SYNC][payload...][XOR checksum]."""
    return (len(raw) == length and raw[0] == sync
            and xor_checksum(raw[:-1]) == raw[-1])


class FrameSync:
    """Buffered sync-byte scanner for fixed-frame mode.

    pyserial's read(n) returns whatever arrived before the timeout, which
    is often fewer than n bytes — so one read() is not one frame, and a
    bare read(length) loop silently desyncs the stream on the first short
    read. This keeps a buffer across reads, scans it byte by byte for the
    SYNC byte (dropping anything before it as partial-read junk), and only
    hands back a candidate once `length` bytes sit behind a SYNC. A
    rejected candidate is dropped whole; scanning then resumes after it.
    """

    def __init__(self, ser, sync, length):
        self._ser = ser
        self._sync = sync
        self._length = length
        self._buf = b""

    def next_candidate(self):
        """Return (candidate, bytes_read).

        candidate is a length-byte frame starting with SYNC, or None on a
        read timeout — partial data stays buffered across calls, so a frame
        split over several reads still assembles. bytes_read is how many
        new bytes came off the port in this call, for the stats counters.
        Whatever ser.read() raises propagates, so the caller still
        reconnects on a dropped port.
        """
        nbytes = 0
        while True:
            # drop everything before the first sync byte, one byte at a
            # time — it's leftover from a partial read, not a frame start
            while self._buf and self._buf[0] != self._sync:
                self._buf = self._buf[1:]
            if len(self._buf) >= self._length:
                cand = self._buf[:self._length]
                self._buf = self._buf[self._length:]
                return cand, nbytes
            chunk = self._ser.read(self._length - len(self._buf))
            if not chunk:
                return None, nbytes
            nbytes += len(chunk)
            self._buf += chunk


def _utc_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def rotated_name(output):
    """Insert a UTC timestamp before the extension: a.csv -> a_<stamp>.csv."""
    base, ext = os.path.splitext(output)
    return f"{base}_{_utc_stamp()}{ext or '.csv'}"


class CsvSink:
    """CSV writer with size-based rotation.

    Opens `output` in append mode, writing a header only when the file is
    empty. After each row, if max_size > 0 and we've grown past it, the
    file is closed and a fresh timestamped one is opened. `files` lists
    every path opened, which the rotation tests rely on.
    """

    def __init__(self, output, max_size=0):
        self.output = output
        self.max_size = max_size
        self.files = []
        self._fh = None
        self._writer = None
        self._open(output)

    def _open(self, path):
        # The README's own examples log to paths like logs/run1.csv; dying
        # with FileNotFoundError on a documented invocation is a papercut,
        # so create the directory — the user already told us where the
        # file should live. Also covers rotation, which reopens here with
        # the same parent directory.
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._fh = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        if self._fh.tell() == 0:
            self._writer.writeheader()
        self.files.append(path)

    def _size(self):
        self._fh.flush()
        return os.fstat(self._fh.fileno()).st_size

    def writerow(self, row):
        self._writer.writerow(row)
        if self.max_size > 0 and self._size() > self.max_size:
            self._fh.close()
            self._open(rotated_name(self.output))

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def default_opener(port, baud, timeout):
    """Open a real serial port (pyserial)."""
    import serial
    return serial.Serial(port, baudrate=baud, timeout=timeout)


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def decode_frame(raw):
    return raw.decode("utf-8", errors="replace").strip()


def read_frames(ser, writer, port, stop, stats=None, frame_spec=None):
    """Read frames until stop is set or the port blows up.

    stats: optional LinkStats — counts bytes/frames/errors and prints a
    throughput line to stderr once per second.

    frame_spec: optional (sync, length) tuple for fixed-frame mode. The
    stream is buffered and scanned for the SYNC byte before assembly, so
    short reads from the port can't silently desync it. Bytes ahead of a
    sync byte are skipped as resync junk (not counted); a candidate that
    starts with SYNC but fails its trailing checksum is dropped and counted
    as an error instead of being logged (good frames are logged
    hex-encoded).

    Returns (clean_exit, frames_logged); clean_exit is False when the
    port raised and the caller should reconnect.
    """
    count = 0
    frame_reader = FrameSync(ser, *frame_spec) if frame_spec else None

    def _report():
        if stats is not None and stats.report_due():
            fps, bps, errs = stats.snapshot()
            print(f"stats: {fps:.1f} frames/s, {bps:.1f} bytes/s, "
                  f"{errs} errors", file=sys.stderr)

    while not stop.is_set():
        try:
            if frame_reader is not None:
                raw, nbytes = frame_reader.next_candidate()
            else:
                raw = ser.readline()
                nbytes = len(raw)
        except Exception:
            return False, count
        if stats is not None:
            stats.record_rx(nbytes)
        if not raw:
            continue  # read timeout, keep waiting

        if frame_reader is not None:
            sync, length = frame_spec
            if not validate_frame(raw, sync, length):
                if stats is not None:
                    stats.record_error()
                _report()
                continue  # bad frame already dropped; scan resumes after it
            frame = raw.hex()
        else:
            frame = decode_frame(raw)
            if frame == "":
                continue

        writer.writerow({
            "timestamp": utc_now_iso(),
            "port": port,
            "frame": frame,
        })
        count += 1
        if stats is not None:
            stats.record_frame()
        _report()
    return True, count


def run(port, baud, output, timeout=DEFAULT_TIMEOUT,
        reconnect_delay=DEFAULT_RECONNECT_DELAY, max_retries=0,
        port_opener=default_opener, stop=None, writer=None,
        close_after=True, sleep=time.sleep, stats=False, max_size=0,
        frame_spec=None):
    """Main logging loop with automatic reconnect.

    Returns the total number of frames logged.
    """
    if stop is None:
        stop = threading.Event()

    sink = None
    if writer is None:
        sink = CsvSink(output, max_size=max_size)
        writer = sink

    link_stats = LinkStats() if stats else None

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
                clean, n = read_frames(ser, writer, port, stop,
                                       stats=link_stats, frame_spec=frame_spec)
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
        if sink is not None and close_after:
            sink.close()
    return logged


def main(argv=None):
    args = parse_args(argv)
    try:
        frame_spec = parse_frame_spec(args.frame)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.max_size < 0:
        print("error: --max-size must be >= 0", file=sys.stderr)
        return 2
    stop = threading.Event()

    def _handle(signum, frame):
        print("\nstopping...", file=sys.stderr)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    frames = run(args.port, args.baud, args.output, args.timeout,
                 args.reconnect_delay, args.max_retries, stop=stop,
                 stats=args.stats, max_size=args.max_size,
                 frame_spec=frame_spec)
    print(f"logged {frames} frames to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
