"""Bounded remote log transfer; all content selection runs on dispatcher Python."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SAFE_PATH = re.compile(r"^/mnt/share/ci_tests/[A-Za-z0-9_-]+/(?:node[0-9]+/)?run\.log$")
METRIC = re.compile(r"tps\b|elapsed_time_per_step|\bstep:\s*[1-9]|\bloss:", re.I)
LOSS = re.compile(r"\bloss\s*[:=]", re.I)
ERROR = re.compile(
    r"Traceback|Error:|Exception:|RuntimeError|AssertionError|FAILED|fatal", re.I
)
EXCEPTION = re.compile(r"\b[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception):")
RANK0 = re.compile(r"rank0\b|rank[_= ]0\b|global_rank_0", re.I)
MAX_REMOTE_BYTES = 131072


def remote_excerpt(
    host: str, path: str, *, approved_hosts: set[str], runner=subprocess.run
) -> str:
    """Retrieve only the last 128 KiB from approved execution hosts."""
    if host not in approved_hosts or not SAFE_PATH.fullmatch(path):
        raise ValueError("untrusted execution log locator")
    command = "tail -c 131072 -- " + shlex.quote(path)
    result = runner(
        __import__("ci_core.transport", fromlist=["command"]).command(host, command),
        capture_output=True,
        timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"remote log unavailable ({host}, rc={result.returncode})")
    if len(result.stdout) > MAX_REMOTE_BYTES:
        raise ValueError("remote log exceeds bounded transfer")
    return result.stdout.decode("utf-8", errors="replace")


def clean(line: str) -> str:
    return "".join(
        ch if (ord(ch) >= 32 and ord(ch) != 127) else " " for ch in ESCAPE.sub("", line)
    )[:180]


def select_lines(data: str, exit_code: int, *, require_loss: bool = False) -> list[str]:
    """Uniform dispatcher-side selection, never executed on training machines."""
    lines = [ESCAPE.sub("", line) for line in data.splitlines() if line.strip()]

    def excerpt(line, pattern):
        match = pattern.search(line)
        if match and match.start() > 110:
            line = "... " + line[max(0, match.start() - 50) :]
        return clean(line)

    if exit_code == 0:
        losses = [line for line in lines if LOSS.search(line)]
        if losses:
            selected = [line for line in losses if RANK0.search(line)] or losses
            return [excerpt(line, LOSS) for line in selected[-20:]]
        if require_loss:
            raise ValueError(
                "training completed without LOSS evidence in the bounded log"
            )
        metric = [line for line in lines if METRIC.search(line) and RANK0.search(line)]
        if not metric:
            metric = [line for line in lines if METRIC.search(line)]
        return [clean(line) for line in (metric[-20:] or lines[-20:])] or [
            "Execution finished without log content"
        ]
    matches = [i for i, line in enumerate(lines) if ERROR.search(line)]
    if matches:
        causes = []
        seen = set()
        for i, line in enumerate(lines):
            exception = EXCEPTION.search(line)
            if exception and line[exception.start() :] not in seen:
                seen.add(line[exception.start() :])
                causes.append(i)
        selected = []
        for i in causes[:2] or matches[-2:]:
            selected.extend(lines[max(0, i - 2) : min(len(lines), i + 3)])
        selected += lines[-8:]
        selected = list(dict.fromkeys(selected))
        return [excerpt(line, ERROR) for line in selected[:20]]
    return [clean(line) for line in lines[-20:]] or [
        f"Process exited with {exit_code}, no diagnostics"
    ]


def select_remote(
    host: str,
    path: str,
    exit_code: int,
    *,
    approved_hosts: set[str],
    runner=subprocess.run,
    require_loss: bool = False,
) -> list[str]:
    return select_lines(
        remote_excerpt(host, path, approved_hosts=approved_hosts, runner=runner),
        exit_code,
        require_loss=require_loss,
    )


def metadata(log: Path) -> dict[str, str]:
    """Read trusted RESULT_HOST/RESULT_LOG emitted by pinned pipeline snapshot."""
    found = {}
    with log.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("RESULT_HOST: "):
                found["host"] = line.partition(": ")[2].strip()
            elif line.startswith("RESULT_LOG: "):
                found["path"] = line.partition(": ")[2].strip()
    if len(found) != 2:
        raise ValueError("missing remote log metadata")
    return found


_SENSITIVE = re.compile(
    r"(?i)(?:authorization\s*[:=]|\bbearer\s+|(?:password|passwd|token|secret|private[_-]?key|access[_-]?key|api[_-]?key)\s*[:=]|(?:gh[pousr]_|github_pat_|AKIA)[A-Za-z0-9_]+|-----BEGIN [A-Z ]*PRIVATE KEY-----|[?&](?:sig|signature|x-amz-signature)=)"
)


def sanitized_lines(lines):
    """Remove credential-bearing records, including multiline PEMs, before slicing."""
    in_key = False
    for raw in lines:
        if not isinstance(raw, str):
            raise ValueError("invalid diagnostic type")
        for text in raw.splitlines():
            text = "".join(c if c.isprintable() else " " for c in ESCAPE.sub("", text))
            if "-----BEGIN " in text and "PRIVATE KEY-----" in text:
                in_key = True
            if in_key:
                if "-----END " in text and "PRIVATE KEY-----" in text:
                    in_key = False
                yield "[REDACTED]"
            else:
                yield "[REDACTED]" if _SENSITIVE.search(text) else text


def yellow_lines(lines):
    """Ten diagnostic lines and 2 KiB total, including separators."""
    out = []
    left = 2048
    for text in sanitized_lines(lines):
        if not text:
            continue
        remain = left - int(bool(out))
        text = (
            text[:180]
            .encode("utf-8", errors="replace")[: max(remain, 0)]
            .decode("utf-8", errors="ignore")
        )
        if not text:
            break
        out.append(text)
        left = remain - len(text.encode())
        if len(out) == 10 or left <= 1:
            break
    return out


BLUE_BYTES = 4 * 1024 * 1024


def blue_diagnostic(raw: bytes, *, limit=BLUE_BYTES):
    """Bound UTF-8 bytes, not logical lines. Never emit the interior of a PEM."""
    import io

    if not 0 < limit <= BLUE_BYTES:
        raise ValueError("invalid Blue log budget")
    text = raw[: limit + 1].decode("utf-8", "replace")
    sink = bytearray()
    in_key = False
    truncated = len(raw) > limit
    for record in io.StringIO(text, newline=None):
        eol = record.endswith("\n")
        line = ESCAPE.sub("", record.removesuffix("\n"))
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            in_key = True
        hide = in_key or _SENSITIVE.search(line)
        if "-----END " in line and "PRIVATE KEY-----" in line:
            in_key = False
        if hide:
            line = "[REDACTED]"
        elif not line.isprintable():
            line = "".join(ch if ch.isprintable() else " " for ch in line)
        chunk = (line + ("\n" if eol else "")).encode()
        room = limit - len(sink)
        if len(chunk) > room:
            sink.extend(chunk[:room].decode("utf-8", "ignore").encode())
            truncated = True
            break
        sink.extend(chunk)
    return bytes(sink), truncated


def blue_remote(host, path, approved, *, runner=subprocess.run):
    if host not in approved or not SAFE_PATH.fullmatch(path):
        raise ValueError("untrusted log location")
    from ci_core.transport import command

    r = runner(
        command(host, f"head -c {BLUE_BYTES + 1} -- " + shlex.quote(path)),
        capture_output=True,
        timeout=60,
        check=True,
    )
    if len(r.stdout) > BLUE_BYTES + 1:
        raise ValueError("oversized internal log sample")
    return blue_diagnostic(r.stdout)
