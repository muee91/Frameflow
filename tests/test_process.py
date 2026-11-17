from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Thread
import time
import unittest
from unittest.mock import patch

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
        # Captured bytes preserve the child platform's native text newline.
        self.assertEqual(result, "ok" + os.linesep)
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

    @unittest.skipUnless(os.name == "posix", "POSIX group signalling")
    def test_permission_denied_during_group_probe_does_not_mask_timeout(self):
        original = os.killpg
        observed = []

        def transient_probe(pgid, signum):
            if signum == 0 and not observed:
                observed.append(pgid)
                raise PermissionError("macOS group probe temporarily denied")
            return original(pgid, signum)

        with patch("frameflow.process.os.killpg", side_effect=transient_probe):
            with self.assertRaises(TaskError) as error:
                run_command([sys.executable, "-c", "import time; time.sleep(30)"],
                            Context(), timeout=0.2)
        self.assertEqual(error.exception.code, "PROCESS_TIMEOUT")
        self.assertTrue(observed)

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

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_windows_cancellation_stops_orphan_retaining_output_pipes(self):
        import psutil

        class ManualContext(Context):
            def __init__(self):
                super().__init__()
                self.cancel_requested = Event()

            def check_cancel(self):
                if self.cancel_requested.is_set():
                    raise TaskCancelled()

        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary).resolve() / "child.pid"
            # The child is silent and inherits both pipes. Its parent exits
            # before cancellation, so taskkill /PID parent /T cannot own it.
            child_code = "import time; time.sleep(60)"
            parent_code = (
                "import pathlib,subprocess,sys; "
                "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
                "pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
            )
            context = ManualContext()
            outcomes, leaders = [], []
            original_popen = subprocess.Popen

            def capture_leader(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                leaders.append(process)
                return process

            def invoke():
                try:
                    run_command(
                        [sys.executable, "-c", parent_code, str(pid_file), child_code],
                        context,
                        timeout=15,
                    )
                except BaseException as error:
                    outcomes.append(error)
                else:
                    outcomes.append(None)

            runner = Thread(target=invoke, daemon=True)
            child = None
            try:
                with patch("frameflow.process.subprocess.Popen", side_effect=capture_leader):
                    runner.start()
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if leaders and leaders[0].poll() is not None:
                            break
                        if not runner.is_alive():
                            self.fail(f"runner exited before cancellation: {outcomes}")
                        time.sleep(0.025)
                    self.assertTrue(leaders, "tool leader did not launch")
                    self.assertEqual(leaders[0].poll(), 0, "leader must exit before cancellation")
                    self.assertTrue(pid_file.exists(), "leader did not launch its child")
                    child = psutil.Process(int(pid_file.read_text()))
                    self.assertTrue(child.is_running(), "fixture child exited too early")
                    self.assertTrue(runner.is_alive(), "inherited pipes must keep the runner waiting")
                    cancelled_at = time.monotonic()
                    context.cancel_requested.set()
                    runner.join(timeout=5)
                    self.assertFalse(runner.is_alive(), "cancel blocked on an orphan's output pipe")
                    self.assertLess(time.monotonic() - cancelled_at, 3)
                    self.assertEqual(len(outcomes), 1)
                    self.assertIsInstance(outcomes[0], TaskCancelled)
                    child.wait(timeout=3)
                    self.assertFalse(child.is_running(), "cancel left the orphan running")
            finally:
                # Bound a failing regression as well: explicitly kill our known
                # fixture processes, releasing readers even in a broken runner.
                context.cancel_requested.set()
                if child is None and pid_file.exists():
                    try:
                        child = psutil.Process(int(pid_file.read_text()))
                    except psutil.NoSuchProcess:
                        pass
                if child is not None:
                    try:
                        child.kill()
                        child.wait(timeout=3)
                    except psutil.NoSuchProcess:
                        pass
                for leader in leaders:
                    if leader.poll() is None:
                        leader.kill()
                        leader.wait(timeout=3)
                runner.join(timeout=5)

    @unittest.skipUnless(os.name == "nt", "Windows suspended-start integration")
    def test_windows_assignment_failure_never_executes_tool(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary).resolve() / "executed.txt"
            processes = []
            original_popen = subprocess.Popen

            def capture_process(*args, **kwargs):
                process = original_popen(*args, **kwargs)
                processes.append(process)
                return process

            with (
                patch("frameflow.process.subprocess.Popen", side_effect=capture_process),
                patch("win32job.AssignProcessToJobObject", side_effect=OSError("test assignment failure")),
                self.assertRaises(TaskError) as error,
            ):
                run_command(
                    [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')", str(marker)],
                    Context(),
                    timeout=5,
                )
            self.assertEqual(error.exception.code, "PROCESS_CONTROL_UNAVAILABLE")
            self.assertFalse(marker.exists(), "unprotected tool started before job assignment")
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].poll(), "failed startup left a suspended process")


if __name__ == "__main__":
    unittest.main()
