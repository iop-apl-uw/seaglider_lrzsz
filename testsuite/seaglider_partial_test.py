"""Drive lsz -> lrz over pipes (throttled) and interrupt the transfer; report what lrz leaves behind.

Seaglider partial-file handling: an interrupted receive should leave <frag>.<UTC timestamp> at
mode 0644 - never the plain fragment name at 0600 (basestation processing may run under a different
account from the glider login).

Usage: python3 seaglider_partial_test.py <bindir> <workdir> <case>
  <bindir> holds lrz and lsz plus symlinks lrb -> lrz and lsb -> lsz (YMODEM, as the gliders use)
  case: ok          - two files, no interruption
        hup         - SIGHUP to lrz part way into the first file (call drop)
        term        - SIGTERM to lrz part way into the first file
        senderdie   - kill lsz part way into the first file (protocol error/timeout path)
        afterfirst  - kill lsz just after the first of two files completes (stale fout/Pathname)
"""

import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

bindir, work, case = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
send = work / "send"
recv = work / "recv"
for d in (send, recv):
    d.mkdir(parents=True, exist_ok=True)
    for f in d.iterdir():
        f.unlink()
names = ["sg0007dz.x01", "sg0007dz.x02"]
for i, n in enumerate(names):
    (send / n).write_bytes(bytes((j * 7 + i) & 0xFF for j in range(16384)))

# YMODEM like the gliders (lsb/lrb are lsz/lrz called as YMODEM)
lrz = subprocess.Popen([str(bindir / "lrb")], cwd=recv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
lsz = subprocess.Popen(
    [str(bindir / "lsb"), *names], cwd=send, stdin=subprocess.PIPE, stdout=subprocess.PIPE
)

sent = 0
RATE = 4000  # bytes/s lsz -> lrz, so a 16 KiB file takes ~4 s


def pump_s2r() -> None:
    global sent
    while True:
        b = lsz.stdout.read1(256)
        if not b:
            break
        try:
            lrz.stdin.write(b)
            lrz.stdin.flush()
        except (BrokenPipeError, ValueError):
            break
        sent += len(b)
        time.sleep(len(b) / RATE)
    try:
        lrz.stdin.close()
    except (BrokenPipeError, OSError):
        pass


def pump_r2s() -> None:
    while True:
        b = lrz.stdout.read1(256)
        if not b:
            break
        try:
            lsz.stdin.write(b)
            lsz.stdin.flush()
        except (BrokenPipeError, ValueError):
            break


threading.Thread(target=pump_s2r, daemon=True).start()
threading.Thread(target=pump_r2s, daemon=True).start()

if case in ("hup", "term", "senderdie"):
    while sent < 8000:
        time.sleep(0.05)
    if case == "hup":
        lrz.send_signal(signal.SIGHUP)
    elif case == "term":
        lrz.send_signal(signal.SIGTERM)
    else:
        lsz.kill()
elif case == "afterfirst":
    while not (recv / names[0]).exists() or (recv / names[0]).stat().st_mode & 0o044 == 0:
        time.sleep(0.02)  # first file chmod 644 = closeit() done
    time.sleep(0.3)
    lsz.kill()

try:
    lrz.wait(timeout=180)
except subprocess.TimeoutExpired:
    lrz.kill()
    print("lrz TIMEOUT")
lsz.kill()
print(f"case={case} lrz rc={lrz.returncode}")
for f in sorted(recv.iterdir()):
    st = f.stat()
    if f.name == "comm.log":  # lrz's syslog lines, written to ./comm.log
        continue
    ok = ""
    src = send / ".".join(f.name.split(".")[:2])
    if src.exists():
        data = f.read_bytes()
        ok = "identical" if data == src.read_bytes() else f"partial {len(data)} bytes"
    print(f"  {f.name:32s} mode {oct(stat.S_IMODE(st.st_mode))} {ok}")
