"""Bounded, cancellable subprocesses for local media tools.

Tool output is data. Callers can interpret stderr lines, but this module never
turns arbitrary tool output (which can contain private paths) into activity.
"""

from __future__ import annotations

import os
from pathlib import Path
from queue import Empty, Full, Queue
import signal
import subprocess
from threading import Event, Thread
import time
from typing import Callable

from .errors import TaskError


_MAX_STDOUT = 4 * 1024 * 1024
_MAX_LINE = 8192
_POLL_SECONDS = 0.1


def _stop_tree(process: subprocess.Popen, windows_job=None) -> None:
    """Stop this invocation's descendants, even after its parent has exited."""
    if os.name == "nt":
        # The job owns descendants independently of the leader's lifetime.
        # taskkill /T cannot find that tree once the leader has disappeared.
        assigned = windows_job is not None and windows_job.assigned
        if windows_job is not None:
            windows_job.close()
        # Assignment can fail while the leader is still suspended and outside
        # the job. It must be killed without ever being resumed.
        if not assigned and process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Waiting for the leader alone is insufficient: its children can ignore
        # SIGTERM and keep an output pipe open after the leader exits.
        until = time.monotonic() + 0.5
        while time.monotonic() < until:
            process.poll()  # Reap the leader promptly if it has already exited.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.025)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def run_command(
    argv: list[str],
    context,
    *,
    timeout: float = 3600,
    stdout_path: Path | None = None,
    on_line: Callable[[str], None] | None = None,
    cwd=None,
    env=None,
) -> str:
    """Run one tool without a shell, polling cancellation even during silence.

    Stderr lines are delivered on the calling thread and bounded to 8 KiB.
    Captured stdout is limited to 4 MiB; large binary output must use
    ``stdout_path``. A partial output file is left for the caller to discard.
    """
    if not argv or not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
        raise ValueError("argv must be a non-empty list of strings")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    context.check_cancel()
    sink = None
    process = None
    windows_job = None
    stopped = Event()
    # A fixed queue bounds memory even if a tool floods stderr. Readers apply
    # backpressure; cancellation can still stop them without waiting on a put.
    messages: Queue[tuple[str, bytes | None]] = Queue(maxsize=128)
    readers: list[Thread] = []
    output: list[bytes] = []
    output_size = 0

    def put(kind: str, data: bytes | None) -> bool:
        while not stopped.is_set():
            try:
                messages.put((kind, data), timeout=_POLL_SECONDS)
                return True
            except Full:
                continue
        return False

    def read_stdout(pipe) -> None:
        try:
            while not stopped.is_set():
                chunk = pipe.read1(65536)
                if not chunk:
                    break
                if not put("stdout", chunk):
                    break
        finally:
            put("stdout", None)

    def read_stderr(pipe) -> None:
        pending = b""
        try:
            while not stopped.is_set():
                chunk = pipe.read1(4096)
                if not chunk:
                    break
                if on_line is None:
                    continue
                pending += chunk
                while pending:
                    newline = pending.find(b"\n")
                    if 0 <= newline < _MAX_LINE:
                        line, pending = pending[:newline], pending[newline + 1 :]
                    elif len(pending) >= _MAX_LINE:
                        line, pending = pending[:_MAX_LINE], pending[_MAX_LINE:]
                    else:
                        break
                    if not put("stderr", line.rstrip(b"\r")):
                        return
            if pending:
                put("stderr", pending)
        finally:
            put("stderr", None)

    started = time.monotonic()
    try:
        if stdout_path is not None:
            try:
                sink = Path(stdout_path).open("wb")
            except OSError as exc:
                raise TaskError("OUTPUT_UNWRITABLE", "无法写入任务临时输出。", retryable=True) from exc
        if os.name == "nt":
            try:
                from ._windows_job import WindowsJob

                windows_job = WindowsJob()
            except Exception as exc:
                raise TaskError(
                    "PROCESS_CONTROL_UNAVAILABLE",
                    "无法建立 Windows 任务进程保护，工具尚未开始运行。",
                    retryable=False,
                ) from exc
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=sink if sink is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                cwd=cwd,
                env=env,
                start_new_session=os.name != "nt",
                creationflags=windows_job.creation_flags if windows_job is not None else 0,
            )
        except OSError as exc:
            raise TaskError("TOOL_UNAVAILABLE", "媒体工具不存在或无法启动。", retryable=False) from exc

        if windows_job is not None:
            try:
                windows_job.assign(process)
            except Exception as exc:
                raise TaskError(
                    "PROCESS_CONTROL_UNAVAILABLE",
                    "无法保护 Windows 工具进程，本次工具未运行。",
                    retryable=False,
                ) from exc
            context.check_cancel()
            try:
                windows_job.resume(process)
            except Exception as exc:
                raise TaskError(
                    "PROCESS_CONTROL_UNAVAILABLE",
                    "无法启动受保护的 Windows 工具进程。",
                    retryable=False,
                ) from exc

        assert process.stderr is not None
        readers.append(Thread(target=read_stderr, args=(process.stderr,), daemon=True))
        expected_eofs = 1
        if sink is None:
            assert process.stdout is not None
            readers.append(Thread(target=read_stdout, args=(process.stdout,), daemon=True))
            expected_eofs += 1
        for thread in readers:
            thread.start()
        received_eofs = 0
        while True:
            context.check_cancel()
            context.heartbeat()
            if time.monotonic() - started >= timeout:
                raise TaskError("PROCESS_TIMEOUT", "媒体工具超过执行时限，已停止本次处理。", retryable=True)
            # Bound work per tick so continuous output cannot starve cancel or
            # heartbeat checks. Queue backpressure takes care of faster writers.
            for index in range(64):
                try:
                    kind, chunk = messages.get(timeout=_POLL_SECONDS if index == 0 else 0)
                except Empty:
                    break
                if chunk is None:
                    received_eofs += 1
                elif kind == "stdout":
                    output_size += len(chunk)
                    if output_size > _MAX_STDOUT:
                        raise TaskError("PROCESS_OUTPUT_LIMIT", "媒体工具返回的数据超过允许大小。", retryable=False)
                    output.append(chunk)
                elif on_line is not None:
                    on_line(chunk.decode("utf-8", errors="replace"))
            if process.poll() is not None and received_eofs == expected_eofs:
                break
        # A cancellation arriving with process completion must still win before
        # the caller can publish a successful result.
        context.check_cancel()
        if process.returncode != 0:
            raise TaskError("PROCESS_FAILED", f"媒体工具执行失败（退出码 {process.returncode}）。", retryable=True)
        if sink is not None:
            sink.flush()
            os.fsync(sink.fileno())
        return b"".join(output).decode("utf-8", errors="replace") if sink is None else ""
    except BaseException:
        if process is not None:
            _stop_tree(process, windows_job)
        raise
    finally:
        # Release the job before joining readers or closing buffered pipes;
        # an orphan retaining an inherited pipe must not keep either blocked.
        if windows_job is not None:
            windows_job.close()
        stopped.set()
        for thread in readers:
            thread.join(timeout=1)
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if sink is not None:
            sink.close()
