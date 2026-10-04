"""Make native assertion failures exit the process instead of opening a modal dialog (Windows only).

LiveKit's native library (livekit_ffi.dll) has a known race in its audio resampler (livekit/rust-sdks PR #1474,
not yet released) that trips a C assertion. On Windows the C runtime then shows "Assertion failed!" and the
process waits for someone to click a button. With these settings the message goes to stderr and the process
exits at once (exit code 3), so a crashed job or recording is noticed and retried. No-op on other platforms.
"""
import sys


def quiet_native_asserts() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    ucrt = ctypes.CDLL("ucrtbase")
    ucrt._set_error_mode(1)            # _OUT_TO_STDERR (1; 2 would be _OUT_TO_MSGBOX): assert() prints, no dialog
    ucrt._set_abort_behavior(0, 0x3)   # clear _WRITE_ABORT_MSG and _CALL_REPORTFAULT: no abort() dialog either
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)  # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
    return True
