"""Windows-only, reversible YouTube block using the system hosts file.

The elevated helper installs a SYSTEM scheduled task *before* touching hosts.
That task contains its own PowerShell restore logic, so expiry still works if
the EXE is closed, moved, or deleted. No ActivityWatch data is touched.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from study_session import ALLOWED_MINUTES, StudySession


TASK_NAME = "ActivityWatchFocusGuard-Unblock"
START_MARKER = b"\r\n# BEGIN ActivityWatchFocusGuard managed block\r\n"
END_MARKER = b"# END ActivityWatchFocusGuard managed block\r\n"
YOUTUBE_HOSTS = (
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "kids.youtube.com", "studio.youtube.com", "tv.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com",
    "www.youtube-nocookie.com", "youtubei.googleapis.com", "youtube.googleapis.com",
)


def windows_directory() -> Path:
    if sys.platform == "win32":
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
            return Path(buffer.value)
    return Path(os.environ.get("SystemRoot", r"C:\Windows"))


def windows_hosts_path() -> Path:
    return windows_directory() / "System32" / "drivers" / "etc" / "hosts"


def powershell_path() -> Path:
    return windows_directory() / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


def block_content() -> bytes:
    lines = [f"{ip} {host}\r\n" for host in YOUTUBE_HOSTS
             for ip in ("0.0.0.0", "::1")]
    return START_MARKER + "".join(lines).encode("ascii") + END_MARKER


def remove_managed_content(data: bytes) -> bytes:
    """Remove our exact marked region, preserving all other host-file bytes."""
    start = data.find(START_MARKER)
    end = data.find(END_MARKER)
    if start < 0 and end < 0:
        return data
    if start < 0 or end < start + len(START_MARKER):
        raise ValueError("Invalid managed hosts markers; manual inspection required")
    if data.find(START_MARKER, start + len(START_MARKER)) >= 0:
        raise ValueError("Duplicate managed hosts blocks; manual inspection required")
    return data[:start] + data[end + len(END_MARKER):]


def restore_powershell_script() -> str:
    """Self-contained SYSTEM task action; independent from the Python EXE."""
    return r'''
$ErrorActionPreference = 'Stop'
$path = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$encoding = [System.Text.Encoding]::GetEncoding(28591)
$text = $encoding.GetString([System.IO.File]::ReadAllBytes($path))
$begin = "`r`n# BEGIN ActivityWatchFocusGuard managed block`r`n"
$end = "# END ActivityWatchFocusGuard managed block`r`n"
$first = $text.IndexOf($begin, [System.StringComparison]::Ordinal)
$last = $text.IndexOf($end, [System.StringComparison]::Ordinal)
if ($first -ge 0 -and $last -ge ($first + $begin.Length)) {
    $clean = $text.Substring(0, $first) + $text.Substring($last + $end.Length)
    [System.IO.File]::WriteAllBytes($path, $encoding.GetBytes($clean))
    & "$env:SystemRoot\System32\ipconfig.exe" /flushdns | Out-Null
}
'''.strip()


def encoded_powershell(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def is_admin() -> bool:
    return sys.platform == "win32" and bool(ctypes.windll.shell32.IsUserAnAdmin())


@contextlib.contextmanager
def elevated_operation_mutex():
    """Serialize elevated helpers to avoid double-click races on hosts/tasks."""
    if sys.platform != "win32":
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateMutexW(None, True, "Local\\ActivityWatchFocusGuard-ElevatedOperation")
    if not handle:
        raise OSError("Cannot create exclusive system operation mutex")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        raise RuntimeError("A Focus Guard system operation is already running")
    try:
        yield
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def run_powershell(script: str) -> None:
    if not is_admin():
        raise PermissionError("Administrator permissions are required")
    result = subprocess.run(
        [str(powershell_path()), "-NoProfile", "-NonInteractive", "-EncodedCommand",
         encoded_powershell(script)],
        capture_output=True, text=True, errors="replace", timeout=40,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        explanation = (result.stderr or result.stdout).strip()[-900:]
        raise RuntimeError(f"Windows Task Scheduler error: {explanation}")


class WindowsHostsBlocker:
    def __init__(self, hosts_path: Path | None = None):
        self.path = hosts_path if hosts_path is not None else windows_hosts_path()

    def is_blocked(self) -> bool:
        return START_MARKER in self.path.read_bytes()

    def restore(self) -> None:
        original = self.path.read_bytes()
        cleaned = remove_managed_content(original)
        if cleaned != original:
            self.path.write_bytes(cleaned)
            if self.path.read_bytes() != cleaned:
                raise OSError("Could not verify restoration of the hosts file")
            self.flush_dns()

    def apply(self) -> None:
        existing = self.path.read_bytes()
        if START_MARKER in existing or END_MARKER in existing:
            raise ValueError("A previous Focus Guard block must be restored first")
        expected = existing + block_content()
        self.path.write_bytes(expected)
        if self.path.read_bytes() != expected:
            raise OSError("Could not verify Windows hosts update")
        self.flush_dns()

    def flush_dns(self) -> None:
        if sys.platform != "win32":
            return
        try:
            subprocess.run([str(windows_directory() / "System32" / "ipconfig.exe"), "/flushdns"],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass  # A DNS-cache failure must never discard the rollback task.

    def schedule_restore(self, deadline: datetime) -> None:
        if deadline.tzinfo is None:
            raise ValueError("Restore deadline must have a timezone")
        utc_deadline = deadline.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        command = encoded_powershell(restore_powershell_script())
        # All interpolated strings are fixed or generated from ISO dates/Base64.
        script = f"""
$ErrorActionPreference = 'Stop'
$when = [DateTimeOffset]::Parse('{utc_deadline}', [Globalization.CultureInfo]::InvariantCulture).LocalDateTime
$action = New-ScheduledTaskAction -Execute '{powershell_path()}' -Argument '-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {command}'
$trigger = New-ScheduledTaskTrigger -Once -At $when
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $action -Trigger $trigger -Settings $settings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
"""
        run_powershell(script)

    def remove_restore_task(self) -> None:
        script = f"""
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName '{TASK_NAME}' -ErrorAction SilentlyContinue
if ($null -ne $task) {{ Unregister-ScheduledTask -TaskName '{TASK_NAME}' -Confirm:$false }}
"""
        run_powershell(script)


def start_system_session(session: StudySession, minutes: int,
                         blocker: WindowsHostsBlocker) -> dict:
    if minutes not in ALLOWED_MINUTES or type(minutes) is not int:
        raise ValueError("Only 25, 40 and 60 minute sessions are supported")
    if session.status()["active"]:
        raise ValueError("A study session is already active")
    if not is_admin():
        raise PermissionError("Administrator permissions are required")

    # Remove only our own *expired* block, if a previous recovery was delayed.
    blocker.restore()
    now = datetime.now(timezone.utc)
    blocker.schedule_restore(now + timedelta(minutes=minutes))
    try:
        blocker.apply()
        return session.start(minutes, now)
    except Exception:
        try:
            blocker.restore()
        except Exception:
            # Leave the SYSTEM recovery task intact if manual cleanup failed.
            raise RuntimeError("Could not undo hosts changes; scheduled recovery remains installed")
        else:
            blocker.remove_restore_task()
        raise


def restore_system_access(session: StudySession, blocker: WindowsHostsBlocker) -> None:
    if not is_admin():
        raise PermissionError("Administrator permissions are required")
    blocker.restore()
    try:
        blocker.remove_restore_task()
    finally:
        # Access was restored even if removing the (harmless) task failed.
        session.path.unlink(missing_ok=True)
