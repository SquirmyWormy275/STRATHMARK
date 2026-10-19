"""Private Win32 file creation and handle-based ACL verification.

Loaded only on Windows. The process user is the sole DACL principal; inherited
permissions never authorize another account. Elevated-token default owners are
accepted for existing files, but never added to the DACL.
"""

import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes as wt


class SecurityAttributes(ctypes.Structure):
    _fields_ = (("length", wt.DWORD), ("descriptor", wt.LPVOID), ("inherit", wt.BOOL))


class FileInformation(ctypes.Structure):
    _fields_ = (
        ("attributes", wt.DWORD),
        ("created", wt.FILETIME),
        ("accessed", wt.FILETIME),
        ("written", wt.FILETIME),
        ("volume", wt.DWORD),
        ("size_high", wt.DWORD),
        ("size_low", wt.DWORD),
        ("links", wt.DWORD),
        ("index_high", wt.DWORD),
        ("index_low", wt.DWORD),
    )


class AclHeader(ctypes.Structure):
    _fields_ = (
        ("revision", wt.BYTE),
        ("reserved", wt.BYTE),
        ("size", wt.WORD),
        ("count", wt.WORD),
        ("reserved2", wt.WORD),
    )


class AllowedAce(ctypes.Structure):
    _fields_ = (
        ("kind", wt.BYTE),
        ("flags", wt.BYTE),
        ("size", wt.WORD),
        ("mask", wt.DWORD),
        ("sid", wt.DWORD),
    )


