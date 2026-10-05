"""Tests for uart_logger using a fake serial port (no hardware needed)."""

import csv
import io
import threading

import pytest

import uart_logger


class FakeSerial:
    """Minimal pyserial stand-in: serves scripted lines, then stops."""

    def __init__(self, lines, fail_after=None, chunks=None, max_read=None):
        # lines: list of bytes returned by readline(); fail_after=N raises
        # SerialError on the Nth read call to simulate a dropped port.
        # chunks: list of bytes objects served (and consumed) by read(n),
        # for fixed-frame mode. max_read: cap each read() at this many bytes
        # to emulate pyserial short reads on a real port.
        self._lines = list(lines)
        self._chunks = list(chunks) if chunks else []
        self._fail_after = fail_after
        self._max_read = max_read
        self._calls = 0
        self.closed = False

    def readline(self):
        self._calls += 1
        if self._fail_after is not None and self._calls > self._fail_after:
            raise FakeSerialError("port dropped")
        if not self._lines:
            return b""
        return self._lines.pop(0)

    def read(self, n):
        self._calls += 1
        if self._fail_after is not None and self._calls > self._fail_after:
            raise FakeSerialError("port dropped")
        out = b""
        while self._chunks and len(out) < n:
            need = n - len(out)
            if self._max_read is not None:
                # one short read per call, like a timeout-driven real port
                need = min(need, self._max_read)
            chunk = self._chunks[0]
            out += chunk[:need]
            if len(chunk) <= need:
                self._chunks.pop(0)
            else:
                self._chunks[0] = chunk[need:]
            if self._max_read is not None:
                break
        return out

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


def test_argparse_stats_flag():
    args = uart_logger.parse_args(["--port", "COM1", "--stats"])
    assert args.stats is True
    args = uart_logger.parse_args(["--port", "COM1"])
    assert args.stats is False  # backward compatible default


def test_link_stats_snapshot_rates():
    now = [1000.0]
    s = uart_logger.LinkStats(clock=lambda: now[0])
    s.record_rx(100)
    s.record_frame()
    s.record_error()
    now[0] = 1002.0
    fps, bps, errs = s.snapshot()
    assert fps == pytest.approx(0.5)
    assert bps == pytest.approx(50.0)
    assert errs == 1
    # second snapshot with no new activity reports zero rates
    now[0] = 1004.0
    fps2, bps2, errs2 = s.snapshot()
    assert fps2 == 0.0
    assert bps2 == 0.0
    assert errs2 == 1


def test_link_stats_report_due():
    now = [0.0]
    s = uart_logger.LinkStats(clock=lambda: now[0])
    assert s.report_due() is False
    now[0] = 0.999
    assert s.report_due() is False
    now[0] = 1.0
    assert s.report_due() is True


def test_stats_printed_once_per_second(capsys):
    now = [5000.0]
    stats = uart_logger.LinkStats(clock=lambda: now[0])
    stop = threading.Event()
    buf, writer = make_writer()
    ser = FakeSerial([b"one\n", b"two\n", b"three\n"])

    # advance the fake clock past the 1s report interval as frames arrive
    orig_record = stats.record_frame

    def hooked():
        orig_record()
        now[0] += 0.4

    stats.record_frame = hooked

    stop_after_n_writes(stop, 3, buf)
    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           stats=stats)

    assert clean is True
    assert count == 3
    assert stats.frames == 3
    assert stats.bytes == len(b"one\n") + len(b"two\n") + len(b"three\n")
    err = capsys.readouterr().err
    assert "frames/s" in err
    assert "bytes/s" in err
    assert "errors" in err


def test_stats_disabled_by_default(capsys):
    stop = threading.Event()
    buf, writer = make_writer()
    ser = FakeSerial([b"one\n"])

    stop_after_n_writes(stop, 1, buf)
    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop)

    assert (clean, count) == (True, 1)
    assert capsys.readouterr().err == ""  # no stats chatter


def test_rotated_name_inserts_timestamp():
    name = uart_logger.rotated_name("logs/run.csv")
    assert name.startswith("logs/run_")
    assert name.endswith(".csv")
    assert name != "logs/run.csv"
    # no extension -> .csv appended
    assert uart_logger.rotated_name("logs/run").endswith(".csv")


def test_csv_sink_writes_header_once(tmp_path):
    out = str(tmp_path / "a.csv")
    sink = uart_logger.CsvSink(out)
    sink.writerow({"timestamp": "t", "port": "p", "frame": "f"})
    sink.close()
    with open(out, encoding="utf-8") as fh:
        assert fh.readline().strip() == "timestamp,port,frame"
    # reopening appends without duplicating the header
    sink = uart_logger.CsvSink(out)
    sink.close()
    with open(out, encoding="utf-8") as fh:
        assert fh.read().count("timestamp,port,frame") == 1


