#!/usr/bin/env python3
"""Type strings into the macOS guest via QEMU `sendkey`.

    docker exec macos-x64 python3 /tmp/type.py 'ls -l /tmp' @ret
    docker exec macos-x64 python3 /tmp/type.py @ctrl-f2 @right @right @down @ret

Arguments starting with '@' are sent as raw key names (@ret, @down, @ctrl-f2);
everything else is typed character by character.

Why this exists rather than mouse automation: HMP `mouse_move` emits *relative*
events, which the absolute `usb-tablet` silently ignores, so the cursor cannot
be steered. `sendkey ctrl-f2` focuses the macOS menu bar and arrows do the rest.

QUOTING: use SINGLE quotes for guest commands. With double quotes your host
shell expands `$?` before the keystrokes are sent, so you type a literal that
looks exactly like a passing exit code:

    ... /tmp/type.py "prog; echo RC=$?" @ret    # WRONG -- types RC=0
    ... /tmp/type.py 'prog; echo RC=$?' @ret    # right
"""
import os
import socket
import sys
import time

SOCK = os.environ.get("QEMU_MONITOR_SOCK", "/home/user/OSX-KVM/monitor.sock")

SPECIAL = {
    ':': 'shift-semicolon', ';': 'semicolon', '\\': 'backslash', '|': 'shift-backslash',
    '.': 'dot', '-': 'minus', '_': 'shift-minus', '/': 'slash', '?': 'shift-slash',
    ' ': 'spc', '=': 'equal', '+': 'shift-equal', ',': 'comma',
    '<': 'shift-comma', '>': 'shift-dot',
    "'": 'apostrophe', '"': 'shift-apostrophe',
    '`': 'grave_accent', '~': 'shift-grave_accent',
    '[': 'bracket_left', ']': 'bracket_right',
    '{': 'shift-bracket_left', '}': 'shift-bracket_right',
    '!': 'shift-1', '@': 'shift-2', '#': 'shift-3', '$': 'shift-4', '%': 'shift-5',
    '^': 'shift-6', '&': 'shift-7', '*': 'shift-8', '(': 'shift-9', ')': 'shift-0',
}


def keys_for(text):
    out = []
    for ch in text:
        if ch in SPECIAL:
            out.append(SPECIAL[ch])
        elif ch.isdigit() or (ch.isalpha() and ch.islower()):
            out.append(ch)
        elif ch.isalpha() and ch.isupper():
            out.append('shift-' + ch.lower())
        else:
            raise SystemExit("unmapped char: %r" % ch)
    return out


def main(argv):
    if not argv:
        raise SystemExit(__doc__)
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    s.settimeout(3)
    time.sleep(0.4)
    try:
        s.recv(65536)
    except Exception:
        pass

    def send(c):
        s.sendall((c + "\n").encode())
        time.sleep(0.12)
        try:
            s.recv(65536)
        except Exception:
            pass

    for arg in argv:
        if arg.startswith("@"):
            send("sendkey " + arg[1:])
        else:
            for k in keys_for(arg):
                send("sendkey " + k)
    print("typed")


if __name__ == "__main__":
    main(sys.argv[1:])
