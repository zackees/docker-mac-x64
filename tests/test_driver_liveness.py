"""Unit tests for the driver's liveness and stall-collect paths (soldr#3097).

Run:  uv run --no-project python -m unittest discover -s tests -v

Everything that touches QEMU or the guest is faked: the monitor is a stub that
writes synthetic PPM frames, the "guest" is the test writing files into the
results dir when the collect command is typed, and time.sleep is a no-op.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import driver


def ppm(fill):
    return b"P6\n4 4\n255\n" + bytes([fill]) * 48


class FakeMonitor:
    """Stub of driver.Monitor. `frames` are consumed one per screendump; the
    last one repeats. `on_type` lets a test play the guest."""

    def __init__(self, frames, on_type=None):
        self.frames = list(frames)
        self.keys = []
        self.typed = []
        self.dumps = []
        self.on_type = on_type

    def screendump(self, path=None):
        path = path or driver.SHOT
        self.dumps.append(path)
        fill = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        with open(path, "wb") as f:
            f.write(ppm(fill))
        return path

    def key(self, *names, per_key=0):
        self.keys.extend(names)

    def type(self, text, per_key=0):
        self.typed.append(text)
        if self.on_type:
            self.on_type(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "results")
        os.makedirs(self.out)
        patches = [
            mock.patch.object(driver, "SHOT", os.path.join(self.tmp, "shot.ppm")),
            mock.patch.object(driver.time, "sleep", lambda *_: None),
            mock.patch.object(driver, "log", lambda *_: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def guest_posts(self, *names):
        for n in names:
            with open(os.path.join(self.out, n), "wb") as f:
                f.write(b"x" * 10)


class HeartbeatScript(unittest.TestCase):
    def test_heartbeat_curl_is_bounded_and_non_fatal(self):
        s = driver.run_script(port=8000, collect="", interval=20, hb_max_time=15)
        hb = [ln for ln in s.splitlines() if "/heartbeat" in ln]
        self.assertEqual(len(hb), 1)
        self.assertIn("--max-time 15", hb[0])
        self.assertTrue(hb[0].rstrip().endswith("|| true"), hb[0])

    def test_heartbeat_pid_is_recorded(self):
        self.assertIn("echo $_hb > /tmp/hb.pid", driver.run_script())

    def test_collector_is_fetched_up_front_and_rc_is_posted_last(self):
        lines = driver.run_script(port=8000, collect="/tmp/results").splitlines()
        fetch = next(i for i, ln in enumerate(lines) if "-o /tmp/collect.sh" in ln)
        run = next(i for i, ln in enumerate(lines) if "sh /tmp/user.sh" in ln)
        coll = next(i for i, ln in enumerate(lines) if ln.startswith("sh /tmp/collect.sh"))
        rc = next(i for i, ln in enumerate(lines) if "@/tmp/rc http" in ln)
        self.assertLess(fetch, run)
        self.assertLess(run, coll)
        self.assertLess(coll, rc)

    def test_collect_script_with_and_without_collect_dir(self):
        with_dir = driver.collect_script(port=8000, collect="/tmp/results")
        self.assertIn("tar -czf /tmp/collect.tar.gz -C /tmp/results .", with_dir)
        self.assertIn("/collect.tar.gz", with_dir)
        self.assertIn("@/tmp/out http://10.0.2.2:8000/stdout", with_dir)
        without = driver.collect_script(port=8000, collect="")
        self.assertNotIn("tar ", without)
        self.assertIn("/stdout", without)
        for ln in with_dir.splitlines() + without.splitlines():
            if "curl" in ln:
                self.assertIn("--max-time", ln)


class StallCollect(Base):
    def test_opens_second_terminal_and_collects(self):
        mon = FakeMonitor(frames=[1, 2],
                          on_type=lambda _: self.guest_posts("stdout", "collect.tar.gz"))
        msg = driver.stall_collect(mon, out=self.out, collect="/tmp/results", timeout=5)
        self.assertIn("meta_l-n", mon.keys)
        self.assertEqual(mon.typed, ["sh /tmp/collect.sh"])
        self.assertIn("stdout (10 bytes)", msg)
        self.assertIn("collect.tar.gz (10 bytes)", msg)
        self.assertNotIn("missing", msg)

    def test_partial_collect_names_what_is_missing(self):
        mon = FakeMonitor(frames=[1, 2], on_type=lambda _: self.guest_posts("stdout"))
        msg = driver.stall_collect(mon, out=self.out, collect="/tmp/results", timeout=0)
        self.assertIn("got stdout", msg)
        self.assertIn("missing collect.tar.gz", msg)

    def test_stale_files_from_before_the_stall_do_not_count(self):
        self.guest_posts("stdout")  # left over; must not be reported as new
        mon = FakeMonitor(frames=[1, 2])
        msg = driver.stall_collect(mon, out=self.out, collect="", timeout=0)
        self.assertIn("nothing arrived", msg)

    def test_no_second_terminal_means_no_typing(self):
        mon = FakeMonitor(frames=[7])  # screen never changes
        msg = driver.stall_collect(mon, out=self.out, collect="", timeout=0, open_timeout=0)
        self.assertEqual(mon.typed, [])
        self.assertIn("did not open", msg)

    def test_dead_monitor_is_reported_not_raised(self):
        class Dead(FakeMonitor):
            def screendump(self, path=None):
                raise OSError("monitor.sock gone")
        msg = driver.stall_collect(Dead(frames=[1]), out=self.out, collect="", timeout=0)
        self.assertIn("could not type", msg)


class WaitForRc(Base):
    def paths(self):
        return (os.path.join(self.out, "rc"), os.path.join(self.out, "heartbeat"))

    def test_stall_screendumps_then_collects_and_returns_stalled(self):
        rc, hb = self.paths()
        mon = FakeMonitor(frames=[1])
        with mock.patch.object(driver, "stall_collect",
                               return_value="stall-collect: got stdout") as sc:
            status = driver.wait_for_rc(mon, rc, hb, lambda: "", out=self.out,
                                        run_timeout=60, no_progress_timeout=0, poll=0)
        self.assertEqual(status, "stalled")
        self.assertEqual(mon.dumps, [os.path.join(self.out, "fail.ppm")])
        sc.assert_called_once_with(mon, out=self.out)
        self.assertFalse(os.path.exists(rc))

    def test_rc_arrival_wins(self):
        rc, hb = self.paths()
        self.guest_posts("rc")
        status = driver.wait_for_rc(FakeMonitor([1]), rc, hb, lambda: "", out=self.out,
                                    run_timeout=60, no_progress_timeout=0, poll=0)
        self.assertEqual(status, "rc")

    def test_run_timeout_without_stall_is_timeout_not_stalled(self):
        rc, hb = self.paths()
        with mock.patch.object(driver, "stall_collect") as sc:
            status = driver.wait_for_rc(FakeMonitor([1]), rc, hb, lambda: "", out=self.out,
                                        run_timeout=0, no_progress_timeout=900, poll=0)
        self.assertEqual(status, "timeout")
        sc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
