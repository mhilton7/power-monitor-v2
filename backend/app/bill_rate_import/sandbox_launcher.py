from __future__ import annotations

import ctypes
import errno
import importlib
import os
import platform
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar, Final

from ..constants import MAX_PDF_BYTES

# The launcher is deliberately small and imports no PDF parser. It establishes the
# kernel boundary before the worker reads a single byte from stdin.
SANDBOX_ROOT: Final = Path("/opt/pm-pdf-sandbox")
SANDBOX_PYTHON: Final = SANDBOX_ROOT / "runtime/usr/local/bin/python3.13"
SANDBOX_ENTRYPOINT: Final = SANDBOX_ROOT / "entry.py"
TMPFS_MAGIC: Final = 0x01021994
_O_PATH: Final = 0x200000
_O_CLOEXEC: Final = 0x80000
_ST_NOSUID: Final = 0x2
_ST_NODEV: Final = 0x4
_ST_NOEXEC: Final = 0x8

_PR_SET_DUMPABLE = 4
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000

_BPF_LD = 0x00
_BPF_W = 0x00
_BPF_ABS = 0x20
_BPF_JMP = 0x05
_BPF_JEQ = 0x10
_BPF_K = 0x00
_BPF_RET = 0x06

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14
_LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15

_LANDLOCK_READ_EXECUTE = (
    _LANDLOCK_ACCESS_FS_EXECUTE | _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
)
_LANDLOCK_READ_DIRECTORY = _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
_LANDLOCK_READ_FILE = _LANDLOCK_ACCESS_FS_READ_FILE
_LANDLOCK_READ_WRITE_FILE = _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_WRITE_FILE


class SandboxBoundaryError(RuntimeError):
    """The required production kernel boundary could not be established."""


class _StatFs(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("f_type", ctypes.c_long),
        ("f_bsize", ctypes.c_long),
        ("f_blocks", ctypes.c_ulong),
        ("f_bfree", ctypes.c_ulong),
        ("f_bavail", ctypes.c_ulong),
        ("f_files", ctypes.c_ulong),
        ("f_ffree", ctypes.c_ulong),
        ("f_fsid", ctypes.c_int * 2),
        ("f_namelen", ctypes.c_long),
        ("f_frsize", ctypes.c_long),
        ("f_flags", ctypes.c_long),
        ("f_spare", ctypes.c_long * 4),
    ]


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
        ("reserved", ctypes.c_uint32),
    ]


class _SockFilter(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("length", ctypes.c_ushort),
        ("filters", ctypes.POINTER(_SockFilter)),
    ]


_ARCHITECTURES: Final[dict[str, tuple[int, dict[str, int]]]] = {
    "x86_64": (
        0xC000003E,
        {
            "socket": 41,
            "connect": 42,
            "accept": 43,
            "sendto": 44,
            "recvfrom": 45,
            "sendmsg": 46,
            "recvmsg": 47,
            "shutdown": 48,
            "bind": 49,
            "listen": 50,
            "getsockname": 51,
            "getpeername": 52,
            "socketpair": 53,
            "setsockopt": 54,
            "getsockopt": 55,
            "ptrace": 101,
            "pivot_root": 155,
            "chroot": 161,
            "mount": 165,
            "umount2": 166,
            "add_key": 248,
            "request_key": 249,
            "keyctl": 250,
            "unshare": 272,
            "accept4": 288,
            "perf_event_open": 298,
            "recvmmsg": 299,
            "fanotify_init": 300,
            "name_to_handle_at": 303,
            "open_by_handle_at": 304,
            "setns": 308,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "kcmp": 312,
            "bpf": 321,
            "userfaultfd": 323,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "open_tree": 428,
            "move_mount": 429,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "pidfd_getfd": 438,
            "process_madvise": 440,
            "mount_setattr": 442,
        },
    ),
    "aarch64": (
        0xC00000B7,
        {
            "umount2": 39,
            "mount": 40,
            "pivot_root": 41,
            "chroot": 51,
            "unshare": 97,
            "ptrace": 117,
            "socket": 198,
            "socketpair": 199,
            "bind": 200,
            "listen": 201,
            "accept": 202,
            "connect": 203,
            "getsockname": 204,
            "getpeername": 205,
            "sendto": 206,
            "recvfrom": 207,
            "setsockopt": 208,
            "getsockopt": 209,
            "shutdown": 210,
            "sendmsg": 211,
            "recvmsg": 212,
            "add_key": 217,
            "request_key": 218,
            "keyctl": 219,
            "perf_event_open": 241,
            "accept4": 242,
            "recvmmsg": 243,
            "fanotify_init": 262,
            "name_to_handle_at": 264,
            "open_by_handle_at": 265,
            "setns": 268,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "kcmp": 272,
            "bpf": 280,
            "userfaultfd": 282,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "open_tree": 428,
            "move_mount": 429,
            "fsopen": 430,
            "fsconfig": 431,
            "fsmount": 432,
            "fspick": 433,
            "pidfd_getfd": 438,
            "process_madvise": 440,
            "mount_setattr": 442,
        },
    ),
}

