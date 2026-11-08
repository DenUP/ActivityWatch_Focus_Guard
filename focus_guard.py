"""Focus Guard: a tray-only Windows app with a local browser dashboard.

Study timers and system blocking are managed here, with an independent Windows
scheduled task restoring access. The tray icon is the entire desktop UI; no
Tkinter window is ever created, so nothing appears in the Windows taskbar.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from study_session import StudySession, session_path
from system_block import (WindowsHostsBlocker, elevated_operation_mutex,
                          is_admin, restore_system_access, start_system_session)
from web_dashboard import HOST, PORT, FocusService, create_dashboard_server


def notify_windows(message: str, error: bool = False) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "ActivityWatch Focus Guard",
                                          0x10 if error else 0x40)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def launch_elevated(*arguments: str) -> None:
    if sys.platform != "win32":
        raise OSError("System blocking is available only on Windows")
    params = list(arguments)
    if not getattr(sys, "frozen", False):
        params.insert(0, str(Path(__file__).resolve()))
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", sys.executable, subprocess.list2cmdline(params), None, 1
    )
    if result <= 32:
        raise PermissionError("Windows administrator permission was declined")


def execute_elevated_helper(arguments: list[str]) -> int:
    try:
        if not is_admin():
            raise PermissionError("Administrator permission was not granted")
        with elevated_operation_mutex():
            session = StudySession(session_path())
            blocker = WindowsHostsBlocker()
            if arguments[0] == "--system-start":
                if len(arguments) != 2:
                    raise ValueError("Expected study duration")
                start_system_session(session, int(arguments[1]), blocker)
            elif arguments[0] == "--system-restore":
                if len(arguments) != 1:
                    raise ValueError("Unexpected restore parameters")
                restore_system_access(session, blocker)
            else:
                raise ValueError("Unknown elevated action")
        return 0
    except (OSError, RuntimeError, ValueError, PermissionError) as error:
        notify_windows(f"Не удалось изменить блокировку: {error}", error=True)
        return 1


def acquire_singleton() -> tuple[object, object] | None:
    """Prevent duplicate servers and icons for the same interactive session."""
    if sys.platform != "win32":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.ReleaseMutex.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    handle = kernel.CreateMutexW(None, True, "Local\\ActivityWatchFocusGuard")
    if not handle:
        raise OSError("Cannot create Focus Guard application mutex")
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        notify_windows("Focus Guard уже работает. Открой его из системного трея Windows.")
        return None
    return kernel, handle


def make_tray_icon(open_dashboard):
    import pystray
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (64, 64), "#102a35")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 6, 58, 58), radius=15, fill="#20b98c")
    draw.ellipse((17, 17, 47, 47), outline="#0e3241", width=5)
    draw.arc((23, 23, 41, 41), 215, 320, fill="white", width=4)
    draw.line(((25, 34), (31, 39), (41, 26)), fill="white", width=4, joint="curve")

    return pystray.Icon(
        "ActivityWatchFocusGuard", image, "Focus Guard — учебный режим",
        menu=pystray.Menu(
            pystray.MenuItem("Открыть Focus Guard", lambda _icon, _item: open_dashboard(),
                            default=True),
            pystray.MenuItem("Выход", lambda icon, _item: icon.stop()),
        ),
    )


def run_app(open_on_start: bool = True) -> int:
    singleton = acquire_singleton()
    if sys.platform == "win32" and singleton is None:
        return 0
    server = None
    try:
        service = FocusService(StudySession(session_path()), WindowsHostsBlocker(),
                               launch_elevated)
        asset_root = Path(__file__).resolve().parent / "web"
        server = create_dashboard_server(service, asset_root)
        worker = threading.Thread(target=server.serve_forever,
                                  name="FocusGuardLocalDashboard", daemon=True)
        worker.start()

        url = f"http://{HOST}:{PORT}/#token={service.access_token}"

        def open_dashboard() -> None:
            webbrowser.open_new_tab(url)

        try:
            icon = make_tray_icon(open_dashboard)
        except (ImportError, OSError) as error:
            raise RuntimeError(f"System tray could not initialize: {error}") from error

        try:
            if open_on_start:
                icon.run(setup=lambda _icon: open_dashboard())
            else:
                icon.run()
        finally:
            icon.stop()
        return 0
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if singleton is not None:
            kernel, handle = singleton
            kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in {"--system-start", "--system-restore"}:
        return execute_elevated_helper(args)
    if args not in ([], ["--tray-only"]):
        notify_windows("Неизвестные параметры запуска.", error=True)
        return 1
    try:
        return run_app(open_on_start=args != ["--tray-only"])
    except (OSError, RuntimeError, ValueError) as error:
        notify_windows(f"Focus Guard не удалось запустить: {error}", error=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
