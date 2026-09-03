#!/usr/bin/env python3
"""Boot the macOS guest and run a shell script in it, headlessly.

Runs INSIDE the container (it needs the QEMU monitor socket and is the slirp
host, 10.0.2.2, from the guest's point of view). Everything is stdlib + the PPM
files QEMU writes -- no ImageMagick, no bc, no docker round-trips per poll.

  RUN_SCRIPT=/work/run.sh SHARE_DIR=/share OUT_DIR=/results python3 driver.py

Result channel is a POST back from the guest rather than screen-scraping: the
guest uploads stdout and the real exit code, and the arrival of that POST is
also how we know the GUI Terminal actually took our keystrokes.
"""
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

MONITOR = os.environ.get("QEMU_MONITOR_SOCK", "/home/user/OSX-KVM/monitor.sock")
SHARE = os.environ.get("SHARE_DIR", "/share")
OUT = os.environ.get("OUT_DIR", "/results")
RUN_SCRIPT = os.environ.get("RUN_SCRIPT", "/work/run.sh")
PORT = int(os.environ.get("PORT", "8000"))
BOOT_TIMEOUT = int(os.environ.get("BOOT_TIMEOUT", "900"))
RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "900"))
COLLECT = os.environ.get("COLLECT", "").strip()
# RUN_TIMEOUT alone cannot tell "the keystrokes never reached a shell" from
# "the payload is still working", so a typing miss used to burn the whole
# budget before the first retry (issue #1: 3600 s of nothing). These split it:
#   START_TIMEOUT       how long a typed command gets to prove it ran at all
#   NO_PROGRESS_TIMEOUT how long a *running* script may produce no new output
START_TIMEOUT = int(os.environ.get("START_TIMEOUT", "120"))
NO_PROGRESS_TIMEOUT = int(os.environ.get("NO_PROGRESS_TIMEOUT", "900"))
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "20"))
ATTEMPTS = int(os.environ.get("TERMINAL_ATTEMPTS", "3"))
SHOT = "/tmp/_drv.ppm"

# Measured framebuffer signatures (mean brightness of the PPM payload).
# Width alone is NOT enough: the framebuffer switches to 1920x1080 while still
# fully black, several seconds before OpenCore paints the picker. Keying on
# width alone sends the arrow/enter into a void and the boot never starts.
PICKER_W, PICKER_MEAN = 1920, 0.005      # drawn picker measures ~0.0121
SHELL_W = 1024                           # UEFI Shell measures ~0.0193
SHELL_MEAN_LO, SHELL_MEAN_HI = 0.008, 0.05
DESKTOP_W, DESKTOP_MEAN = 1024, 0.05     # Recovery desktop measures 0.09-0.13

T0 = time.time()


def log(msg):
    print("[%7.1fs] %s" % (time.time() - T0, msg), flush=True)


# ---------------------------------------------------------------- monitor ---
SPECIAL = {
    ':': 'shift-semicolon', ';': 'semicolon', '\\': 'backslash', '|': 'shift-backslash',
    '.': 'dot', '-': 'minus', '_': 'shift-minus', '/': 'slash', '?': 'shift-slash',
    ' ': 'spc', '=': 'equal', '+': 'shift-equal', ',': 'comma',
    '<': 'shift-comma', '>': 'shift-dot', "'": 'apostrophe', '"': 'shift-apostrophe',
    '`': 'grave_accent', '~': 'shift-grave_accent', '[': 'bracket_left',
    ']': 'bracket_right', '{': 'shift-bracket_left', '}': 'shift-bracket_right',
    '!': 'shift-1', '@': 'shift-2', '#': 'shift-3', '$': 'shift-4', '%': 'shift-5',
    '^': 'shift-6', '&': 'shift-7', '*': 'shift-8', '(': 'shift-9', ')': 'shift-0',
}


