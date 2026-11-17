"""Own one Windows tool process tree, including orphaned descendants.

The caller creates its normal ``subprocess.Popen`` with ``creation_flags``.
CREATE_SUSPENDED prevents the tool spawning children before job assignment;
psutil resumes it only after the assignment has succeeded. We reuse pywin32
and psutil instead of maintaining a second CreateProcess/stdio implementation.

https://learn.microsoft.com/windows/win32/procthread/job-objects
https://psutil.readthedocs.io/en/latest/#psutil.Process.resume
"""

from __future__ import annotations

import psutil
import win32api
import win32con
import win32job


class WindowsJob:
    creation_flags = win32con.CREATE_SUSPENDED | win32con.CREATE_NEW_PROCESS_GROUP

    def __init__(self):
        # No name or inheritable security attributes: only this runner owns the
        # handle, so closing it also covers Frameflow exiting unexpectedly.
        self.assigned = False
        self._handle = win32job.CreateJobObject(None, None)
        try:
            limits = win32job.QueryInformationJobObject(
                self._handle, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                self._handle, win32job.JobObjectExtendedLimitInformation, limits
            )
        except BaseException:
            self.close()
            raise

    def assign(self, process) -> None:
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
            False,
            process.pid,
        )
        try:
            win32job.AssignProcessToJobObject(self._handle, handle)
            self.assigned = True
        finally:
            handle.Close()

    def resume(self, process) -> None:
        # Popen retains the process handle, preventing PID reuse here. psutil's
        # supported Windows API resumes every thread of the suspended process.
        psutil.Process(process.pid).resume()

    def close(self) -> None:
        if self._handle is not None:
            handle, self._handle = self._handle, None
            # Closing the sole handle with KILL_ON_JOB_CLOSE terminates every
            # member, even when the original process has already exited.
            handle.Close()
