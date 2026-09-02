#!/usr/bin/env bash
# Save / restore the whole running VM (RAM included) to a host volume, so the
# guest comes up in seconds instead of re-booting macOS.
#
#   ./snapshot.sh save     # freeze the running guest into /state/vm.state
#   ./snapshot.sh restore  # recreate the container resumed from that state
#
# Requires: `+invtsc` REMOVED from the -cpu line (it marks the vCPU
# non-migratable and QEMU refuses both `migrate` and `savevm` while it is set),
# and a directory bind-mounted at /state.
set -euo pipefail
C="${CONTAINER:-macos-x64}"
D="${STATE_DIR:-$PWD/state}"
IMG="${IMAGE:-etasdemir/osx-container:ventura}"

mon() { docker exec "$C" python3 /tmp/mon.py "$@"; }

case "${1:-}" in
save)
  docker cp "$(dirname "$0")/mon.py" "$C:/tmp/mon.py" >/dev/null
  mon "migrate_set_speed 0" >/dev/null
  # The URI contains a space, so HMP needs it quoted -- unquoted you get
  # "migrate: extraneous characters at the end of line" and no file.
  docker exec "$C" python3 -c '
import socket,time
s=socket.socket(socket.AF_UNIX); s.connect("/home/user/OSX-KVM/monitor.sock")
s.settimeout(5); time.sleep(0.3)
try: s.recv(65536)
except Exception: pass
s.sendall(b"migrate \"exec:cat > /state/vm.state\"\n"); time.sleep(2)
'
  echo -n "saving"
  for _ in $(seq 1 90); do
    st=$(docker exec "$C" python3 -c "
import socket,time,re
s=socket.socket(socket.AF_UNIX); s.connect('/home/user/OSX-KVM/monitor.sock')
s.settimeout(5); time.sleep(0.2)
try: s.recv(65536)
except Exception: pass
s.sendall(b'info migrate\n'); time.sleep(1.0)
d=s.recv(200000).decode(errors='replace')
m=re.search(r'Migration status: (\w+)',d); print(m.group(1) if m else 'none')" 2>/dev/null | tail -1)
    echo -n "."
    [ "$st" = completed ] && { echo " done ($(du -h "$D/vm.state" | cut -f1))"; exit 0; }
    [ "$st" = failed ] && { echo " FAILED"; exit 1; }
    sleep 3
  done
  echo " timed out"; exit 1 ;;
restore)
  [ -f "$D/vm.state" ] || { echo "no $D/vm.state -- run 'save' first"; exit 1; }
  docker rm -f "$C" >/dev/null 2>&1 || true
  docker run -d --name "$C" \
    --device /dev/kvm --group-add "$(stat -c %g /dev/kvm)" \
    -p 50922:10022 -p 5900:5900 \
    -e RAM=8 -e CORES=2 -e THREADS=4 -e DISPLAY_MODE=vnc \
    -e INCOMING="exec:cat /state/vm.state" \
    -v "$PWD/Launch.sh:/home/user/OSX-KVM/Launch.sh:ro" \
    -v "$D/../disk/mac_hdd_ng.img:/home/user/OSX-KVM/mac_hdd_ng.img" \
    -v "$D:/state" "$IMG" >/dev/null
  echo "restored -- guest resumes exactly where it was saved" ;;
*)
  echo "usage: $0 save|restore"; exit 2 ;;
esac