_LANDLOCK_SYSCALLS: Final = {
    "x86_64": (444, 445, 446),
    "aarch64": (444, 445, 446),
}


def _libc() -> ctypes.CDLL:
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    return library


def _raise_errno(operation: str) -> None:
    error_number = ctypes.get_errno() or errno.EPERM
    raise SandboxBoundaryError(f"{operation} failed with errno {error_number}")


def _verify_pipe_ipc() -> None:
    if not stat.S_ISFIFO(os.fstat(0).st_mode) or not stat.S_ISFIFO(os.fstat(1).st_mode):
        raise SandboxBoundaryError("sandbox stdin/stdout must be anonymous pipes")


def _verify_private_tmpfs(workdir: Path) -> None:
    expected_parent = Path("/tmp").resolve(strict=True)  # noqa: S108 - required tmpfs mount
    resolved = workdir.resolve(strict=True)
    if resolved.parent != expected_parent or not resolved.name.startswith("pm-pdf-sandbox-"):
        raise SandboxBoundaryError("sandbox work directory is outside the private tmpfs")
    details = resolved.stat()
    current_uid = Path("/proc/self").stat().st_uid
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != current_uid:
        raise SandboxBoundaryError("sandbox work directory ownership is invalid")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise SandboxBoundaryError("sandbox work directory must have mode 0700")
    if any(resolved.iterdir()):
        raise SandboxBoundaryError("sandbox work directory must start empty")

    filesystem = _StatFs()
    libc = _libc()
    if libc.statfs(os.fsencode(resolved), ctypes.byref(filesystem)) != 0:
        _raise_errno("statfs")
    required_flags = _ST_NODEV | _ST_NOEXEC | _ST_NOSUID
    if filesystem.f_type != TMPFS_MAGIC or filesystem.f_flags & required_flags != required_flags:
        raise SandboxBoundaryError("sandbox work directory must be on a nodev,noexec,nosuid tmpfs")


def _verify_trusted_path(path: Path) -> None:
    details = path.stat()
    if details.st_uid != 0 or stat.S_IMODE(details.st_mode) & 0o022:
        raise SandboxBoundaryError(f"sandbox runtime path is not root-owned and immutable: {path}")


def _verify_frozen_runtime() -> None:
    _verify_trusted_path(SANDBOX_ROOT)
    for root, directories, files in os.walk(SANDBOX_ROOT):
        root_path = Path(root)
        for name in (*directories, *files):
            _verify_trusted_path(root_path / name)


