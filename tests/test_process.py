from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from frameflow.errors import TaskCancelled, TaskError
from frameflow.process import run_command


class Context:
    def __init__(self, cancel_after=None):
        self.started = time.monotonic()
        self.cancel_after = cancel_after
        self.heartbeats = 0
        self.activities = []

    def check_cancel(self):
        if self.cancel_after is not None and time.monotonic() - self.started >= self.cancel_after:
            raise TaskCancelled()

    def heartbeat(self):
        self.heartbeats += 1

    def activity(self, *args, **kwargs):
        self.activities.append((args, kwargs))


class ProcessTests(unittest.TestCase):
    def test_captures_stdout_and_does_not_publish_stderr(self):
        context = Context()
        lines = []
        result = run_command(
            [sys.executable, "-c", "import sys; print('ok'); print('private-tool-output', file=sys.stderr)"],
            context,
            on_line=lines.append,
            timeout=5,
        )
        self.assertEqual(result, "ok\n")
        self.assertEqual(lines, ["private-tool-output"])
        self.assertGreater(context.heartbeats, 0)
        self.assertEqual(context.activities, [])

    def test_binary_stdout_goes_to_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "audio.part"
            result = run_command(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(bytes(range(256))*100)"],
                Context(),
                stdout_path=output,
                timeout=5,
            )
            self.assertEqual(result, "")
            self.assertEqual(output.read_bytes(), bytes(range(256)) * 100)

    def test_silent_process_can_be_cancelled(self):
        context = Context(cancel_after=0.25)
        started = time.monotonic()
        with self.assertRaises(TaskCancelled):
            run_command([sys.executable, "-c", "import time; time.sleep(30)"], context, timeout=10)
        self.assertLess(time.monotonic() - started, 3)
        self.assertGreaterEqual(context.heartbeats, 2)

    def test_silent_process_timeout_is_explicit(self):
        with self.assertRaises(TaskError) as error:
            run_command([sys.executable, "-c", "import time; time.sleep(30)"], Context(), timeout=0.2)
        self.assertEqual(error.exception.code, "PROCESS_TIMEOUT")

    def test_nonzero_exit_and_missing_tool_are_distinct(self):
        with self.assertRaises(TaskError) as error:
            run_command([sys.executable, "-c", "raise SystemExit(7)"], Context(), timeout=5)
        self.assertEqual(error.exception.code, "PROCESS_FAILED")
        with self.assertRaises(TaskError) as error:
            run_command(["frameflow-test-nonexistent-command-7ee04b"], Context(), timeout=5)
        self.assertEqual(error.exception.code, "TOOL_UNAVAILABLE")

    def test_stdout_is_bounded(self):
        with self.assertRaises(TaskError) as error:
            run_command(
                [sys.executable, "-c", "import sys; sys.stdout.write('x'*(4*1024*1024+1))"],
                Context(),
                timeout=5,
            )
        self.assertEqual(error.exception.code, "PROCESS_OUTPUT_LIMIT")

    def test_long_stderr_lines_are_bounded_and_drained(self):
        lengths = []
        run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('x'*100000+'\\n')"],
            Context(),
            timeout=5,
            on_line=lambda line: lengths.append(len(line)),
        )
        self.assertEqual(sum(lengths), 100000)
        self.assertLessEqual(max(lengths), 8192)

    def test_argv_is_not_interpreted_by_a_shell(self):
        literal = "$(printf unsafe); & echo unsafe"
        result = run_command([sys.executable, "-c", "import sys; print(sys.argv[1])", literal], Context(), timeout=5)
        self.assertEqual(result.strip(), literal)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group assertion")
    def test_cancellation_stops_descendants_even_if_leader_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "child.pid"
            child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            parent = (
                "import pathlib,subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
            )
            with self.assertRaises(TaskCancelled):
                run_command([sys.executable, "-c", parent, str(pid_file), child], Context(cancel_after=0.4), timeout=10)
            self.assertTrue(pid_file.exists())
            pid = int(pid_file.read_text())
            deadline = time.monotonic() + 2
            alive = True
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
                    break
                # Containers can leave a dead orphan waiting for PID 1 to reap;
                # a zombie is not a still-running media worker.
                status = Path(f"/proc/{pid}/stat")
                if status.exists() and status.read_text().split(") ", 1)[1].startswith("Z"):
                    alive = False
                    break
                time.sleep(0.025)
            self.assertFalse(alive, "cancel left a running descendant")


if __name__ == "__main__":
    unittest.main()
