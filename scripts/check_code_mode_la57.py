#!/usr/bin/env python3
"""Offline Linux regression probe for code-mode host LA57 crashes (#43820).

Runs a supplied host binary, never an app-server or the model API. The host child
has no enabled tools, uses a temporary HOME, and inherits the existing RLIMIT_AS.
A runner-wide alarm bounds hangs; protocol messages, not sleeps, synchronize work.
"""

import argparse
import json
import os
from pathlib import Path
import resource
import signal
import struct
import subprocess
import sys
import tempfile


CASES = [
    ("arithmetic", "text(1 + 1)", 2),
    ("array_callback", "text([1, 2, 3].map(x => x + 1))", [2, 3, 4]),
    ("ordinary_call", "function add(a, b) { return a + b; } text(add(20, 22))", 42),
    (
        "async_callback",
        "text(await Promise.all([1, 2, 3].map(async x => x * 2)))",
        [2, 4, 6],
    ),
]


def send(host, message):
    data = json.dumps(message).encode()
    host.stdin.write(struct.pack("<I", len(data)) + data)
    host.stdin.flush()


def read_exact(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("code-mode host closed stdout")
        chunks.extend(chunk)
    return bytes(chunks)


def receive(host):
    size = struct.unpack("<I", read_exact(host.stdout, 4))[0]
    if size > 1024 * 1024:
        raise ValueError("unexpectedly large response")
    return json.loads(read_exact(host.stdout, size))


def response(host, kind, request_id=None):
    while True:
        message = receive(host)
        if message["type"] == kind and (
            request_id is None or message.get("id") == request_id
        ):
            if "result" not in message:
                return message
            result = message["result"]
            if result["status"] != "ok":
                raise RuntimeError(result)
            return result["value"]
        if message["type"] not in ("operation/response", "cell/closed"):
            raise RuntimeError(f"unexpected protocol message: {message}")


def request(host, request_id, method, **fields):
    send(
        host,
        {
            "type": "operation/request",
            "id": request_id,
            "request": {"method": method, "sessionId": "la57-probe", **fields},
        },
    )


def child_limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def high_anonymous_mappings(pid):
    found = []
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split()
        start = int(fields[0].split("-")[0], 16)
        if 1 << 48 <= start < 1 << 56 and len(fields) == 5:
            found.append({"range": fields[0], "permissions": fields[1]})
    return found


def run(binary):
    with tempfile.TemporaryDirectory(prefix="codex-la57-probe-") as home:
        env = os.environ.copy()
        env.update(HOME=home, CODEX_HOME=home, TMPDIR=home)
        with tempfile.TemporaryFile() as stderr:
            host = subprocess.Popen(
                [str(binary), "--listen", "stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                env=env,
                bufsize=0,
                preexec_fn=child_limits,
            )
            report = {"pid": host.pid, "cases": []}
            try:
                send(
                    host,
                    {
                        "type": "connection/hello",
                        "supportedVersions": [1],
                        "requiredCapabilities": [],
                        "optionalCapabilities": [],
                    },
                )
                response(host, "connection/ready")
                request(host, 1, "session/open")
                response(host, "operation/response", 1)
                request_id = 2
                for name, source, expected in CASES:
                    report["active_case"] = name
                    request(
                        host,
                        request_id,
                        "session/execute",
                        request={
                            "tool_call_id": name,
                            "enabled_tools": [],
                            "source": source,
                            "yield_time_ms": 1000,
                            "max_output_tokens": 256,
                        },
                    )
                    value = response(host, "execute/initialResponse", request_id)
                    content = []
                    while "Yielded" in value:
                        yielded = value["Yielded"]
                        content.extend(yielded["content_items"])
                        request_id += 1
                        request(
                            host,
                            request_id,
                            "session/wait",
                            request={
                                "cell_id": yielded["cell_id"],
                                "yield_time_ms": 1000,
                            },
                        )
                        completed = response(host, "operation/response", request_id)
                        outcome = completed["outcome"]
                        value = outcome.get("LiveCell", outcome.get("MissingCell"))
                    if (
                        "Result" not in value
                        or value["Result"].get("error_text") is not None
                    ):
                        raise RuntimeError(value)
                    content.extend(value["Result"]["content_items"])
                    actual = [json.loads(item["text"]) for item in content]
                    if actual != [expected]:
                        raise AssertionError({"expected": [expected], "actual": actual})
                    report["cases"].append({"name": name, "output": actual[0]})
                    request_id += 1
                report["high_anonymous_mappings"] = high_anonymous_mappings(host.pid)
                request(host, request_id, "session/shutdown")
                response(host, "operation/response", request_id)
                host.stdin.close()
                report["exit_code"] = host.wait()
                if report["exit_code"]:
                    raise RuntimeError("host exited unsuccessfully after shutdown")
                report["passed"] = True
            except Exception as error:
                report["passed"] = False
                report["error"] = str(error)
                if isinstance(error, EOFError):
                    report["exit_code"] = host.wait()
            finally:
                if host.poll() is None:
                    host.kill()
                    host.wait()
                report.setdefault("exit_code", host.returncode)
                stderr.seek(0)
                report["stderr_tail"] = stderr.read().decode(errors="replace")[-2000:]
                host.stdout.close()
                if not host.stdin.closed:
                    host.stdin.close()
            return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--timeout", type=int, default=60, help="whole-probe runner deadline in seconds"
    )
    args = parser.parse_args()
    if args.runs < 1 or args.timeout < 1:
        parser.error("--runs and --timeout must be positive")
    binary = args.binary.resolve(strict=True)

    def deadline(_signum, _frame):
        raise TimeoutError("whole-probe deadline exceeded")

    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(args.timeout)
    result = {
        "binary": str(binary),
        "kernel": os.uname().release,
        "rlimit_as": resource.getrlimit(resource.RLIMIT_AS),
        "runs": [],
    }
    for _ in range(args.runs):
        report = run(binary)
        result["runs"].append(report)
        if not report["passed"]:
            break
    signal.alarm(0)
    print(json.dumps(result, indent=2))
    return 0 if all(item["passed"] for item in result["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