def _supported_landlock_access(abi: int) -> int:
    if abi < 3:
        # ABI 3 is required so truncate(2) is governed by the policy as well as open(2).
        raise SandboxBoundaryError("Linux Landlock ABI 3 or newer is required")
    access = (1 << 15) - 1
    if abi >= 5:
        access |= _LANDLOCK_ACCESS_FS_IOCTL_DEV
    return access


def _add_landlock_path(
    libc: ctypes.CDLL,
    add_rule_number: int,
    ruleset_fd: int,
    path: Path,
    allowed_access: int,
) -> None:
    path_fd = os.open(path, _O_PATH | _O_CLOEXEC)
    try:
        attribute = _LandlockPathBeneathAttr(
            allowed_access=allowed_access,
            parent_fd=path_fd,
            reserved=0,
        )
        result = libc.syscall(
            add_rule_number,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attribute),
            0,
        )
        if result != 0:
            _raise_errno(f"landlock_add_rule({path})")
    finally:
        os.close(path_fd)


def _install_landlock(workdir: Path, trusted_read_paths: Sequence[tuple[Path, int]]) -> None:
    architecture = platform.machine().lower()
    syscall_numbers = _LANDLOCK_SYSCALLS.get(architecture)
    if syscall_numbers is None:
        raise SandboxBoundaryError(f"unsupported sandbox architecture: {architecture}")
    create_number, add_rule_number, restrict_number = syscall_numbers
    libc = _libc()
    abi = libc.syscall(create_number, 0, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    if abi < 0:
        _raise_errno("landlock ABI query")
    handled_access = _supported_landlock_access(int(abi))
    ruleset_attribute = _LandlockRulesetAttr(handled_access_fs=handled_access)
    ruleset_fd = libc.syscall(
        create_number,
        ctypes.byref(ruleset_attribute),
        ctypes.sizeof(ruleset_attribute),
        0,
    )
    if ruleset_fd < 0:
        _raise_errno("landlock_create_ruleset")
    try:
        for path, access in trusted_read_paths:
            _add_landlock_path(
                libc,
                add_rule_number,
                int(ruleset_fd),
                path,
                access & handled_access,
            )
        writable_access = (
            _LANDLOCK_ACCESS_FS_READ_FILE
            | _LANDLOCK_ACCESS_FS_READ_DIR
            | _LANDLOCK_ACCESS_FS_WRITE_FILE
            | _LANDLOCK_ACCESS_FS_REMOVE_DIR
            | _LANDLOCK_ACCESS_FS_REMOVE_FILE
            | _LANDLOCK_ACCESS_FS_MAKE_DIR
            | _LANDLOCK_ACCESS_FS_MAKE_REG
            | _LANDLOCK_ACCESS_FS_MAKE_SYM
            | _LANDLOCK_ACCESS_FS_REFER
            | _LANDLOCK_ACCESS_FS_TRUNCATE
        )
        _add_landlock_path(
            libc,
            add_rule_number,
            int(ruleset_fd),
            workdir,
            writable_access & handled_access,
        )
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            _raise_errno("PR_SET_NO_NEW_PRIVS")
        if libc.syscall(restrict_number, int(ruleset_fd), 0) != 0:
            _raise_errno("landlock_restrict_self")
    finally:
        os.close(int(ruleset_fd))


def _bpf_statement(code: int, value: int) -> _SockFilter:
    return _SockFilter(code=code, jt=0, jf=0, k=value)


def _bpf_jump(code: int, value: int, true_offset: int, false_offset: int) -> _SockFilter:
    return _SockFilter(code=code, jt=true_offset, jf=false_offset, k=value)


def _install_seccomp() -> None:
    architecture = platform.machine().lower()
    architecture_definition = _ARCHITECTURES.get(architecture)
    if architecture_definition is None:
        raise SandboxBoundaryError(f"unsupported seccomp architecture: {architecture}")
    audit_arch, syscall_numbers = architecture_definition
    instructions: list[_SockFilter] = [
        _bpf_statement(_BPF_LD | _BPF_W | _BPF_ABS, 4),
        _bpf_jump(_BPF_JMP | _BPF_JEQ | _BPF_K, audit_arch, 1, 0),
        _bpf_statement(_BPF_RET | _BPF_K, _SECCOMP_RET_KILL_PROCESS),
        _bpf_statement(_BPF_LD | _BPF_W | _BPF_ABS, 0),
    ]
    for number in sorted(set(syscall_numbers.values())):
        instructions.extend(
            (
                _bpf_jump(_BPF_JMP | _BPF_JEQ | _BPF_K, number, 0, 1),
                _bpf_statement(_BPF_RET | _BPF_K, _SECCOMP_RET_ERRNO | errno.EPERM),
            )
        )
    instructions.append(_bpf_statement(_BPF_RET | _BPF_K, _SECCOMP_RET_ALLOW))
    array_type = _SockFilter * len(instructions)
    instruction_array = array_type(*instructions)
    program = _SockFprog(
        length=len(instructions),
        filters=ctypes.cast(instruction_array, ctypes.POINTER(_SockFilter)),
    )
    libc = _libc()
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program)) != 0:
        _raise_errno("seccomp filter installation")


