#!/usr/bin/env python3
"""Send QEMU monitor (HMP) commands to the macOS guest.

Launch.sh points -monitor at a unix socket, because `-monitor stdio` has no tty
under `docker run -d`. That socket is also the only way to observe a detached
VM: `screendump` writes the current framebuffer to a PPM you can copy out.

    docker exec macos-x64 python3 /tmp/mon.py "screendump /tmp/s.ppm"
    docker exec macos-x64 python3 /tmp/mon.py "info registers" "info usb"

Beware `info usb` and friends: the monitor echoes each character you send, so
the reply is prefixed with escape-sequence noise. Read the LAST line, and do not
pipe through `tail -n` with a small n -- that truncation can hide a device that
is genuinely present and send you chasing the wrong bug.
"""
import os
import socket
import sys
import time

SOCK = os.environ.get("QEMU_MONITOR_SOCK", "/home/user/OSX-KVM/monitor.sock")


def mon(cmds, wait=1.0, sock_path=SOCK):
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock_path)
    s.settimeout(3)
    time.sleep(0.4)
    try:
        s.recv(65536)  # banner
    except Exception:
        pass
    out = []
    for c in cmds:
        s.sendall((c + "\n").encode())
        time.sleep(wait)
        try:
            out.append(s.recv(300000).decode(errors="replace"))
        except Exception:
            out.append("")
    s.close()
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for r in mon(sys.argv[1:]):
        print(r.split("\n")[-2] if "\n" in r else r)
