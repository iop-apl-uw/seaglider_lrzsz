#!/usr/bin/env python3
"""Hang up a live glider call part way through a file upload, then record what lrz leaves behind.

Watches <mission>/comm.log. Once lrz logs the chosen sector of the target file, it issues
TIOCVHANGUP on the tty of the receiving lrz/lrb. That takes the same kernel path (__tty_hangup) as
the serial driver losing DCD, so the login shell and lrz see a real call drop. It only ever hangs up
a /dev/ttyS* line; anything else aborts without touching it.

Every file of the target's dive and type (sg0100d* for sg0100dz.x) is removed when the tool arms,
once no call is in progress: the upload, any partials from earlier runs and what Base.py built from
them. A leftover copy of the upload could hide the 0600 problem this is testing for. --dry-run only
lists them.

On a plain hangup lrz normally sees read() fail, burns through its retries in milliseconds and takes
its protocol-error path before any SIGHUP reaches it. --signal-first sends lrz SIGHUP just before
the hangup, to force the other ordering (the path that leaves the plain fragment name on an old lrz).

Must be run as root (TIOCVHANGUP needs CAP_SYS_ADMIN, and reading lrz's /proc/<pid>/fd too).

Usage: sudo python3 seaglider_drop_call.py --mission <mission_dir> \\
           --user <glider_account> --target sg0100dz.x --out /tmp/drop.txt
"""

import argparse
import fcntl
import glob
import os
import re
import signal
import stat
import subprocess
import sys
import time

TIOCVHANGUP = 0x5437


def now() -> str:
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def find_lrz(user: str) -> list[tuple[int, str]]:
    r = subprocess.run(["ps", "-u", user, "-o", "pid=,comm="], capture_output=True, text=True)
    found = []
    for line in r.stdout.splitlines():
        pid, comm = line.split(None, 1)
        if comm.strip() in ("lrz", "lrb", "lrx"):
            found.append((int(pid), comm.strip()))
    return found


def snapshot(mission: str, target: str, user: str) -> tuple:
    files = []
    for path in sorted(glob.glob(os.path.join(glob.escape(mission), glob.escape(target) + "*"))):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            continue
        files.append((os.path.basename(path), oct(stat.S_IMODE(st.st_mode)), st.st_size, st.st_uid))
    return tuple(files), tuple(find_lrz(user))


def dive_file_glob(target: str) -> str | None:
    """Glob for the target's dive and type, or None if the target isn't a glider upload name."""
    m = re.match(r"sg\d{4}[a-z]", target)
    return m.group(0) + "*" if m else None


