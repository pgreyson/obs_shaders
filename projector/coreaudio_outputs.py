"""coreaudio_outputs.py — list CoreAudio OUTPUT devices as (name, uid) via ctypes.

The uid is what OBS stores in basic.ini [Audio] MonitoringDeviceId. No toolchain needed.
"""
import ctypes, ctypes.util
from ctypes import byref, c_uint32, c_void_p, c_char_p, c_bool, c_long

CA = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
CF = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


class PropAddr(ctypes.Structure):
    _fields_ = [("selector", c_uint32), ("scope", c_uint32), ("element", c_uint32)]


def fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


SYSTEM_OBJECT = 1
GLOBAL, OUTPUT = fourcc("glob"), fourcc("outp")
DEVICES, NAME, UID, STREAMS = fourcc("dev#"), fourcc("lnam"), fourcc("uid "), fourcc("stm#")

CA.AudioObjectGetPropertyDataSize.argtypes = [c_uint32, ctypes.POINTER(PropAddr), c_uint32, c_void_p, ctypes.POINTER(c_uint32)]
CA.AudioObjectGetPropertyData.argtypes = [c_uint32, ctypes.POINTER(PropAddr), c_uint32, c_void_p, ctypes.POINTER(c_uint32), c_void_p]
CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
CF.CFStringGetCString.restype = c_bool
CF.CFRelease.argtypes = [c_void_p]
kCFStringEncodingUTF8 = 0x08000100


def _size(obj, addr):
    n = c_uint32(0)
    if CA.AudioObjectGetPropertyDataSize(obj, byref(addr), 0, None, byref(n)) != 0:
        return 0
    return n.value


def _cfstring(obj, sel):
    addr = PropAddr(sel, GLOBAL, 0)
    ref = c_void_p(0)
    n = c_uint32(ctypes.sizeof(c_void_p))
    if CA.AudioObjectGetPropertyData(obj, byref(addr), 0, None, byref(n), byref(ref)) != 0 or not ref.value:
        return ""
    buf = ctypes.create_string_buffer(1024)
    ok = CF.CFStringGetCString(ref, buf, 1024, kCFStringEncodingUTF8)
    CF.CFRelease(ref)
    return buf.value.decode("utf-8", "replace") if ok else ""


def output_devices():
    addr = PropAddr(DEVICES, GLOBAL, 0)
    n = _size(SYSTEM_OBJECT, addr)
    ids = (c_uint32 * (n // 4))()
    size = c_uint32(n)
    CA.AudioObjectGetPropertyData(SYSTEM_OBJECT, byref(addr), 0, None, byref(size), ids)
    out = []
    for dev in ids:
        if _size(dev, PropAddr(STREAMS, OUTPUT, 0)) == 0:
            continue  # no output streams
        out.append((_cfstring(dev, NAME), _cfstring(dev, UID)))
    return out


if __name__ == "__main__":
    for name, uid in output_devices():
        print(f"{name}\t{uid}")