class Monitor:
    def __init__(self, path=MONITOR):
        self.path = path

    def _conn(self):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.path)
        s.settimeout(5)
        time.sleep(0.3)
        try:
            s.recv(65536)
        except Exception:
            pass
        return s

    def cmd(self, *cmds, wait=0.8):
        out = []
        s = self._conn()
        try:
            for c in cmds:
                s.sendall((c + "\n").encode())
                time.sleep(wait)
                try:
                    out.append(s.recv(300000).decode(errors="replace"))
                except Exception:
                    out.append("")
        finally:
            s.close()
        return out

    def alive(self):
        try:
            return "VM status: running" in self.cmd("info status")[0]
        except Exception:
            return False

    def type(self, text, per_key=0.12):
        """Type a literal string. Raises on any character we cannot map."""
        keys = []
        for ch in text:
            if ch in SPECIAL:
                keys.append(SPECIAL[ch])
            elif ch.isdigit() or (ch.isalpha() and ch.islower()):
                keys.append(ch)
            elif ch.isalpha() and ch.isupper():
                keys.append("shift-" + ch.lower())
            else:
                raise ValueError("unmapped char %r" % ch)
        s = self._conn()
        try:
            for k in keys:
                s.sendall(("sendkey " + k + "\n").encode())
                time.sleep(per_key)
                try:
                    s.recv(65536)
                except Exception:
                    pass
        finally:
            s.close()

    def key(self, *names, per_key=0.25):
        s = self._conn()
        try:
            for n in names:
                s.sendall(("sendkey " + n + "\n").encode())
                time.sleep(per_key)
                try:
                    s.recv(65536)
                except Exception:
                    pass
        finally:
            s.close()

    def screendump(self, path=SHOT):
        try:
            os.unlink(path)
        except OSError:
            pass
        self.cmd("screendump " + path, wait=1.2)
        for _ in range(20):
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return path
            time.sleep(0.3)
        return None