def _apply_resource_limits(timeout_seconds: int) -> None:
    resource_module = importlib.import_module("resource")
    resource_module.setrlimit(resource_module.RLIMIT_CORE, (0, 0))
    resource_module.setrlimit(resource_module.RLIMIT_CPU, (timeout_seconds, timeout_seconds + 1))
    resource_module.setrlimit(resource_module.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource_module.setrlimit(resource_module.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    resource_module.setrlimit(resource_module.RLIMIT_NOFILE, (32, 32))
    if hasattr(resource_module, "RLIMIT_NPROC"):
        resource_module.setrlimit(resource_module.RLIMIT_NPROC, (32, 32))


def _close_inherited_file_descriptors() -> None:
    try:
        descriptors = tuple(int(name) for name in os.listdir("/proc/self/fd"))
    except (OSError, ValueError) as exc:
        raise SandboxBoundaryError("cannot enumerate inherited file descriptors") from exc
    for descriptor in descriptors:
        if descriptor > 2:
            try:
                os.close(descriptor)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise SandboxBoundaryError("cannot close inherited file descriptor") from exc


def _production_read_paths() -> tuple[tuple[Path, int], ...]:
    loader_by_architecture = {
        "x86_64": Path("/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"),
        "aarch64": Path("/usr/lib/aarch64-linux-gnu/ld-linux-aarch64.so.1"),
    }
    dynamic_loader = loader_by_architecture.get(platform.machine().lower())
    if dynamic_loader is None:
        raise SandboxBoundaryError("unsupported dynamic-loader architecture")
    required = (
        (SANDBOX_ROOT, _LANDLOCK_READ_EXECUTE),
        (dynamic_loader, _LANDLOCK_READ_FILE | _LANDLOCK_ACCESS_FS_EXECUTE),
        (Path("/usr/bin/tesseract"), _LANDLOCK_READ_FILE | _LANDLOCK_ACCESS_FS_EXECUTE),
        (Path("/usr/lib"), _LANDLOCK_READ_DIRECTORY),
        (Path("/usr/share/tesseract-ocr"), _LANDLOCK_READ_DIRECTORY),
        (Path("/usr/share/fonts"), _LANDLOCK_READ_DIRECTORY),
        (Path("/etc/fonts"), _LANDLOCK_READ_DIRECTORY),
        (Path("/etc/ld.so.cache"), _LANDLOCK_READ_FILE),
        (Path("/dev/null"), _LANDLOCK_READ_WRITE_FILE),
        (Path("/dev/urandom"), _LANDLOCK_READ_FILE),
    )
    optional = (
        (Path("/var/cache/fontconfig"), _LANDLOCK_READ_DIRECTORY),
        (Path("/usr/share/fontconfig"), _LANDLOCK_READ_DIRECTORY),
    )
    for path, _access in required:
        if not path.exists():
            raise SandboxBoundaryError(f"required sandbox runtime path is missing: {path}")
        if not str(path).startswith("/dev/"):
            _verify_trusted_path(path)
    _verify_frozen_runtime()
    present_optional = tuple(item for item in optional if item[0].exists())
    for path, _access in present_optional:
        _verify_trusted_path(path)
    return (*required, *present_optional)


def _fixed_environment(workdir: Path) -> dict[str, str]:
    return {
        "HOME": str(workdir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "LD_LIBRARY_PATH": str(SANDBOX_ROOT / "runtime/usr/local/lib"),
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHOME": str(SANDBOX_ROOT / "runtime/usr/local"),
        "TESSDATA_PREFIX": "/usr/share/tesseract-ocr/5/tessdata",
        "TMPDIR": str(workdir),
        "XDG_CACHE_HOME": str(workdir),
    }


def _establish_boundary(
    workdir: Path,
    timeout_seconds: int,
    *,
    require_tmpfs: bool = True,
    trusted_read_paths: Sequence[tuple[Path, int]] | None = None,
) -> None:
    """Establish the boundary; the non-tmpfs option is for Linux tests only."""

    if platform.system() != "Linux":
        raise SandboxBoundaryError("the production PDF sandbox requires Linux")
    _verify_pipe_ipc()
    if require_tmpfs:
        _verify_private_tmpfs(workdir)
    else:
        details = workdir.stat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o700:
            raise SandboxBoundaryError("test work directory must have mode 0700")
    read_paths = tuple(trusted_read_paths or _production_read_paths())
    _close_inherited_file_descriptors()
    _apply_resource_limits(timeout_seconds)
    os.umask(0o077)
    os.chdir(workdir)
    os.environ.clear()
    os.environ.update(_fixed_environment(workdir))
    libc = _libc()
    if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        _raise_errno("PR_SET_DUMPABLE")
    _install_landlock(workdir, read_paths)
    _install_seccomp()


def _parse_arguments(arguments: Sequence[str]) -> tuple[str, Path, int, str | None]:
    if len(arguments) not in (3, 4, 5):
        raise SandboxBoundaryError("invalid sandbox invocation")
    mode = arguments[1]
    if mode not in {"parse", "self-test"}:
        raise SandboxBoundaryError("invalid sandbox mode")
    workdir = Path(arguments[2])
    timeout_seconds = int(arguments[3]) if len(arguments) >= 4 else 30
    if not 5 <= timeout_seconds <= 60:
        raise SandboxBoundaryError("invalid sandbox timeout")
    sentinel = arguments[4] if len(arguments) == 5 else None
    if mode == "parse" and sentinel is not None:
        raise SandboxBoundaryError("parse mode does not accept a sentinel")
    return mode, workdir, timeout_seconds, sentinel


def main(arguments: Sequence[str] | None = None) -> int:
    try:
        mode, workdir, timeout_seconds, sentinel = _parse_arguments(arguments or sys.argv)
        _establish_boundary(workdir, timeout_seconds)
        worker_arguments = [str(SANDBOX_PYTHON), "-P", "-S", str(SANDBOX_ENTRYPOINT), mode]
        if sentinel is not None:
            worker_arguments.append(sentinel)
        if mode == "parse":
            worker_arguments.append(str(MAX_PDF_BYTES))
        os.execve(  # noqa: S606 - executable and argv are fixed by this launcher
            SANDBOX_PYTHON, worker_arguments, dict(os.environ)
        )
    except (OSError, ValueError, SandboxBoundaryError):
        # No exception text crosses stderr/stdout. Parent maps every setup failure to
        # the single fail-closed public error.
        return 126
    return 126


if __name__ == "__main__":
    raise SystemExit(main())
