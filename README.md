# docker-mac-x64

[![macOS x64 smoke test](https://github.com/zackees/docker-mac-x64/actions/workflows/macos-x64.yml/badge.svg)](https://github.com/zackees/docker-mac-x64/actions/workflows/macos-x64.yml)

Run an **x86_64 macOS** guest in Docker on a Linux host, and execute Intel-Mac
binaries in it — without owning an Intel Mac and without waiting on a
`macos-*-intel` CI runner.

Verified end to end on an **AMD Ryzen 7 3700X**: macOS Ventura Recovery boots,
and an `x86_64-apple-darwin` Rust binary runs in it and returns exit code 0.

```
-bash-3.2# uname -m
x86_64
-bash-3.2# /tmp/soldr --version; echo RC=$?
soldr 0.9.11
RC=0
```

Execution is **hardware virtualization via KVM, not emulation** — no Rosetta, no
QEMU TCG. The guest CPU is presented as an Intel Penryn with
`vendor=GenuineIntel`, which is exactly what lets macOS run on an AMD host: XNU
never takes its AMD code paths. The tradeoff is that Penryn is a 2008-era model,
so anything gated behind newer ISA features (AVX2, AVX-512) is not exercised.

## Quick start

```bash
docker run -d --name macos-x64 \
  --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
  -p 50922:10022 -p 5900:5900 \
  -e RAM=8 -e CORES=2 -e THREADS=4 -e DISPLAY_MODE=vnc \
  -v "$PWD/Launch.sh:/home/user/OSX-KVM/Launch.sh:ro" \
  -v "$PWD/disk/mac_hdd_ng.img:/home/user/OSX-KVM/mac_hdd_ng.img" \
  etasdemir/osx-container:ventura
```

Create the persistent disk first, so the install survives `docker rm`:

```bash
mkdir -p disk
docker run --rm -v "$PWD/disk:/disk" --entrypoint qemu-img \
  etasdemir/osx-container:ventura create -f qcow2 /disk/mac_hdd_ng.img 128G
```

Then drive it (see **Driving it headless**), or open the VNC console on `:5900`.

## Why this repo exists

It is a fixed `Launch.sh` plus tooling for
[etasdemir/osx-container](https://github.com/etasdemir/osx-container), which
wraps [kholia/OSX-KVM](https://github.com/kholia/OSX-KVM). The upstream launcher
does not work under `docker run -d`. Four things had to change.

### 1. Two display adapters (this is the one that matters)

Upstream sets **both** `-vga virtio` and `-device VGA,vgamem_mb=256`. OSX-KVM's
own `OpenCore-Boot.sh` sets only the latter. With two GOP-capable adapters,
OpenCore renders its boot picker and then wedges:

- consecutive `screendump`s are byte-identical — the framebuffer is frozen
- the picker's 45-second `Timeout` never fires
- `info registers` shows RIP looping in the DXE range (`0x7f4xxxxx`) at 100% CPU

This reads exactly like a kernel panic, but it is **entirely pre-XNU** — the
kernel was never reached. Fix: one `-device VGA,vgamem_mb=128`.

### 2. No ssh port forward

The upstream README documents `-p 50922:10022`, but its `-netdev user,id=net0`
has no `hostfwd`, so port 10022 forwards to nothing and ssh can never connect.
Fix: `hostfwd=tcp::10022-:22`.

### 3. `-monitor stdio` has no tty under `docker run -d`

Fix: a unix socket. This is also what makes the guest observable headlessly —
`screendump` over the monitor is the only way to see what a detached VM is doing.

### 4. 9p share aborts startup

`-fsdev local,path=/mnt/MacosShared` makes QEMU refuse to start if that path does
not exist in the container. Dropped; use the HTTP transfer below instead.

### Also: the upstream Dockerfile cannot build

`apt-get install -y qemu` — that metapackage was removed after Ubuntu 22.04 (it
is `qemu-system-x86` now) — and `fetch-macOS-v2.py` is run without `python3`
installed. `Dockerfile` here fixes both. The **prebuilt
`etasdemir/osx-container:ventura` image is the verified path**; see
[Verification status](#verification-status).

## What was *not* the problem

`kvm.ignore_msrs=0`. OSX-KVM's `kvm_amd.conf` asks for `ignore_msrs=1`, and it is
the standard advice for macOS-on-AMD, so it is a tempting diagnosis for a guest
that resets right after `HANDOFF TO XNU`. This host booted XNU with
`ignore_msrs` left at `N`. If your symptom is a frozen picker rather than a
post-handoff reset, check the display config first — the two failures look
similar from the outside and the MSR one requires root on the host.

## Driving it headless

Boot is not automatic: NVRAM lands in the UEFI Shell rather than OpenCore.

```
fs0:
\efi\boot\bootx64.efi     # start OpenCore
<right><enter>            # entry 2 = macOS Base System
```

`scripts/mon.py` sends QEMU monitor commands; `scripts/type.py` types strings and
special keys through `sendkey`.

```bash
docker exec macos-x64 python3 /tmp/mon.py "screendump /tmp/s.ppm"
docker exec macos-x64 python3 /tmp/type.py 'ls -l /tmp' @ret
```

**Progress signal:** watch `docker stats`. ~116 MB of RSS means you are still in
firmware; crossing ~2.8 GB means XNU is up. CPU dropping from 100% to single
digits means it reached an idle UI.

**The mouse does not work.** HMP `mouse_move` emits *relative* events, which the
absolute `usb-tablet` ignores, so the cursor cannot be steered. Navigate menus
with the keyboard: `sendkey ctrl-f2` focuses the macOS menu bar, then arrows and
`ret`. That is how `Utilities > Terminal` gets opened in Recovery.


## Fast resume: snapshot the whole VM to a volume

Booting macOS costs ~80 s and cannot be tuned away -- it is XNU startup, not
I/O, so a faster disk does not help. What *does* help is never booting: save the
running VM (RAM and all) to a file on a host volume and resume from it.

| | Time |
|---|---|
| Cold: `docker run` -> `soldr --version` output | ~165 s |
| Save running VM to volume (3.0 GB state file) | 28.8 s |
| **Restore: `docker run` -> VM running** | **6.2 s** |

Roughly **27x faster**, and the guest comes back *exactly* where it was -- same
Terminal, same scrollback, binary still in `/tmp`, ready for the next command.

```bash
./scripts/snapshot.sh save      # guest freezes into state/vm.state
./scripts/snapshot.sh restore   # back in ~6 s
```

### The catch: `+invtsc` blocks it

OSX-KVM's CPU line includes `+invtsc`, which marks the vCPU **non-migratable**.
QEMU then refuses both `migrate` and `savevm`:

```
Outgoing migration blocked:
  State blocked by non-migratable CPU device (invtsc flag)
```

`Launch.sh` here drops `+invtsc`. Ventura boots and runs fine without it (this
is the configuration all the timings above were measured on). `Launch.sh.invtsc`
keeps the original if you need it -- but with it you get no fast resume.

The guest clock is frozen at save time, so after a long suspend `date` is stale;
re-sync in the guest if anything you run cares.

Two other things that trip this up, both fixed here:

* The HMP URI contains a space, so it must be quoted. Unquoted you get
  `migrate: extraneous characters at the end of line`, no file, and a
  `Migration status` that never appears -- easy to misread as a slow save.
* `savevm` (rather than `migrate`) additionally requires every writable block
  device to support snapshots. `BaseSystem.img` is raw, and marking it
  read-only fails with `Block node is read-only` because `-device ide-hd`
  rejects a read-only node. `migrate exec:` sidesteps all of that.

### Cheaper alternative

`docker pause` / `docker unpause` resumes instantly with zero setup, but the
container must stay alive, it holds the full 8 GB of RAM, and it does not
survive `docker stop` or a host reboot. Use it for minutes-scale iteration;
use the snapshot for anything longer.

### A note on boot paths

A **fresh** container (`docker run`) goes straight to the OpenCore picker in
~3 s. A **restarted** one (`docker stop; docker start`) lands in the UEFI Shell
instead and needs the two-command handoff, because OpenCore has written NVRAM
into the container writable layer by then. Prefer `docker rm` + `docker run`, or
just use snapshots and never boot twice.


## Use as a GitHub Action

Run commands on a real x86_64 macOS guest from a stock `ubuntu-latest` runner —
no macOS runner minutes, no Apple hardware.

```yaml
- uses: zackees/docker-mac-x64@main
  id: macos
  with:
    share-dir: share          # files served to the guest at http://10.0.2.2:8000/<name>
    run: |
      curl -s -o /tmp/prog http://10.0.2.2:8000/prog
      chmod +x /tmp/prog
      /tmp/prog --version

- run: echo "guest rc=${{ steps.macos.outputs.exit-code }}"
```

| Input | Default | |
|---|---|---|
| `run` | *(required)* | shell script executed in the guest, as root |
| `share-dir` | `''` | directory served to the guest over HTTP |
| `image` | `etasdemir/osx-container:ventura` | |
| `ram` / `cores` / `threads` | `6` / `2` / `4` | |
| `free-disk-space` | `true` | reclaims ~20 GB before pulling |

Outputs: `exit-code`, `stdout`, and `workdir` (holds `results/` with the guest
output plus `final.ppm` / `fail.ppm` screendumps — upload it as an artifact,
it is your only view into a failed boot).

### How commands and results actually move

Not screen-scraping. The driver runs *inside* the container, which is the guest's
slirp host (`10.0.2.2`), and serves an HTTP endpoint. It types a short bootstrap
line into the Recovery Terminal; the guest then fetches the real script and
**POSTs stdout and the true exit code back**. The arrival of that POST is also
the oracle that the keystrokes landed in a shell at all — if it never arrives,
the driver reopens Terminal and retries.

The driver is stdlib-only and reads the framebuffer straight from QEMU's PPM
header, so the runner needs no ImageMagick and no `bc`.

### Failing fast when the keystrokes miss (issue #1)

The bootstrap is typed into a GUI Terminal, so it can miss. When it does, the
Recovery window still has focus — and its default button is **Restore from Time
Machine**, so a blind Enter launches the restore assistant and nothing runs. The
driver used to wait the entire `run-timeout` (3600 s) for a result that could
never come, then retry four more times with no new information: **68 minutes to
report "no result"**, with no screenshot to say why.

Three changes, in order of how much they help:

1. **Don't type unless Terminal actually opened.** Opening it repaints most of
   the screen; an unchanged frame after `⇧⌘T` means the shortcut did not
   register, so the driver skips typing entirely rather than firing Enter at
   whatever dialog has focus.
2. **A start marker.** The guest wrapper POSTs `/started` as its very first
   action, before anything that can fail. "Did the typed line reach a shell?"
   (`start-timeout`, 120 s, retryable) is now a different question from "is the
   payload still working?" (`run-timeout`) — the conflation of those two is the
   whole bug.
3. **A heartbeat.** A background loop POSTs the tail of the live output every
   20 s. It drives `no-progress-timeout` (900 s) so a wedged payload cannot sit
   out the full `run-timeout` in silence, and the tail is echoed into the driver
   log so a hang shows *which step* it stopped at.

Retries stop once the script has provably started — retyping a command at a
guest that is already half-way through a run only makes it worse. Every miss
writes a `.ppm` screendump to `<workdir>/results/`; upload that directory as an
artifact and a failure explains itself.

Measured against a guest where Terminal deliberately never opens:

| | before | after |
|---|---|---|
| time to report the failure | ~68 min (3600 s + 4x120 s) | **~7.5 min** (3x148 s) |
| diagnostics | none | 3 screendumps naming the window that ate the keys |

### Two things that make headless boot reliable

**Never key on framebuffer width alone.** It flips to 1920x1080 while the screen
is still fully black, *seconds* before OpenCore paints the picker. Arrow/Enter
sent into that gap go nowhere, the picker then auto-boots its default (the wrong
entry) after 45 s, and you get a black screen that looks like a kernel panic.
Measured signatures the driver uses instead:

| Screen | Width | Mean brightness |
|---|---|---|
| Black / not yet painted | any | `0.0000` |
| OpenCore picker, painted | 1920 | `~0.0121` |
| UEFI Shell | 1024 | `~0.0193` |
| Recovery desktop | 1024 | `0.09`–`0.13` |

**Never use RSS as a UI-ready signal.** It crosses any threshold well before
Recovery accepts input, so `⇧⌘T` lands in whatever dialog is up — twice it
opened "Restore from Time Machine" instead of Terminal. The driver waits for
three consecutive byte-identical desktop frames, then 15 s more.

Every miss is recoverable: the driver `system_reset`s and retries the whole
picker sequence up to 3 times. **This is not theoretical** — on the very first
CI run, boot attempt 1 typed the UEFI-Shell handoff and the picker never
painted; the reset-and-retry is the only reason the run went green:

```
[   14.0s] boot attempt 1
[   19.1s]   UEFI Shell -> handing off to OpenCore
[  255.5s]   no painted picker; resetting
[  261.6s] boot attempt 2
[  263.1s]   picker painted
[  268.2s]   highlight moved (attempt 1)
[  322.5s] Recovery desktop ready
[  371.5s] guest exit code 0
soldr 0.9.11
```

Measured on `ubuntu-latest` (4 vCPU, nested virt): ~6 min for the driver
including one failed attempt, ~9.5 min for the whole job. Locally on a Ryzen
7 3700X the same driver takes 154 s. Budget accordingly — do not tune timeouts
to local numbers.

## Running a Mac binary without installing macOS

Recovery ships a full userland, so `Utilities > Terminal` is enough to execute an
x86_64 Mach-O. A smoke test costs minutes instead of the 30-60 minute install.

Ventura does bind `en0` to `virtio-net-pci`, so slirp networking works:

```bash
# host/container side
docker cp ./my-binary macos-x64:/tmp/prog
docker exec -d macos-x64 python3 -m http.server 8000 --directory /tmp
```

```bash
# guest side — 10.0.2.2 is slirp's host end, i.e. the container
ipconfig set en0 DHCP
curl -s -o /tmp/prog http://10.0.2.2:8000/prog
chmod +x /tmp/prog
/tmp/prog --version; echo RC=$?
```

Recovery is **not** a full install: `/tmp` is a ramdisk, there is no home
directory, no sshd, and nothing survives reboot. It proves a binary *runs*. For
repeated ssh-driven test runs, do the real install and snapshot the disk.

### Gotcha when scripting `type.py`

Quote the guest command in **single** quotes. With double quotes your *host*
shell expands `$?` before the keystrokes are sent, and you get a hardcoded
literal that looks exactly like a passing exit code:

```bash
docker exec macos-x64 python3 /tmp/type.py "prog; echo RC=$?" @ret   # WRONG: types RC=0
docker exec macos-x64 python3 /tmp/type.py 'prog; echo RC=$?' @ret   # right
```

## Cross-compiling the binary on Linux

No Mac is needed to *produce* the binary either. For Rust, targeting
`x86_64-apple-darwin` with an Apple SDK yields a Mach-O directly on Linux.
Confirm you got one before shipping it into the guest:

```bash
od -A n -t x1 -N 8 ./my-binary
# cf fa ed fe 07 00 00 01  =  MH_MAGIC_64 + CPU_TYPE_X86_64
```

Check its dylib needs too — anything under `/usr/local` or `/opt` will not exist
in Recovery, though system frameworks (CoreFoundation, Security, IOKit,
libSystem) all do:

```bash
llvm-objdump --macho --dylibs-used ./my-binary
```

## Verification status

Being explicit, because "it works" claims about macOS-in-Docker age badly:

| Item | Status |
|---|---|
| Ventura Recovery boots to a desktop on AMD Ryzen 7 3700X | Verified |
| x86_64 Mach-O runs in Recovery, `RC=0`, `uname -m` = `x86_64` | Verified |
| Single-VGA fix unwedges the boot | Verified (was the actual blocker) |
| slirp networking + HTTP transfer into the guest | Verified |
| `hostfwd` ssh forward reaches a guest sshd | **Not verified** — needs a full install with Remote Login on |
| `Dockerfile` builds from scratch | **Not verified** — the prebuilt image is what was tested |
| Keyboard `bus=ehci.0` binding | **Not isolated** — changed together with the display fix |
| Headless driver: boot -> run -> real exit code, no OCR | Verified locally (153 s, rc=0) |
| Runs on a GitHub-hosted `ubuntu-latest` runner | See the badge above |
| Intel hosts | Only AMD Ryzen exercised locally; CI runners are Intel/EPYC |

## Requirements

- Linux host, x86_64, `/dev/kvm` exposed to containers
- Docker **engine** — Docker Desktop's LinuxKit VM does not expose `/dev/kvm`
- ~8 GB RAM for the guest, and disk for a 128 GB sparse qcow2
- AMD hosts work; no `ignore_msrs` change was needed here

## Credits

- [etasdemir/osx-container](https://github.com/etasdemir/osx-container) — the
  container image this builds on (MIT)
- [kholia/OSX-KVM](https://github.com/kholia/OSX-KVM) — OpenCore images, QEMU
  recipe, `fetch-macOS-v2.py`
- [sickcodes/Docker-OSX](https://github.com/sickcodes/Docker-OSX),
  [thenickdude/KVM-Opencore](https://github.com/thenickdude/KVM-Opencore)

## Legal

Apple's macOS license permits running macOS only on Apple-branded hardware. You
are responsible for your own compliance. No Apple software is redistributed
here — `fetch-macOS-v2.py` pulls the recovery image from Apple directly.
