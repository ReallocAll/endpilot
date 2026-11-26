"""Windows cache files with a protected DACL for the current user."""

from contextlib import ExitStack, contextmanager
from ctypes import WinError
import msvcrt
import os

import pywintypes
import win32api
import win32con
import win32file
import win32security
import winerror


_NOT_FOUND = {winerror.ERROR_FILE_NOT_FOUND, winerror.ERROR_PATH_NOT_FOUND}
_SHARE = win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE
_OPEN_OBJECT = win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32file.FILE_FLAG_BACKUP_SEMANTICS


def _user_sid():
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def _private_attributes(sid):
    sid_text = win32security.ConvertSidToStringSid(sid)
    attrs = pywintypes.SECURITY_ATTRIBUTES()
    attrs.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        f"O:{sid_text}D:P(A;;FA;;;{sid_text})", win32security.SDDL_REVISION_1,
    )
    return attrs


def _check_private(handle, sid):
    descriptor = win32security.GetSecurityInfo(
        handle, win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    control, _ = descriptor.GetSecurityDescriptorControl()
    if (descriptor.GetSecurityDescriptorOwner() != sid or not control & win32security.SE_DACL_PROTECTED
            or dacl is None or dacl.GetAceCount() != 1):
        raise PermissionError("Xbox cache ACL must be private to the current user")
    ace = dacl.GetAce(0)
    if ace[0][0] != win32security.ACCESS_ALLOWED_ACE_TYPE or ace[2] != sid:
        raise PermissionError("Xbox cache ACL must be private to the current user")


def _open(path, access, disposition, attributes=None):
    return win32file.CreateFile(str(path), access, _SHARE, attributes, disposition, _OPEN_OBJECT, None)


def _check_object(handle, *, directory):
    info = win32file.GetFileInformationByHandle(handle)
    attributes = info[0]
    if (attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory
            or (not directory and (info[7] != 1 or win32file.GetFileType(handle) != win32file.FILE_TYPE_DISK))):
        raise PermissionError("Xbox cache path must contain only ordinary directories and files")


@contextmanager
def open_directory(path, *, create):
    """Keep directory handles open without delete sharing during a cache operation."""
    sid = _user_sid()
    with ExitStack() as opened:
        for current in (*reversed(path.parents), path):
            access = win32con.READ_CONTROL if current == path else 0
            try:
                handle = _open(current, access, win32con.OPEN_EXISTING)
            except pywintypes.error as error:
                if not create or error.winerror not in _NOT_FOUND:
                    raise WinError(error.winerror) from None
                try:
                    win32file.CreateDirectory(str(current), _private_attributes(sid))
                except pywintypes.error as mkdir_error:
                    if mkdir_error.winerror != winerror.ERROR_ALREADY_EXISTS:
                        raise WinError(mkdir_error.winerror) from None
                try:
                    handle = _open(current, access, win32con.OPEN_EXISTING)
                except pywintypes.error as open_error:
                    raise WinError(open_error.winerror) from None
            opened.callback(handle.Close)
            _check_object(handle, directory=True)
            if current == path:
                _check_private(handle, sid)
        yield


def open_file(path, flags):
    sid = _user_sid()
    write_mode = flags & (os.O_WRONLY | os.O_RDWR)
    access = win32con.READ_CONTROL | win32con.GENERIC_READ
    if write_mode:
        access |= win32con.GENERIC_WRITE
    create = bool(flags & os.O_CREAT)
    disposition = win32con.OPEN_EXISTING
    if create:
        disposition = win32con.CREATE_NEW if flags & os.O_EXCL else win32con.OPEN_ALWAYS
    try:
        handle = _open(path, access, disposition, _private_attributes(sid) if create else None)
        try:
            _check_object(handle, directory=False)
            _check_private(handle, sid)
            native_handle = handle.Detach()
        finally:
            handle.Close()
    except pywintypes.error as error:
        raise WinError(error.winerror) from None
    try:
        return msvcrt.open_osfhandle(native_handle, write_mode | os.O_BINARY | os.O_NOINHERIT)
    except OSError:
        win32api.CloseHandle(native_handle)
        raise
