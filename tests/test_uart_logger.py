"""Tests for uart_logger using a fake serial port (no hardware needed)."""

import csv
import io
import threading

import pytest

import uart_logger


class FakeSerial:
    """Minimal pyserial stand-in: serves scripted lines, then stops."""

    def __init__(self, lines, fail_after=None):
        # lines: list of bytes returned by readline(); fail_after=N raises
        # SerialError on the Nth readline() call to simulate a dropped port.
        self._lines = list(lines)
        self._fail_after = fail_after
        self._calls = 0
        self.closed = False

    def readline(self):
        self._calls += 1
        if self._fail_after is not None and self._calls > self._fail_after:
            raise FakeSerialError("port dropped")
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def close(self):
        self.closed = True


class FakeSerialError(Exception):
    pass


def make_writer():
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=uart_logger.CSV_FIELDS)
    writer.writeheader()
    return buf, writer


def stop_after_n_writes(stop, n, buf, poll=0.001):
    """Helper: watch the CSV buffer and set stop once n data rows exist."""
    import time

    def _watch():
        while True:
            buf.seek(0)
            rows = list(csv.DictReader(buf))
            if len(rows) >= n:
                stop.set()
                return
            time.sleep(poll)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()
    return t


def test_logs_frames_with_timestamps():
    stop = threading.Event()
    buf, writer = make_writer()
    ser = FakeSerial([b"temp=21.5\n", b"temp=21.6\n", b"\n", b"hum=40\n"])

    stop_after_n_writes(stop, 3, buf)
    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop)

    assert clean is True
    assert count == 3  # blank line skipped
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert [r["frame"] for r in rows] == ["temp=21.5", "temp=21.6", "hum=40"]
    assert all(r["port"] == "COM1" for r in rows)
    assert all(r["timestamp"] for r in rows)  # non-empty ISO timestamps


def test_read_frames_reports_port_failure():
    stop = threading.Event()
    buf, writer = make_writer()
    ser = FakeSerial([b"one\n", b"two\n"], fail_after=2)

    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop)

    assert clean is False  # port dropped -> caller should reconnect
    assert count == 2


def test_run_reconnects_and_continues():
    stop = threading.Event()
    buf, writer = make_writer()

    # First open drops after 1 line; second open serves 1 more line.
    ports = [
        FakeSerial([b"before-drop\n"], fail_after=1),
        FakeSerial([b"after-reconnect\n"]),
    ]
    created = []

    def opener(port, baud, timeout):
        ser = ports.pop(0)
        created.append(ser)
        return ser

    stop_after_n_writes(stop, 2, buf)
    logged = uart_logger.run("COM1", 115200, "unused.csv",
                             port_opener=opener, stop=stop, writer=writer,
                             sleep=lambda s: None)

    assert logged == 2
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert [r["frame"] for r in rows] == ["before-drop", "after-reconnect"]
    assert all(s.closed for s in created)  # every port closed after use


def test_run_gives_up_after_max_retries(capsys):
    stop = threading.Event()
    buf, writer = make_writer()

    def opener(port, baud, timeout):
        raise FakeSerialError("no such port")

    logged = uart_logger.run("COM9", 9600, "unused.csv", max_retries=2,
                             reconnect_delay=0, port_opener=opener, stop=stop,
                             writer=writer, sleep=lambda s: None)

    assert logged == 0
    assert "max retries exceeded" in capsys.readouterr().err


def test_argparse_defaults():
    args = uart_logger.parse_args(["--port", "/dev/ttyUSB0"])
    assert args.port == "/dev/ttyUSB0"
    assert args.baud == 115200
    assert args.output == "uart_log.csv"
    assert args.max_retries == 0


def test_decode_frame_handles_binary():
    assert uart_logger.decode_frame(b"\xff\xfe\n") != ""
    assert uart_logger.decode_frame(b"  ok  \r\n") == "ok"