class WindowsStorage:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.security = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = wt.LPVOID
        pp = ctypes.POINTER(pointer)
        self._bind(self.kernel, "GetCurrentProcess", [], wt.HANDLE)
        self._bind(self.kernel, "CloseHandle", [wt.HANDLE], wt.BOOL)
        self._bind(self.kernel, "LocalFree", [pointer], pointer)
        self._bind(self.kernel, "CreateDirectoryW", [wt.LPCWSTR, pointer], wt.BOOL)
        self._bind(
            self.kernel,
            "CreateFileW",
            [wt.LPCWSTR, wt.DWORD, wt.DWORD, pointer, wt.DWORD, wt.DWORD, wt.HANDLE],
            wt.HANDLE,
        )
        self._bind(
            self.kernel,
            "GetFileInformationByHandle",
            [wt.HANDLE, ctypes.POINTER(FileInformation)],
            wt.BOOL,
        )
        self._bind(self.security, "OpenProcessToken", [wt.HANDLE, wt.DWORD, pp], wt.BOOL)
        self._bind(
            self.security,
            "GetTokenInformation",
            [wt.HANDLE, wt.DWORD, pointer, wt.DWORD, ctypes.POINTER(wt.DWORD)],
            wt.BOOL,
        )
        self._bind(self.security, "ConvertSidToStringSidW", [pointer, pp], wt.BOOL)
        self._bind(
            self.security,
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            [wt.LPCWSTR, wt.DWORD, pp, pointer],
            wt.BOOL,
        )
        self._bind(
            self.security,
            "GetSecurityInfo",
            [wt.HANDLE, wt.DWORD, wt.DWORD, pp, pp, pp, pp, pp],
            wt.DWORD,
        )
        self._bind(
            self.security,
            "SetSecurityInfo",
            [wt.HANDLE, wt.DWORD, wt.DWORD, pointer, pointer, pointer, pointer],
            wt.DWORD,
        )
        self._bind(
            self.security,
            "GetSecurityDescriptorDacl",
            [pointer, ctypes.POINTER(wt.BOOL), pp, ctypes.POINTER(wt.BOOL)],
            wt.BOOL,
        )
        self._bind(
            self.security,
            "GetSecurityDescriptorControl",
            [pointer, ctypes.POINTER(wt.WORD), ctypes.POINTER(wt.DWORD)],
            wt.BOOL,
        )
        self._bind(self.security, "GetAce", [pointer, wt.DWORD, pp], wt.BOOL)
        self._bind(self.security, "EqualSid", [pointer, pointer], wt.BOOL)
        token = pointer()
        self._check(
            self.security.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, ctypes.byref(token))
        )
        try:
            self.user_buffer = self._token_info(token, 1)
            self.owner_buffer = self._token_info(token, 4)
        finally:
            self.kernel.CloseHandle(token)
        self.user = ctypes.cast(self.user_buffer, pp).contents.value
        self.owner = ctypes.cast(self.owner_buffer, pp).contents.value
        text = pointer()
        self._check(self.security.ConvertSidToStringSidW(self.user, ctypes.byref(text)))
        try:
            self.sid = ctypes.wstring_at(text)
        finally:
            self.kernel.LocalFree(text)

    @staticmethod
    def _bind(library, name, args, result):
        function = getattr(library, name)
        function.argtypes = args
        function.restype = result

    @staticmethod
    def _check(success, error=None):
        if not success:
            code = ctypes.get_last_error() if error is None else error
            raise PermissionError(f"Private Windows authority storage failed (Win32 {code})")

    def _token_info(self, token, kind):
        length = wt.DWORD()
        self.security.GetTokenInformation(token, kind, None, 0, ctypes.byref(length))
        self._check(0 < length.value < 65536)
        value = ctypes.create_string_buffer(length.value)
        self._check(
            self.security.GetTokenInformation(token, kind, value, length, ctypes.byref(length))
        )
        return value

    @contextmanager
    def descriptor(self, directory):
        descriptor = wt.LPVOID()
        inheritance = "OICI" if directory else ""
        sddl = f"O:{self.sid}D:P(A;{inheritance};FA;;;{self.sid})"
        self._check(
            self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None
            )
        )
        try:
            yield descriptor
        finally:
            self.kernel.LocalFree(descriptor)

    def _private_acl(self, descriptor, dacl, directory):
        control, revision = wt.WORD(), wt.DWORD()
        self._check(
            self.security.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            )
        )
        if not dacl or not control.value & 0x1000:
            return False
        if ctypes.cast(dacl, ctypes.POINTER(AclHeader)).contents.count != 1:
            return False
        ace_pointer = wt.LPVOID()
        self._check(self.security.GetAce(dacl, 0, ctypes.byref(ace_pointer)))
        ace = ctypes.cast(ace_pointer, ctypes.POINTER(AllowedAce)).contents
        sid = ace_pointer.value + AllowedAce.sid.offset
        return (
            ace.kind == 0
            and ace.flags == (3 if directory else 0)
            and ace.mask == 0x1F01FF
            and bool(self.security.EqualSid(sid, self.user))
        )

    def _secure_handle(self, handle, directory, descriptor):
        information = FileInformation()
        self._check(self.kernel.GetFileInformationByHandle(handle, ctypes.byref(information)))
        if (
            information.attributes & 0x400
            or bool(information.attributes & 0x10) != directory
            or (not directory and information.links != 1)
        ):
            raise PermissionError("Authority storage must not be a reparse point or hard link")
        for attempt in range(2):
            owner, dacl, observed = wt.LPVOID(), wt.LPVOID(), wt.LPVOID()
            code = self.security.GetSecurityInfo(
                handle,
                1,
                5,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(observed),
            )
            self._check(code == 0, code)
            try:
                if not (
                    self.security.EqualSid(owner, self.user)
                    or self.security.EqualSid(owner, self.owner)
                ):
                    raise PermissionError("Authority storage is owned by another account")
                if self._private_acl(observed, dacl, directory):
                    return
            finally:
                self.kernel.LocalFree(observed)
            if attempt:
                raise PermissionError("Filesystem did not retain a private Windows ACL")
            present, defaulted, private_dacl = wt.BOOL(), wt.BOOL(), wt.LPVOID()
            self._check(
                self.security.GetSecurityDescriptorDacl(
                    descriptor,
                    ctypes.byref(present),
                    ctypes.byref(private_dacl),
                    ctypes.byref(defaulted),
                )
            )
            self._check(present.value and private_dacl.value)
            code = self.security.SetSecurityInfo(
                handle, 1, 0x80000004, None, None, private_dacl, None
            )
            self._check(code == 0, code)

    def open_file(self, path, *, exclusive=False):
        return self._open(path, directory=False, exclusive=exclusive)

    def directory(self, path):
        with self.descriptor(True) as descriptor:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                code = ctypes.get_last_error()
                self._check(code == 183, code)
        self.kernel.CloseHandle(self._open(path, directory=True))

    def _open(self, path, *, directory, exclusive=False):
        with self.descriptor(directory) as descriptor:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            # OPEN_REPARSE_POINT makes final-link inspection handle-based. No
            # DELETE sharing prevents replacement while security is inspected.
            handle = self.kernel.CreateFileW(
                str(path),
                0x60000 | (0 if directory else 0xC0000000),
                3,
                ctypes.byref(attributes),
                3 if directory else (1 if exclusive else 4),
                0x00200000 | (0x02000000 if directory else 0x80),
                None,
            )
            if handle == wt.HANDLE(-1).value:
                code = ctypes.get_last_error()
                if exclusive and code in (80, 183):
                    raise FileExistsError(path)
                self._check(False, code)
            try:
                self._secure_handle(handle, directory, descriptor)
            except BaseException:
                self.kernel.CloseHandle(handle)
                raise
            return handle

    def descriptor_file(self, path, *, exclusive=False):
        import msvcrt

        handle = self.open_file(path, exclusive=exclusive)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
