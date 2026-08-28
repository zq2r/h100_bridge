#!/usr/bin/env python3

import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
import traceback

BASE = Path(
    os.environ["H100_BRIDGE"]
).expanduser().resolve()

REQUESTS = BASE / "requests"

PROCESSING = (
    REQUESTS / "processing"
)

SESSIONS = BASE / "sessions"

HEARTBEAT = (
    BASE / "supervisor_heartbeat"
)

PIDFILE = (
    BASE / "supervisor.pid"
)

WORKER_SCRIPT = (
    BASE / "h100_worker.py"
)

SESSION_RE = re.compile(
    r"^[A-Za-z0-9_-]+$"
)

os.umask(0o077)

for p in (
    BASE,
    REQUESTS,
    PROCESSING,
    SESSIONS,
):
    p.mkdir(
        parents=True,
        exist_ok=True,
    )


def atomic_write(
    path,
    content,
):

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.parent / (
        f".{path.name}.tmp."
        f"{os.getpid()}"
    )

    tmp.write_text(
        str(content)
    )

    os.replace(
        tmp,
        path,
    )


# =========================================
# Only one supervisor
# =========================================

def acquire_supervisor_lock():

    key = hashlib.sha256(
        str(BASE).encode()
    ).hexdigest()[:12]

    lock_path = (
        f"/tmp/"
        f"h100_bridge_{key}_"
        f"supervisor.lock"
    )

    fd = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR,
        0o600,
    )

    try:

        fcntl.flock(
            fd,
            fcntl.LOCK_EX
            | fcntl.LOCK_NB,
        )

    except BlockingIOError:

        print(
            "Another H100 supervisor "
            "is already running.",
            flush=True,
        )

        raise SystemExit(1)

    return fd


# =========================================
# Worker check
# =========================================

def worker_pid(root):

    try:
        return int(
            (
                root
                / "state"
                / "worker_pid"
            ).read_text().strip()
        )

    except Exception:
        return None


def pid_matches_worker(
    pid,
    sid,
):

    if pid is None:
        return False

    try:

        os.kill(
            pid,
            0,
        )

    except OSError:
        return False

    # We're on H100, so /proc refers
    # to the actual worker machine.
    try:

        cmdline = (
            Path(
                f"/proc/{pid}/cmdline"
            )
            .read_bytes()
            .replace(
                b"\x00",
                b" ",
            )
            .decode(
                errors="ignore"
            )
        )

        return (
            "h100_worker.py"
            in cmdline
            and sid in cmdline
        )

    except Exception:

        # fallback:
        # recent heartbeat + existing PID
        heartbeat = (
            SESSIONS
            / sid
            / "heartbeat"
        )

        try:

            timestamp = float(
                heartbeat
                .read_text()
                .strip()
            )

            return (
                time.time()
                - timestamp
                < 10
            )

        except Exception:
            return False


def worker_alive(sid):

    root = (
        SESSIONS / sid
    )

    return pid_matches_worker(
        worker_pid(root),
        sid,
    )


# =========================================
# Recover requests after supervisor crash
# =========================================

def recover_processing_requests():

    for old in (
        PROCESSING.glob(
            "*.json"
        )
    ):

        target = (
            REQUESTS
            / old.name
        )

        if target.exists():

            old.unlink(
                missing_ok=True
            )

        else:

            os.replace(
                old,
                target,
            )


# =========================================
# Launch independent worker
# =========================================

def launch_worker(
    sid,
    start_dir,
):

    root = (
        SESSIONS / sid
    )
    
    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    (root / "state").mkdir(
        parents=True,
        exist_ok=True,
    )

    log = (
        root / "worker.log"
    )

    args = [
        sys.executable,
        str(WORKER_SCRIPT),
        "--session",
        sid,
    ]

    if start_dir:

        args.extend(
            [
                "--start-dir",
                start_dir,
            ]
        )

    env = os.environ.copy()

    env[
        "H100_BRIDGE"
    ] = str(BASE)

    log_fd = open(
        log,
        "ab",
        buffering=0,
    )

    # 关键：
    #
    # start_new_session=True
    #
    # worker 自己成为新的 session leader，
    # 和 supervisor 的进程组/terminal 完全脱离。
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        env=env,
        close_fds=True,
        start_new_session=True,
    )

    log_fd.close()

    atomic_write(
        root
        / "state"
        / "launched_pid",
        process.pid,
    )

    print(
        f"Launched worker: "
        f"session={sid}, "
        f"pid={process.pid}",
        flush=True,
    )


# =========================================
# Request
# =========================================

def process_request(path):

    # Atomic claim
    claimed = (
        PROCESSING / path.name
    )

    try:

        os.replace(
            path,
            claimed,
        )

    except FileNotFoundError:
        return

    try:

        request = json.loads(
            claimed.read_text()
        )

        sid = request.get(
            "session_id"
        )

        start_dir = request.get(
            "start_dir"
        )

        if (
            not sid
            or not SESSION_RE.match(
                sid
            )
        ):

            print(
                f"Invalid session request: "
                f"{claimed}",
                flush=True,
            )

            return

        # Supervisor 重启期间，
        # 有可能 request 被重复处理。
        #
        # 已有 worker 就不重复启动。
        if worker_alive(sid):

            print(
                f"Worker already alive: "
                f"{sid}",
                flush=True,
            )

            return

        launch_worker(
            sid,
            start_dir,
        )

    finally:

        claimed.unlink(
            missing_ok=True
        )


# =========================================
# Main
# =========================================

def main():

    lock_fd = (
        acquire_supervisor_lock()
    )

    atomic_write(
        PIDFILE,
        os.getpid(),
    )

    recover_processing_requests()

    print(
        f"H100 supervisor started "
        f"on {os.uname().nodename}",
        flush=True,
    )

    print(
        f"PID: {os.getpid()}",
        flush=True,
    )

    print(
        f"Bridge: {BASE}",
        flush=True,
    )

    try:

        while True:

            atomic_write(
                HEARTBEAT,
                time.time(),
            )

            requests = sorted(
                REQUESTS.glob(
                    "*.json"
                ),
                key=lambda p:
                    p.stat().st_mtime_ns,
            )

            for path in requests:

                try:
                    process_request(
                        path
                    )

                except Exception:
                    print(
                        f"Failed to process request: "
                        f"{path}",
                        flush=True,
                    )

                    traceback.print_exc()

            time.sleep(0.1)

    finally:

        try:

            current = int(
                PIDFILE
                .read_text()
                .strip()
            )

            if current == os.getpid():
                PIDFILE.unlink(
                    missing_ok=True
                )

        except Exception:
            pass

        os.close(lock_fd)


if __name__ == "__main__":
    main()