def test_csv_sink_creates_missing_directory(tmp_path):
    # the README's examples use logs/run1.csv — the directory must not
    # need to exist beforehand
    out = str(tmp_path / "newdir" / "nested" / "a.csv")
    sink = uart_logger.CsvSink(out)
    sink.writerow({"timestamp": "t", "port": "p", "frame": "f"})
    sink.close()
    with open(out, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["frame"] for r in rows] == ["f"]


def test_csv_sink_rotates_on_max_size(tmp_path):
    out = str(tmp_path / "log.csv")
    lines = [b"f%d\n" % i for i in range(10)]
    ser = FakeSerial(lines, fail_after=10)  # 10 lines, then port "drops"
    sink = uart_logger.CsvSink(out, max_size=120)
    stop = threading.Event()

    clean, count = uart_logger.read_frames(ser, sink, "COM1", stop)
    sink.close()

    assert clean is False  # port dropped after 10 reads
    assert count == 10
    files = sorted(tmp_path.glob("log*.csv"))
    assert len(files) >= 2  # rotation happened
    assert files[0].name == "log.csv"
    total = 0
    for f in files:
        with open(f, encoding="utf-8") as fh:
            content = fh.read().splitlines()
        assert content[0] == "timestamp,port,frame"
        rows = list(csv.DictReader(content))
        total += len(rows)
        assert all(r["port"] == "COM1" for r in rows)
    assert total == 10  # no frames lost or duplicated across rotation


def test_run_with_max_size_rotates(tmp_path):
    out = str(tmp_path / "run.csv")
    ports = [FakeSerial([b"a\n"] * 10, fail_after=10)]

    def opener(port, baud, timeout):
        if not ports:
            raise FakeSerialError("gone")
        return ports.pop(0)

    logged = uart_logger.run("COM1", 115200, out, port_opener=opener,
                             sleep=lambda s: None, max_size=120,
                             max_retries=1, reconnect_delay=0)

    assert logged == 10
    files = sorted(tmp_path.glob("run*.csv"))
    assert len(files) >= 2
    assert all(f.stat().st_size > 0 for f in files)


def test_argparse_max_size_flag():
    args = uart_logger.parse_args(["--port", "COM1", "--max-size", "4096"])
    assert args.max_size == 4096
    args = uart_logger.parse_args(["--port", "COM1"])
    assert args.max_size == 0  # backward compatible default


def make_frame(sync, payload):
    """Build a valid fixed frame: [SYNC][payload...][XOR checksum]."""
    body = bytes([sync]) + bytes(payload)
    return body + bytes([uart_logger.xor_checksum(body)])


def test_parse_frame_spec():
    assert uart_logger.parse_frame_spec(["0xAA", "8"]) == (0xAA, 8)
    assert uart_logger.parse_frame_spec(["170", "8"]) == (0xAA, 8)
    assert uart_logger.parse_frame_spec(None) is None
    with pytest.raises(ValueError):
        uart_logger.parse_frame_spec(["0xAA", "1"])  # too short
    with pytest.raises(ValueError):
        uart_logger.parse_frame_spec(["999", "8"])  # sync out of range
    with pytest.raises(ValueError):
        uart_logger.parse_frame_spec(["zz", "8"])  # not a number


def test_validate_frame():
    good = make_frame(0xAA, b"\x01\x02")
    assert uart_logger.validate_frame(good, 0xAA, len(good)) is True
    bad_sync = bytes([0xBB]) + good[1:]
    assert uart_logger.validate_frame(bad_sync, 0xAA, len(good)) is False
    bad_crc = good[:-1] + bytes([good[-1] ^ 0xFF])
    assert uart_logger.validate_frame(bad_crc, 0xAA, len(good)) is False
    assert uart_logger.validate_frame(good[:-1], 0xAA, len(good)) is False


