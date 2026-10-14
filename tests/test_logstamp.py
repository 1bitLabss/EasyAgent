"""Server log lines carry a UTC timestamp."""

import io

from easyagent.logstamp import StampedStream


def test_each_server_log_line_starts_with_a_utc_timestamp():
    buf = io.StringIO()
    stream = StampedStream(buf)
    stream.write("heuristic reconnect: Model not answering, retrying (attempt 10). Timed out calling the model\n")
    stream.write("model call: purpose=check ")
    stream.write("prompt_tokens=1\n")
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 2
    for line in lines:
        stamp, _rest = line.split(" ", 1)
        assert len(stamp) == 20
        assert stamp[4] == "-" and stamp[10] == "T" and stamp.endswith("Z")
    assert lines[0].endswith("heuristic reconnect: Model not answering, retrying (attempt 10). Timed out calling the model")
    assert lines[1].endswith("model call: purpose=check prompt_tokens=1")