def ppm_stats(path):
    """(width, height, mean_brightness, payload_bytes) straight from the PPM."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return (0, 0, 0.0, b"")
    m = re.match(rb"P6\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    if not m:
        return (0, 0, 0.0, b"")
    w, h = int(m.group(1)), int(m.group(2))
    body = data[m.end():]
    mean = (sum(body) / len(body) / 255.0) if body else 0.0
    return (w, h, mean, body)


# ------------------------------------------------------------------- http ---
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _serve(self, name):
        path = os.path.join(SHARE, name)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_GET(self):
        self._serve(os.path.basename(self.path.lstrip("/")) or "run.sh")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n) if n else b""
        name = os.path.basename(self.path.lstrip("/")) or "post"
        if name not in ("stdout", "rc", "collect.tar.gz", "started", "heartbeat"):
            name = "post"
        with open(os.path.join(OUT, name), "wb") as f:
            f.write(body)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


def serve():
    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("http server on :%d serving %s" % (PORT, SHARE))


# ------------------------------------------------------------------- boot ---
def wait_qemu(mon, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        if mon.alive():
            return True
        time.sleep(1)
    return False


def classify(mon):
    """One screendump -> ('picker'|'shell'|'desktop'|'black'|'none', frame)."""
    if not mon.screendump():
        return ("none", b"")
    w, _, mean, body = ppm_stats(SHOT)
    if w == PICKER_W:
        return ("picker" if mean > PICKER_MEAN else "black", body)
    if w == SHELL_W:
        if mean > DESKTOP_MEAN:
            return ("desktop", body)
        if mean > SHELL_MEAN_LO:
            return ("shell", body)
        return ("black", body)
    return ("none", body)


def reach_picker(mon, timeout=240):
    """Get to a PAINTED OpenCore picker, handling the UEFI Shell detour.

    The framebuffer flips to 1920x1080 while still fully black, seconds before
    OpenCore paints anything -- so 'picker' here means painted, never merely
    1920 wide. Returns as soon as it is painted: the picker auto-boots its
    default (the wrong entry) 45s later, so there is no time to re-verify.
    """
    end = time.time() + timeout
    handed_off = False
    while time.time() < end:
        kind, _ = classify(mon)
        if kind == "picker":
            return True
        if kind == "shell" and not handed_off:
            log("  UEFI Shell -> handing off to OpenCore")
            mon.type("fs0:")
            mon.key("ret")
            time.sleep(1)
            mon.type("\\efi\\boot\\bootx64.efi")
            mon.key("ret")
            handed_off = True
        time.sleep(2)
    return False


def select_installer(mon, tries=3):
    """Move to entry 2 (macOS Base System) and boot it, verifying the highlight
    actually moved before committing with Enter."""
    for i in range(1, tries + 1):
        mon.screendump()
        _, _, _, before = ppm_stats(SHOT)
        mon.key("right")
        time.sleep(1.5)
        mon.screendump()
        _, _, _, after = ppm_stats(SHOT)
        if after and before and after != before:
            log("  highlight moved (attempt %d)" % i)
            mon.key("ret")
            return True
        log("  arrow did not register (attempt %d)" % i)
    return False


def wait_settled(mon, timeout, need=3, gap=5):
    """Recovery UI is drawn AND static: same desktop frame `need` times running.

    RSS is a bad readiness signal -- it crosses any threshold well before the UI
    accepts input, which lands keystrokes in whatever dialog happens to be up.
    """
    end = time.time() + timeout
    prev = None
    stable = 0
    while time.time() < end:
        kind, body = classify(mon)
        if kind == "desktop":
            if prev is not None and body == prev:
                stable += 1
                if stable >= need:
                    return True
            else:
                stable = 0
            prev = body
        time.sleep(gap)
    return False


def boot_to_desktop(mon, attempts=3):
    """Picker -> installer -> Recovery desktop, resetting the VM on any miss."""
    for attempt in range(1, attempts + 1):
        log("boot attempt %d" % attempt)
        if not reach_picker(mon):
            log("  no painted picker; resetting")
            mon.cmd("system_reset")
            time.sleep(5)
            continue
        log("  picker painted")
        if not select_installer(mon):
            log("  could not select installer; resetting")
            mon.cmd("system_reset")
            time.sleep(5)
            continue
        log("  booting macOS Base System")
        if wait_settled(mon, BOOT_TIMEOUT // attempts if attempts > 1 else BOOT_TIMEOUT):
            return True
        log("  desktop never settled; resetting")
        mon.cmd("system_reset")
        time.sleep(5)
    return False


def main():
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(SHARE, exist_ok=True)

    with open(RUN_SCRIPT) as f:
        user_script = f.read()
    with open(os.path.join(SHARE, "user.sh"), "w") as f:
        f.write(user_script)
    # Optional: tar a directory in the guest and POST it back, so results
    # (JUnit XML, JSON) survive the guest being torn down. Sent BEFORE rc,
    # because rc is what the driver waits on -- posting it last would race.
    collect_snippet = ""
    if COLLECT:
        collect_snippet = (
            "tar -czf /tmp/collect.tar.gz -C %s . 2>/dev/null || true\n"
            "curl -s -X POST --data-binary @/tmp/collect.tar.gz "
            "http://10.0.2.2:%d/collect.tar.gz || true\n" % (COLLECT, PORT)
        )

    # Guest-side wrapper: capture output + real rc, then POST both back.
    # Recovery ships bash 3.2 -- keep this portable.
    with open(os.path.join(SHARE, "run.sh"), "w") as f:
        lines = [
            "#!/bin/sh",
            # FIRST, before anything that can fail: proof the typed line reached
            # a shell. Without this the driver cannot tell a typing miss from a
            # slow payload, which is issue #1.
            "curl -s -X POST --data-binary started "
            "http://10.0.2.2:{p}/started".format(p=PORT),
            "curl -s -o /tmp/user.sh http://10.0.2.2:{p}/user.sh || exit 90".format(p=PORT),
            # Background heartbeat carrying the tail of the live output, so a
            # hang shows WHICH step stopped instead of just going quiet.
            ": > /tmp/out",
            "( while : ; do",
            "    tail -c 4000 /tmp/out > /tmp/hb 2>/dev/null || : > /tmp/hb",
            "    curl -s -X POST --data-binary @/tmp/hb "
            "http://10.0.2.2:{p}/heartbeat".format(p=PORT),
            "    sleep {i}".format(i=HEARTBEAT_INTERVAL),
            "  done ) &",
            "_hb=$!",
            "sh /tmp/user.sh > /tmp/out 2>&1; echo $? > /tmp/rc",
            "kill $_hb 2>/dev/null",
        ]
        if COLLECT:
            lines += [
                "tar -czf /tmp/collect.tar.gz -C {c} . 2>/dev/null || true".format(c=COLLECT),
                "curl -s -X POST --data-binary @/tmp/collect.tar.gz "
                "http://10.0.2.2:{p}/collect.tar.gz || true".format(p=PORT),
            ]
        lines += [
            "curl -s -X POST --data-binary @/tmp/out http://10.0.2.2:{p}/stdout".format(p=PORT),
            # rc goes LAST: it is what the driver waits on, so posting it before
            # the collected results would race the tarball.
            "curl -s -X POST --data-binary @/tmp/rc http://10.0.2.2:{p}/rc".format(p=PORT),
        ]
        f.write("\n".join(lines) + "\n")

    serve()
    mon = Monitor()

    if not wait_qemu(mon):
        log("FAIL: qemu never reported running")
        return 3
    log("qemu up")

    if not boot_to_desktop(mon):
        log("FAIL: never reached the Recovery desktop")
        mon.screendump(os.path.join(OUT, "fail.ppm"))
        return 5
    log("Recovery desktop ready")
    time.sleep(15)

    # Open Terminal and bootstrap. Two distinct waits, because they fail for
    # completely different reasons and only one of them is worth retrying:
    #
    #   1. Did the typed line reach a shell at all?  -> START_TIMEOUT, retryable.
    #      This is issue #1: Terminal was "opened" but the keystrokes went
    #      somewhere else (a Recovery dialog can take focus), so nothing ran and
    #      the old code sat on the full RUN_TIMEOUT -- 3600 s -- before its first
    #      retry, then retried four more times with no new information.
    #   2. Is a script that HAS started still making progress? -> heartbeat +
    #      NO_PROGRESS_TIMEOUT, NOT retryable. Retyping the command at a guest
    #      that is already half-way through a run only makes the mess worse.
    boot_cmd = ("ipconfig set en0 DHCP; until curl -s -o /tmp/r.sh "
                "http://10.0.2.2:%d/run.sh; do sleep 2; done; sh /tmp/r.sh" % PORT)
    rc_path = os.path.join(OUT, "rc")
    started_path = os.path.join(OUT, "started")
    hb_path = os.path.join(OUT, "heartbeat")

    def hb_tail(n=3):
        try:
            with open(hb_path, errors="replace") as f:
                tail = [ln for ln in f.read().strip().split("\n") if ln.strip()]
            return " | ".join(tail[-n:])[:300]
        except OSError:
            return ""

    started = False
    for attempt in range(1, ATTEMPTS + 1):
        log("terminal attempt %d" % attempt)

        # Confirm Terminal actually opened BEFORE typing. If it did not, the
        # Recovery window still has focus and its default button is "Restore
        # from Time Machine" -- so blindly typing and pressing Enter launches
        # the restore assistant, which is how issue #1 actually happened (its
        # no-start screendump shows exactly that window). Opening Terminal
        # repaints a large part of the screen, so an unchanged frame means the
        # shortcut did not register and typing would do harm.
        mon.screendump()
        _, _, _, before = ppm_stats(SHOT)
        mon.key("shift-meta_l-t")
        time.sleep(12)
        mon.screendump()
        _, _, _, after = ppm_stats(SHOT)
        if before and after and before == after:
            shot = os.path.join(OUT, "no-terminal-attempt-%d.ppm" % attempt)
            mon.screendump(shot)
            log("  Terminal did not open (screen unchanged); NOT typing, to avoid "
                "hitting the Recovery window's default button. Screen: %s"
                % os.path.basename(shot))
            mon.key("esc")
            time.sleep(8)
            continue

        mon.type(boot_cmd)
        mon.key("ret")

        deadline = time.time() + START_TIMEOUT
        while time.time() < deadline:
            if os.path.exists(started_path) or os.path.exists(rc_path):
                started = True
                break
            time.sleep(2)
        if started:
            log("  guest confirmed the command started")
            break

        # A screendump here is the whole point: without it there is no way to
        # tell afterwards WHERE the keystrokes landed.
        shot = os.path.join(OUT, "no-start-attempt-%d.ppm" % attempt)
        mon.screendump(shot)
        log("  no start marker after %ds -- keystrokes did not reach a shell "
            "(screen saved to %s)" % (START_TIMEOUT, os.path.basename(shot)))
        mon.key("esc")
        time.sleep(8)

    if not started:
        log("FAIL: the run command never started after %d attempts (%ds each)"
            % (ATTEMPTS, START_TIMEOUT))
        mon.screendump(os.path.join(OUT, "fail.ppm"))
        return 6

    # It is running. Wait for rc, but give up if it stops producing output --
    # a wedged payload should not consume the whole RUN_TIMEOUT in silence.
    deadline = time.time() + RUN_TIMEOUT
    last_hb = None
    last_change = time.time()
    last_report = 0.0
    while time.time() < deadline:
        if os.path.exists(rc_path):
            break
        try:
            stamp = os.path.getmtime(hb_path)
        except OSError:
            stamp = None
        if stamp != last_hb:
            last_hb, last_change = stamp, time.time()
        quiet = time.time() - last_change
        if quiet > NO_PROGRESS_TIMEOUT:
            mon.screendump(os.path.join(OUT, "fail.ppm"))
            log("FAIL: no output for %ds (last heartbeat: %s)"
                % (int(quiet), hb_tail() or "<none>"))
            return 7
        # Surface progress in the driver log, so a hang shows its last step.
        if time.time() - last_report > 60:
            last_report = time.time()
            t = hb_tail()
            if t:
                log("  guest: %s" % t)
        time.sleep(2)

    mon.screendump(os.path.join(OUT, "final.ppm"))
    if not os.path.exists(rc_path):
        log("FAIL: guest never posted a result")
        return 6

    cp = os.path.join(OUT, "collect.tar.gz")
    if COLLECT:
        log("collected %s (%s bytes)" % (COLLECT, os.path.getsize(cp)) if os.path.exists(cp)
            else "WARNING: nothing collected from %s" % COLLECT)

    rc = open(rc_path).read().strip()
    out = ""
    sp = os.path.join(OUT, "stdout")
    if os.path.exists(sp):
        out = open(sp, errors="replace").read()
    log("guest exit code %s" % rc)
    print("----- guest stdout -----", flush=True)
    print(out, flush=True)
    print("------------------------", flush=True)
    try:
        return int(rc)
    except ValueError:
        return 7


if __name__ == "__main__":
    sys.exit(main())
