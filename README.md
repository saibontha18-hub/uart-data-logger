# uart-data-logger

A small Python CLI tool that reads newline-terminated frames from a serial port, stamps each one with a UTC timestamp, and appends them to a CSV file. If the port drops or fails to open, it waits and reconnects automatically (configurable delay and retry limit). Ctrl-C stops logging cleanly.

## Prerequisites

- Python 3.9+
- A serial port (USB-UART adapter, dev board, or a virtual port such as `socat`-created PTYs for testing)
- Install dependencies: `pip install -r requirements.txt`

## Usage

```sh
# basic: log /dev/ttyUSB0 at 115200 baud to uart_log.csv
python uart_logger.py --port /dev/ttyUSB0

# custom baud rate, output file, and reconnect behavior
python uart_logger.py --port /dev/ttyUSB0 --baud 9600 \
    --output logs/run1.csv --reconnect-delay 5 --max-retries 10
```

CSV columns: `timestamp` (ISO-8601 UTC), `port`, `frame`.

Example output:

```csv
timestamp,port,frame
2026-10-02T08:00:01.123456+00:00,/dev/ttyUSB0,temp=21.5
2026-10-02T08:00:02.031209+00:00,/dev/ttyUSB0,temp=21.6
```

## Tests

The test suite uses a fake serial port, so no hardware is needed:

```sh
python -m pytest tests/ -v
```

## Files

- `uart_logger.py` — the logger (argparse CLI, reconnect loop, CSV writer)
- `requirements.txt` — `pyserial`, `pytest`
- `tests/test_uart_logger.py` — tests with a `FakeSerial` stub covering logging, reconnects, retry limits, and CLI defaults

## License

MIT