def test_frame_mode_logs_valid_frames_hex():
    stop = threading.Event()
    buf, writer = make_writer()
    # payloads picked so no payload byte and no checksum equals the 0xAA
    # sync byte — otherwise the resync scanner can glue a rejected frame's
    # tail to the next chunk into a phantom "valid" frame, which is correct
    # streaming behavior but makes for a confusing test
    good1 = make_frame(0xAA, b"\x01\x02\x04")
    good2 = make_frame(0xAA, b"\x04\x05\x06")
    bad_sync = bytes([0xBB]) + good1[1:]
    bad_crc = good1[:-1] + bytes([good1[-1] ^ 0xFF])
    ser = FakeSerial([], chunks=[good1, bad_sync, bad_crc, good2],
                     fail_after=4)
    stats = uart_logger.LinkStats()

    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           stats=stats, frame_spec=(0xAA, 5))

    assert clean is False  # port "dropped" after 4 reads
    assert count == 2  # only the valid frames logged
    # the wrong-sync frame is skipped as resync junk, not counted; only
    # the frame that survived sync but failed its checksum is an error
    assert stats.errors == 1
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert [r["frame"] for r in rows] == [good1.hex(), good2.hex()]


def test_frame_mode_drops_partial_frame_on_port_drop():
    # a short read is buffered, not an error; if the port then drops,
    # the incomplete frame is simply lost and the caller reconnects
    stop = threading.Event()
    buf, writer = make_writer()
    ser = FakeSerial([], chunks=[b"\xAA\x01"], fail_after=1)  # 2 of 5 bytes
    stats = uart_logger.LinkStats()

    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           stats=stats, frame_spec=(0xAA, 5))

    assert clean is False
    assert count == 0
    assert stats.errors == 0  # incomplete data is not a bad frame
    buf.seek(0)
    assert list(csv.DictReader(buf)) == []


def test_frame_mode_assembles_split_frames():
    # 1-byte reads: a frame split across many reads still assembles
    stop = threading.Event()
    buf, writer = make_writer()
    good = make_frame(0xAA, b"\x01\x02\x03")  # 5 bytes
    ser = FakeSerial([], chunks=[good], max_read=1)
    stats = uart_logger.LinkStats()

    stop_after_n_writes(stop, 1, buf)
    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           stats=stats, frame_spec=(0xAA, 5))

    assert (clean, count) == (True, 1)
    assert stats.errors == 0
    assert stats.bytes == 5  # every byte off the port still counted
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert rows[0]["frame"] == good.hex()


def test_frame_mode_skips_junk_before_sync():
    # garbage bytes (line noise, a truncated frame) ahead of the sync
    # byte must not desync the stream
    stop = threading.Event()
    buf, writer = make_writer()
    good = make_frame(0xAA, b"\x09\x08\x07")  # 5-byte frame
    ser = FakeSerial([], chunks=[b"\x00\xffnoise", good], max_read=2)
    stats = uart_logger.LinkStats()

    stop_after_n_writes(stop, 1, buf)
    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           stats=stats, frame_spec=(0xAA, 5))

    assert (clean, count) == (True, 1)
    assert stats.errors == 0
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert rows[0]["frame"] == good.hex()


def test_frame_mode_recovers_after_bad_checksum_short_reads():
    # corrupt frame mid-stream, everything arriving 1 byte at a time:
    # the bad frame is dropped and counted, valid ones still log
    stop = threading.Event()
    buf, writer = make_writer()
    good1 = make_frame(0xAA, b"\x01\x02\x03")
    bad = good1[:-1] + bytes([good1[-1] ^ 0xFF])
    good2 = make_frame(0xAA, b"\x04\x05\x06")
    ser = FakeSerial([], chunks=[good1, bad, good2], max_read=1)
    stats = uart_logger.LinkStats()

    stop_after_n_writes(stop, 2, buf)
    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           stats=stats, frame_spec=(0xAA, 5))

    assert clean is True
    assert count == 2
    assert stats.errors == 1
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert [r["frame"] for r in rows] == [good1.hex(), good2.hex()]


def test_frame_mode_works_without_stats():
    stop = threading.Event()
    buf, writer = make_writer()
    good = make_frame(0x7E, b"\x10")
    ser = FakeSerial([], chunks=[good], fail_after=1)

    clean, count = uart_logger.read_frames(ser, writer, "COM1", stop,
                                           frame_spec=(0x7E, 3))

    assert (clean, count) == (False, 1)
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    assert rows[0]["frame"] == good.hex()


def test_argparse_frame_flag():
    args = uart_logger.parse_args(
        ["--port", "COM1", "--frame", "0xAA", "8"])
    assert args.frame == ["0xAA", "8"]
    args = uart_logger.parse_args(["--port", "COM1"])
    assert args.frame is None  # backward compatible default


def test_main_rejects_bad_frame_spec(capsys):
    rc = uart_logger.main(["--port", "COM1", "--frame", "0xAA", "1"])
    assert rc == 2
    assert "error" in capsys.readouterr().err