def clear_dive_files(mission: str, pattern: str, dry_run: bool) -> list[str]:
    """Remove the regular files in mission matching pattern; return the names found."""
    names = []
    for path in sorted(glob.glob(os.path.join(glob.escape(mission), pattern))):
        if not os.path.isfile(path):
            continue
        if not dry_run:
            os.unlink(path)
        names.append(os.path.basename(path))
    return names


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mission", required=True)
    ap.add_argument("--user", required=True, help="glider login account")
    ap.add_argument("--target", required=True, help="uploaded file to interrupt, e.g. sg0100dz.x")
    ap.add_argument("--sector", type=int, default=1, help="hang up after this sector is logged")
    ap.add_argument("--wait", type=int, default=3600, help="seconds to wait for the upload")
    ap.add_argument("--observe", type=int, default=120, help="seconds to record after the hangup")
    ap.add_argument("--out", help="also append the report to this file")
    ap.add_argument("--dry-run", action="store_true", help="find the tty, but do not hang up")
    ap.add_argument(
        "--signal-first", action="store_true", help="send lrz SIGHUP just before the hangup"
    )
    args = ap.parse_args()

    outf = open(args.out, "a", encoding="utf-8") if args.out else None

    def say(msg: str) -> None:
        line = f"{now()} {msg}"
        print(line, flush=True)
        if outf:
            outf.write(line + "\n")
            outf.flush()

    if os.geteuid() != 0 and not args.dry_run:
        say("must be run as root")
        return 2

    pattern = dive_file_glob(args.target)
    if pattern is None:
        say(f"{args.target} doesn't look like sgNNNNx...; refusing to clear files for it")
        return 2

    comm = os.path.join(args.mission, "comm.log")
    deadline = time.time() + args.wait

    # Don't pull files out from under a call already in progress
    connected = os.path.join(args.mission, ".connected")
    while os.path.exists(connected):
        if time.time() > deadline:
            say("timed out waiting for the current call to end (.connected still present)")
            return 1
        time.sleep(0.5)

    verb = "would remove" if args.dry_run else "removed"
    for name in clear_dive_files(args.mission, pattern, args.dry_run):
        say(f"{verb} {name}")

    with open(comm, "rb") as f:
        f.seek(0, os.SEEK_END)
        start_offset = f.tell()
    say(f"armed: waiting for sector {args.sector} of {args.target}")

    receiving = re.compile(rb"Receiving \d+ bytes of " + re.escape(args.target.encode()) + rb"\s*$")
    received = re.compile(rb"Received \d+ bytes of " + re.escape(args.target.encode()) + rb"\b")
    sector = re.compile(rb"sector number = " + str(args.sector).encode() + rb",")

    in_target = False
    pending = b""
    offset = start_offset
    triggered = False
    while not triggered:
        if time.time() > deadline:
            say("timed out waiting for the upload")
            return 1
        with open(comm, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            offset = f.tell()
        pending += chunk
        *lines, pending = pending.split(b"\n")
        for line in lines:
            if receiving.search(line):
                in_target = True
                say(f"saw: {line.decode(errors='replace')}")
            elif in_target and received.search(line):
                say("MISSED: the file finished before the trigger; no hangup")
                return 1
            elif in_target and sector.search(line):
                say(f"saw: {line.decode(errors='replace')}")
                triggered = True
                break
        if not triggered:
            time.sleep(0.02)

    procs = find_lrz(args.user)
    if len(procs) != 1:
        say(f"expected one lrz for {args.user}, found {procs}; no hangup")
        return 1
    pid, name = procs[0]
    try:
        tty = os.readlink(f"/proc/{pid}/fd/0")
    except OSError as e:
        say(f"cannot read tty of {name} pid {pid}: {e}; no hangup")
        return 1
    say(f"{name} pid {pid} is on {tty}")
    if not re.fullmatch(r"/dev/ttyS\d+", tty):
        say("not a /dev/ttyS* line; no hangup")
        if not args.dry_run:
            return 1

    if args.dry_run:
        say("dry run: not hanging up")
    else:
        fd = os.open(tty, os.O_RDWR | os.O_NONBLOCK | os.O_NOCTTY)
        if args.signal_first:
            os.kill(pid, signal.SIGHUP)
            say(f"SIGHUP sent to {name} pid {pid}")
            time.sleep(0.05)
        try:
            fcntl.ioctl(fd, TIOCVHANGUP)
        finally:
            os.close(fd)
        say(f"HANGUP issued on {tty}")

    # Record every change: fast at first, because what matters most is the state of the file
    # at the moment the logout script starts Base.py
    t0 = time.time()
    last = None
    while time.time() - t0 < args.observe:
        snap = snapshot(args.mission, args.target, args.user)
        if snap != last:
            say(f"T+{time.time() - t0:6.2f}s files={list(snap[0])} lrz={list(snap[1])}")
            last = snap
        time.sleep(0.05 if time.time() - t0 < 30 else 0.5)

    with open(comm, "rb") as f:
        f.seek(start_offset)
        say("--- comm.log since armed")
        for line in f.read().decode(errors="replace").splitlines():
            say(line)

    say("--- baselogs since the hangup mentioning permissions or the target")
    stem = args.target.split(".")[0]
    for path in sorted(glob.glob(os.path.join(glob.escape(args.mission), "baselog_*"))):
        if os.path.getmtime(path) < t0:
            continue
        say(f"# {os.path.basename(path)}")
        with open(path, errors="replace") as f:
            for line in f:
                if "file permissions" in line or stem in line:
                    say(line.rstrip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
