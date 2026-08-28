#!/usr/bin/env python3

import argparse
import fcntl
import hashlib
import os
import pty
import shlex
import signal
import termios
import threading
import time
from pathlib import Path


BASE = Path(
    os.environ["H100_BRIDGE"]
).expanduser().resolve()


def atomic_write(path, content):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.parent / (
        f".{path.name}.tmp."
        f"{os.getpid()}"
    )

    tmp.write_text(str(content))

    os.replace(tmp, path)


class Worker:

    def __init__(self, sid, start_dir):

        self.sid = sid

        self.root = BASE / "sessions" / sid

        self.queue = self.root / "queue"
        self.running = self.root / "running"
        self.done = self.root / "done"
        self.control = self.root / "control"
        self.state = self.root / "state"

        self.stream = self.root / "stream.log"
        self.ready = self.root / "ready"
        self.heartbeat = self.root / "heartbeat"

        for p in (
            self.root,
            self.queue,
            self.running,
            self.done,
            self.control,
            self.state,
        ):
            p.mkdir(
                parents=True,
                exist_ok=True,
            )

        self.stream.touch(exist_ok=True)

        if start_dir:
            candidate = (
                Path(start_dir)
                .expanduser()
                .resolve()
            )

            if candidate.is_dir():
                self.start_dir = candidate
            else:
                self.start_dir = Path.home()

        else:
            self.start_dir = Path.home()

        self.shell_pid = None
        self.master_fd = None

        self.stop_requested = False

        self.lock_fd = None

    # =========================================
    # Local worker lock
    # =========================================

    def acquire_lock(self):

        key = hashlib.sha256(
            str(BASE).encode()
        ).hexdigest()[:12]

        lock_path = (
            f"/tmp/"
            f"h100_bridge_{key}_"
            f"{self.sid}.lock"
        )

        self.lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )

        try:
            fcntl.flock(
                self.lock_fd,
                fcntl.LOCK_EX
                | fcntl.LOCK_NB,
            )

        except BlockingIOError:

            print(
                f"Worker already exists "
                f"for session {self.sid}",
                flush=True,
            )

            raise SystemExit(0)

    # =========================================
    # State
    # =========================================

    def set_state(self, name, value):

        atomic_write(
            self.state / name,
            value,
        )

    def update_heartbeat(self):

        atomic_write(
            self.heartbeat,
            time.time(),
        )

    # =========================================
    # PTY / shell
    # =========================================

    def send(self, text):

        os.write(
            self.master_fd,
            text.encode(),
        )

    def start_shell(self):

        pid, master_fd = pty.fork()

        if pid == 0:

            os.chdir(
                self.start_dir
            )

            env = os.environ.copy()

            env[
                "H100_BRIDGE_SESSION"
            ] = str(self.root)

            env[
                "PYTHONUNBUFFERED"
            ] = "1"

            os.execvpe(
                "/bin/bash",
                [
                    "bash",
                    "--noprofile",
                    "--norc",
                    "-i",
                ],
                env,
            )

        self.shell_pid = pid
        self.master_fd = master_fd

        # 不让 bash 把收到的命令原样 echo
        attrs = termios.tcgetattr(
            master_fd
        )

        attrs[3] &= ~termios.ECHO

        termios.tcsetattr(
            master_fd,
            termios.TCSANOW,
            attrs,
        )

        self.set_state(
            "worker_pid",
            os.getpid(),
        )

        self.set_state(
            "shell_pid",
            self.shell_pid,
        )

        threading.Thread(
            target=self.output_loop,
            daemon=True,
        ).start()

        # 初始化 persistent shell
        self.send(
            "source ~/.bashrc "
            ">/dev/null 2>&1 || true; "
            "export PS1=''; "
            "export PS2=''; "
            "export PYTHONUNBUFFERED=1; "

            "printf '%s\\n' \"$PWD\" "
            "> \"$H100_BRIDGE_SESSION/state/pwd\"; "

            "printf '%s\\n' "
            "\"${CONDA_DEFAULT_ENV-}\" "
            "> \"$H100_BRIDGE_SESSION/state/conda\"; "

            "printf '%s\\n' \"$HOSTNAME\" "
            "> \"$H100_BRIDGE_SESSION/state/hostname\"; "

            "touch "
            "\"$H100_BRIDGE_SESSION/state/initialized\"\n"
        )

        initialized = (
            self.state / "initialized"
        )

        deadline = (
            time.time() + 10
        )

        while (
            not initialized.exists()
            and time.time() < deadline
        ):
            if not self.shell_alive():
                raise RuntimeError(
                    "bash exited during initialization"
                )

            time.sleep(0.05)

        if not initialized.exists():
            raise RuntimeError(
                "bash initialization timeout"
            )

    def output_loop(self):

        fd = os.open(
            self.stream,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_APPEND,
            0o600,
        )

        try:

            while True:

                try:
                    data = os.read(
                        self.master_fd,
                        65536,
                    )

                except OSError:
                    return

                if not data:
                    return

                os.write(
                    fd,
                    data,
                )

        finally:

            os.close(fd)

    def shell_alive(self):

        if self.shell_pid is None:
            return False

        try:

            pid, _ = os.waitpid(
                self.shell_pid,
                os.WNOHANG,
            )

        except ChildProcessError:
            return False

        return pid == 0

    # =========================================
    # Kill whole session
    # =========================================

    def terminate_shell(self):

        if self.master_fd is not None:

            # 先尝试终止 foreground job
            try:
                pgid = os.tcgetpgrp(
                    self.master_fd
                )

                if pgid > 0:
                    os.killpg(
                        pgid,
                        signal.SIGTERM,
                    )

            except Exception:
                pass

            try:
                os.write(
                    self.master_fd,
                    b"\x03",
                )
            except Exception:
                pass

        time.sleep(0.2)

        if self.shell_pid is not None:

            try:
                pgid = os.getpgid(
                    self.shell_pid
                )

                os.killpg(
                    pgid,
                    signal.SIGHUP,
                )

            except Exception:
                pass

        if self.master_fd is not None:

            try:
                os.close(
                    self.master_fd
                )
            except OSError:
                pass

            self.master_fd = None

    # =========================================
    # Ctrl+C
    # =========================================

    def handle_interrupts(
        self,
        jid,
    ):

        pattern = (
            f"interrupt.{jid}.*"
        )

        for request in (
            self.control.glob(pattern)
        ):

            try:

                os.write(
                    self.master_fd,
                    b"\x03",
                )

            except OSError:
                pass

            try:
                request.unlink()
            except FileNotFoundError:
                pass

    # =========================================
    # Job
    # =========================================

    def execute_job(self, job):

        jid = job.stem

        running = (
            self.running
            / job.name
        )

        try:

            os.replace(
                job,
                running,
            )

        except FileNotFoundError:
            return

        rc_file = (
            self.done
            / f"{jid}.rc"
        )

        try:
            offset = (
                self.stream
                .stat()
                .st_size
            )
        except OSError:
            offset = 0

        self.set_state(
            "status",
            "running",
        )

        self.set_state(
            "current_job",
            jid,
        )

        self.set_state(
            "current_job_offset",
            offset,
        )

        # source 而不是 bash xxx.cmd
        #
        # 所以：
        # cd
        # export
        # conda activate
        #
        # 都修改当前 persistent shell。
        wrapper = (

            "set +e; "

            f"source "
            f"{shlex.quote(str(running))}; "

            "__h100_rc=$?; "

            "printf '%s\\n' \"$PWD\" "
            "> \"$H100_BRIDGE_SESSION/state/pwd\"; "

            "printf '%s\\n' "
            "\"${CONDA_DEFAULT_ENV-}\" "
            "> \"$H100_BRIDGE_SESSION/state/conda\"; "

            "export PS1=''; "
            "export PS2=''; "

            f"printf '\\036H100_DONE:"
            f"{jid}:%s\\037\\n' "
            "\"$__h100_rc\"; "

            f"printf '%s\\n' "
            "\"$__h100_rc\" "
            f"> \"$H100_BRIDGE_SESSION/"
            f"done/{jid}.rc\"\n"
        )

        self.send(wrapper)

        while not rc_file.exists():

            self.update_heartbeat()

            self.handle_interrupts(
                jid
            )

            if (
                self.control
                / "terminate"
            ).exists():

                self.stop_requested = True
                return

            if not self.shell_alive():

                self.set_state(
                    "status",
                    "dead",
                )

                return

            time.sleep(0.05)

        try:

            os.replace(
                running,
                self.done
                / f"{jid}.cmd",
            )

        except FileNotFoundError:
            pass

        self.set_state(
            "current_job",
            "",
        )

        self.set_state(
            "current_job_offset",
            "",
        )

        self.set_state(
            "status",
            "idle",
        )

    # =========================================
    # Worker lifetime
    # =========================================

    def run(self):

        self.acquire_lock()

        self.start_shell()

        self.set_state(
            "status",
            "idle",
        )

        self.update_heartbeat()

        atomic_write(
            self.ready,
            time.time(),
        )

        print(
            f"H100 worker started: "
            f"session={self.sid}, "
            f"worker_pid={os.getpid()}, "
            f"shell_pid={self.shell_pid}",
            flush=True,
        )

        while True:

            self.update_heartbeat()

            if (
                self.control
                / "terminate"
            ).exists():

                self.stop_requested = True

            if self.stop_requested:

                self.set_state(
                    "status",
                    "terminating",
                )

                self.terminate_shell()

                self.set_state(
                    "status",
                    "dead",
                )

                return

            if not self.shell_alive():

                self.set_state(
                    "status",
                    "dead",
                )

                return

            jobs = sorted(
                self.queue.glob(
                    "*.cmd"
                ),
                key=lambda p:
                    p.stat().st_mtime_ns,
            )

            if jobs:

                self.execute_job(
                    jobs[0]
                )

                continue

            time.sleep(0.05)


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--session",
        required=True,
    )

    parser.add_argument(
        "--start-dir",
        default=None,
    )

    args = parser.parse_args()

    worker = Worker(
        sid=args.session,
        start_dir=args.start_dir,
    )

    def stop_handler(
        signum,
        frame,
    ):
        worker.stop_requested = True

    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )

    try:

        worker.run()

    except Exception as exc:

        try:
            worker.set_state(
                "status",
                "error",
            )

            worker.set_state(
                "error",
                repr(exc),
            )

        finally:

            worker.terminate_shell()

        raise


if __name__ == "__main__":
    main()