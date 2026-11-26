"""Terminal input and output for the command line."""

import asyncio
from contextlib import ExitStack
import os
import select
from stat import S_ISFIFO
import sys
from threading import Thread
import webbrowser


def _open_verification_page(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass  # The terminal already has the manual instructions.


def _is_pty_master(fd: int) -> bool:
    if sys.platform == "linux":
        # Linux assigns major 2 to BSD PTY masters and (5, 2) to the UNIX98 clone device.
        device = os.fstat(fd).st_rdev
        return os.major(device) == 2 or device == os.makedev(5, 2)
    if sys.platform == "darwin":
        from errno import ENOTTY
        from fcntl import ioctl

        # TIOCPTYGNAME returns the replica's name on a PTY master.
        try:
            ioctl(fd, 0x40807453, bytes(128))
        except OSError as error:
            if error.errno != ENOTTY:
                raise
        else:
            return True
    return False


def _posix_output_blocked(fd: int) -> bool:
    from errno import ENOTTY
    import termios

    try:
        foreground = os.tcgetpgrp(fd)
    except OSError as error:
        # A stream outside the controlling terminal is unaffected by this job's terminal discipline.
        return error.errno != ENOTTY
    if foreground == os.getpgrp():
        return False
    try:
        return bool(termios.tcgetattr(fd)[3] & termios.TOSTOP)
    except termios.error:
        return True


def _print_status(message: str, *, file=None, flush: bool = False) -> bool:
    stream = sys.stdout if file is None else file
    if stream is None:
        return False
    try:
        if stream.closed or not stream.writable():
            return False
        try:
            fd = stream.fileno()
        except (OSError, ValueError):
            pass
        else:
            if sys.platform != "win32" and _posix_output_blocked(fd):
                return False
            if (stream is sys.__stderr__ or stream is sys.__stdout__) and S_ISFIFO(os.fstat(fd).st_mode):
                # Leave no pending output for the interpreter to flush if the reader went away.
                stream.flush()
                data = (message + os.linesep).encode(stream.encoding, stream.errors)
                return os.write(fd, data) == len(data)
        print(message, file=stream, flush=flush)
        return True
    except (OSError, ValueError):
        return False


def _is_terminal(stream, *, writable: bool = False) -> bool:
    if stream is None:
        return False
    try:
        if writable and not stream.writable():
            return False
        fd = stream.fileno()
        if sys.platform == "win32":
            from ctypes import byref, c_void_p, windll
            from ctypes.wintypes import DWORD
            from msvcrt import get_osfhandle

            handle = c_void_p(get_osfhandle(fd))
            value = byref(DWORD())
            if writable:
                # An empty console write checks the handle type and access without displaying text.
                return bool(windll.kernel32.WriteConsoleW(handle, "", 0, value, None))
            # The Windows CRT also reports character devices such as NUL as ttys.
            return bool(
                windll.kernel32.GetConsoleMode(handle, value)
                and windll.kernel32.GetNumberOfConsoleInputEvents(handle, value)
            )
        from fcntl import F_GETFL, fcntl

        # Writes to a PTY master become input on its paired terminal.
        if not os.isatty(fd) or _is_pty_master(fd):
            return False
        access = fcntl(fd, F_GETFL) & os.O_ACCMODE
        return access in (os.O_RDWR, os.O_WRONLY if writable else os.O_RDONLY) and (
            not writable or not _posix_output_blocked(fd)
        )
    except (OSError, ValueError):
        return False


def _can_read_terminal(stream) -> bool:
    if not _is_terminal(stream):
        return False
    try:
        return sys.platform == "win32" or os.tcgetpgrp(stream.fileno()) == os.getpgrp()
    except (OSError, ValueError):
        return False


def _device_terminal(owned: ExitStack):
    for stream in (sys.stderr, sys.stdout):
        if _is_terminal(stream, writable=True):
            return stream
    return _controlling_terminal(owned)


def _controlling_terminal(owned: ExitStack):
    if not _is_terminal(sys.stdin):
        return None
    try:
        if sys.platform == "win32":
            terminal = owned.enter_context(open("CONOUT$", "w", encoding="utf-8"))
        else:
            # Confirm that stdin reports the same foreground group as the controlling terminal.
            stdin_group = os.tcgetpgrp(sys.stdin.fileno())
            if stdin_group <= 0:
                return None
            terminal = owned.enter_context(open(
                "/dev/tty", "w", encoding="utf-8", opener=lambda path, _: os.open(path, os.O_WRONLY),
            ))
            if os.tcgetpgrp(terminal.fileno()) != stdin_group:
                return None
    except (OSError, ValueError):
        return None
    return terminal if _is_terminal(terminal, writable=True) else None


def _write_notice(notice: str, stream) -> bool:
    return stream is not None and _print_status(notice, file=stream, flush=True)


def _print_remember_notice(path: str, *, terminal=None) -> bool:
    notice = (f"Remembering this account stores renewable sign-in credentials in plaintext at {path}. "
              "Anyone who can read the file may reuse the authorization.")
    if terminal is not None:
        return _write_notice(notice, terminal)
    if _write_notice(notice, sys.stderr) or _write_notice(notice, sys.stdout):
        return True
    with ExitStack() as owned:
        return _write_notice(notice, _controlling_terminal(owned))


def _discard_typeahead(stdin) -> bool:
    fd = stdin.fileno()
    if sys.platform == "win32":
        from ctypes import c_void_p, windll
        from msvcrt import get_osfhandle

        return bool(windll.kernel32.FlushConsoleInputBuffer(c_void_p(get_osfhandle(fd))))
    import termios

    # tcflush() can stop a background job even when normal output is allowed.
    if os.tcgetpgrp(fd) != os.getpgrp():
        return False
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except termios.error:
        return False
    return True


def _enter_pressed(stdin) -> bool:
    if sys.platform == "win32":
        import msvcrt

        return msvcrt.kbhit() and msvcrt.getwch() in ("\r", "\n")
    fd = stdin.fileno()
    if os.tcgetpgrp(fd) != os.getpgrp():
        raise PermissionError("terminal input requires a foreground job")
    if not select.select((fd,), (), (), 0)[0]:
        return False
    key = os.read(fd, 1)
    if not key:
        raise EOFError
    return key in (b"\r", b"\n")


async def _listen_for_enter(stdin, verification_uri: str, owner: asyncio.Task, expires: float) -> None:
    clock = asyncio.get_running_loop().time
    while clock() < expires and not owner.cancelling():
        try:
            if _enter_pressed(stdin):
                if clock() < expires and not owner.cancelling():
                    # Some browser launchers wait until their process exits.
                    Thread(target=_open_verification_page, args=(verification_uri,), daemon=True).start()
                return
        except (EOFError, OSError, RuntimeError, ValueError):
            return
        await asyncio.sleep(0.05)


def _show_device_code(terminal, verification_uri: str, user_code: str) -> None:
    from .xbox import _device_prompt_scope

    if sys.platform != "win32" and not _is_terminal(terminal, writable=True):
        # Job control can change while the provider waits for the device-code response.
        raise OSError("device authorization terminal is no longer writable")
    scope = _device_prompt_scope.get()
    stdin = sys.stdin
    try:
        listen = scope is not None and _is_terminal(stdin) and _discard_typeahead(stdin)
    except (OSError, ValueError):
        listen = False
    # The provider validates both values before it calls this callback.
    if listen:
        instruction = f"Press Enter to open {verification_uri} here, or visit it on another device."
    else:
        instruction = f"Visit {verification_uri} on another device and enter the code."
    terminal.write(f"! Copy your one-time code to sign in to Microsoft\n  {user_code}\n\n{instruction}\n")
    terminal.flush()
    if not listen:
        return
    prompt, expires = scope
    listener = asyncio.create_task(_listen_for_enter(stdin, verification_uri, asyncio.current_task(), expires))
    prompt.callback(listener.cancel)
