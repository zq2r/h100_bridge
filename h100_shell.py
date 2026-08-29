#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path
import readline
import select
import termios
import tty
from contextlib import contextmanager
import select
import termios
import tty

BASE = Path(
    os.environ["H100_BRIDGE"]
).expanduser().resolve()

REQUESTS = BASE / "requests"
SESSIONS = BASE / "sessions"
NAMES = BASE / "names"

SUPERVISOR_HEARTBEAT = (
    BASE / "supervisor_heartbeat"
)

NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
)
MAX_HISTORY_COMMANDS = 100

class DetachRequested(Exception):
    pass

# =========================================================
# Basic helpers
# =========================================================
# =========================================================
# Path completion
# =========================================================

def path_completion_matches(
    sid,
    text,
):
    """
    Complete paths using the filesystem visible
    to the h100-shell client.

    Relative paths are resolved against the real
    H100 working directory stored in state/pwd.
    """

    root = root_of(sid)

    pwd = read_text(
        root / "state" / "pwd",
        "",
    )

    if not pwd:
        return []

    # ~ expansion is intentionally not handled here.
    # The client machine's home may differ from H100.
    if text.startswith("~"):
        return []

    parent_text, prefix = os.path.split(
        text
    )

    if os.path.isabs(text):

        base = Path(
            parent_text or "/"
        )

    else:

        base = Path(pwd)

        if parent_text:
            base = (
                base / parent_text
            )

    try:
        entries = list(
            base.iterdir()
        )

    except (
        OSError,
        PermissionError,
    ):
        return []

    # Preserve the part already typed.
    #
    # foo/ba
    # -> display_parent = foo/
    #
    # /abc/ba
    # -> display_parent = /abc/
    if prefix:
        display_parent = (
            text[:-len(prefix)]
        )
    else:
        display_parent = text

    matches = []

    for entry in entries:

        name = entry.name

        # Like normal bash:
        # don't show hidden files unless
        # user already typed ".".
        if (
            name.startswith(".")
            and not prefix.startswith(".")
        ):
            continue

        if not name.startswith(prefix):
            continue

        candidate = (
            display_parent + name
        )

        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False

        if is_dir:
            candidate += "/"
        else:
            candidate += " "

        matches.append(
            candidate
        )

    return sorted(matches)


def make_path_completer(sid):

    cached_text = None
    cached_matches = []

    def completer(text, state):

        nonlocal cached_text
        nonlocal cached_matches

        if (
            state == 0
            or text != cached_text
        ):
            cached_text = text

            cached_matches = (
                path_completion_matches(
                    sid,
                    text,
                )
            )

        if state < len(
            cached_matches
        ):
            return cached_matches[state]

        return None

    return completer

def delete_session(ref):

    sid = resolve_ref(ref)

    root = root_of(sid)

    name = session_name(sid)

    # 还活着就先 kill
    if worker_alive(sid):

        print(
            f"Stopping session: "
            f"{name or sid}"
        )

        (
            root
            / "control"
            / "terminate"
        ).touch()

        deadline = (
            time.time() + 10
        )

        while (
            time.time() < deadline
        ):

            if not worker_alive(sid):
                break

            time.sleep(0.1)

        if worker_alive(sid):

            raise RuntimeError(
                f"Session {name or sid} "
                f"did not stop within 10 seconds."
            )

    # 删除名字映射
    if name:

        release_name(
            name,
            sid,
        )

    # 删除整个 session 目录
    if root.exists():

        shutil.rmtree(
            root,
            ignore_errors=False,
        )

    print(
        f"Deleted session: "
        f"{name or sid} ({sid})"
    )

def setup_readline(sid):
    root = root_of(sid)

    history_file = (
        root
        / "state"
        / "client_history"
    )

    history_file.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Normal shell-style line editing.
    readline.parse_and_bind(
        "set editing-mode emacs"
    )

    # Important:
    # keep a pasted multi-line block in the input buffer
    # instead of treating every pasted newline as Enter.
    readline.parse_and_bind(
        "set enable-bracketed-paste on"
    )
    
    # Path completion.
    #
    # Keep "/" inside the readline word so
    # "foo/bar<Tab>" is passed as a whole path.
    readline.set_completer_delims(
        " \t\n;|&()<>="
    )

    readline.set_completer(
        make_path_completer(sid)
    )

    readline.parse_and_bind(
        "tab: complete"
    )

    readline.parse_and_bind(
        '"\\e[A": previous-history'
    )

    readline.parse_and_bind(
        '"\\e[B": next-history'
    )

    readline.set_history_length(MAX_HISTORY_COMMANDS)

    # Each h100-shell client reloads the persistent
    # history belonging to this H100 session.
    readline.clear_history()

    if history_file.exists():
        try:
            with history_file.open(
                "r",
                encoding="utf-8",
            ) as f:

                for line in f:
                    line = line.rstrip("\n")

                    if not line:
                        continue

                    command = None

                    try:
                        record = json.loads(line)

                        # New format:
                        # {"command": "..."}
                        if (
                            isinstance(record, dict)
                            and isinstance(
                                record.get("command"),
                                str,
                            )
                        ):
                            command = record[
                                "command"
                            ]

                    except json.JSONDecodeError:
                        pass

                    # Backward compatibility with
                    # the old one-command-per-line format.
                    if command is None:
                        command = line

                    if command:
                        readline.add_history(
                            command
                        )

        except Exception as exc:
            print(
                f"[warning] failed to load history: {exc}",
                file=sys.stderr,
            )

    return history_file


def save_command_history(
    history_file,
    command,
):
    if not command.strip():
        return

    try:
        record = {
            "command": command,
        }

        new_line = json.dumps(
            record,
            ensure_ascii=False,
        )

        old_lines = []

        if history_file.exists():
            with history_file.open(
                "r",
                encoding="utf-8",
            ) as f:
                old_lines = [
                    line.rstrip("\n")
                    for line in f
                    if line.strip()
                ]

        # 保留最近 99 条 + 当前这一条
        lines = (
            old_lines[
                -(MAX_HISTORY_COMMANDS - 1):
            ]
            + [new_line]
        )

        tmp = (
            history_file.parent
            / (
                f".{history_file.name}.tmp."
                f"{os.getpid()}."
                f"{uuid.uuid4().hex[:8]}"
            )
        )

        with tmp.open(
            "w",
            encoding="utf-8",
        ) as f:
            for line in lines:
                f.write(
                    line + "\n"
                )

            f.flush()
            os.fsync(
                f.fileno()
            )

        os.replace(
            tmp,
            history_file,
        )

    except Exception as exc:
        print(
            f"[warning] failed to save history: {exc}",
            file=sys.stderr,
        )


def atomic_write(path, content):

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = path.parent / (
        f".{path.name}.tmp."
        f"{os.getpid()}."
        f"{uuid.uuid4().hex[:8]}"
    )

    tmp.write_text(str(content))

    os.replace(tmp, path)


def read_text(path, default=""):

    try:
        return path.read_text().strip()
    except OSError:
        return default


def heartbeat_age(path):

    try:
        timestamp = float(
            path.read_text().strip()
        )

        return time.time() - timestamp

    except Exception:
        return None


def supervisor_alive():

    age = heartbeat_age(
        SUPERVISOR_HEARTBEAT
    )

    return (
        age is not None
        and age < 10
    )


def root_of(sid):

    return SESSIONS / sid


def status_of(sid):

    return read_text(
        root_of(sid)
        / "state"
        / "status",
        "unknown",
    )


def worker_alive(sid):

    root = root_of(sid)

    age = heartbeat_age(
        root / "heartbeat"
    )

    status = status_of(sid)

    return (
        age is not None
        and age < 10
        and status not in {
            "dead",
            "error",
        }
    )


# =========================================================
# Session naming
# =========================================================

def validate_name(name):

    if not NAME_RE.match(name):

        raise ValueError(
            "Invalid session name. "
            "Use 1-32 characters: "
            "letters, numbers, '.', '_' or '-'."
        )


def name_path(name):

    return NAMES / name


def session_name(sid):

    return read_text(
        root_of(sid)
        / "meta"
        / "name",
        "",
    )


def reserve_name(name, sid):

    validate_name(name)

    NAMES.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = name_path(name)

    try:

        fd = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL,
            0o600,
        )

    except FileExistsError:

        existing = read_text(
            path,
            "",
        )

        raise RuntimeError(
            f"Session name '{name}' "
            f"is already used by {existing}."
        )

    try:

        os.write(
            fd,
            (sid + "\n").encode(),
        )

    finally:

        os.close(fd)


def release_name(name, sid):

    if not name:
        return

    path = name_path(name)

    try:

        current = (
            path.read_text()
            .strip()
        )

    except OSError:
        return

    # 避免误删已经被别的 session 占用的名字。
    if current == sid:

        try:
            path.unlink()
        except FileNotFoundError:
            pass


def set_session_name(sid, name):

    meta = (
        root_of(sid)
        / "meta"
    )

    meta.mkdir(
        parents=True,
        exist_ok=True,
    )

    atomic_write(
        meta / "name",
        name,
    )


def resolve_ref(ref):

    """
    支持：
      1. 完整 session ID
      2. session name
      3. 唯一 session ID 前缀
    """

    # Exact ID
    exact = root_of(ref)

    if exact.is_dir():
        return ref

    # Name
    path = name_path(ref)

    if path.exists():

        sid = read_text(
            path,
            "",
        )

        if sid and root_of(sid).is_dir():
            return sid

        raise RuntimeError(
            f"Name '{ref}' points to "
            f"a missing session."
        )

    # ID prefix
    if SESSIONS.exists():

        matches = [
            p.name
            for p in SESSIONS.iterdir()
            if (
                p.is_dir()
                and p.name.startswith(ref)
            )
        ]

        if len(matches) == 1:
            return matches[0]

        if len(matches) > 1:

            raise RuntimeError(
                f"Ambiguous session prefix "
                f"'{ref}': "
                + ", ".join(matches)
            )

    raise RuntimeError(
        f"Session not found: {ref}"
    )


# =========================================================
# Create
# =========================================================

def create_session(start_dir, name):

    if not supervisor_alive():

        raise RuntimeError(
            "H100 supervisor is not running. "
            "Existing sessions may still be "
            "used with --attach."
        )

    sid = (
        uuid.uuid4()
        .hex[:10]
    )

    root = root_of(sid)

    root.mkdir(
        parents=True,
        exist_ok=True,
    )

    reserved = False

    if name:

        reserve_name(
            name,
            sid,
        )

        reserved = True

        set_session_name(
            sid,
            name,
        )

    request = {
        "session_id": sid,
        "start_dir": start_dir,
    }

    REQUESTS.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = (
        REQUESTS
        / f".{sid}.tmp"
    )

    target = (
        REQUESTS
        / f"{sid}.json"
    )

    try:

        tmp.write_text(
            json.dumps(request)
        )

        os.replace(
            tmp,
            target,
        )

    except Exception:

        if reserved:

            release_name(
                name,
                sid,
            )

        raise

    deadline = (
        time.time() + 20
    )

    ready = (
        root / "ready"
    )

    while not ready.exists():

        status = status_of(sid)

        if status == "error":

            error = read_text(
                root
                / "state"
                / "error",
                "unknown worker error",
            )

            raise RuntimeError(
                f"Worker failed: {error}"
            )

        if time.time() > deadline:

            raise RuntimeError(
                f"Timed out creating "
                f"session {sid}. "
                f"Check {root / 'worker.log'}"
            )

        time.sleep(0.05)

    return sid


# =========================================================
# Rename
# =========================================================

def rename_session(ref, new_name):

    sid = resolve_ref(ref)

    validate_name(new_name)

    old_name = session_name(sid)

    if old_name == new_name:

        print(
            f"Session is already named "
            f"'{new_name}'."
        )

        return

    # 先占新名字，再放旧名字。
    # 避免 rename 中途失败导致名字丢失。
    reserve_name(
        new_name,
        sid,
    )

    try:

        set_session_name(
            sid,
            new_name,
        )

    except Exception:

        release_name(
            new_name,
            sid,
        )

        raise

    if old_name:

        release_name(
            old_name,
            sid,
        )

    print(
        f"{sid}: "
        f"{old_name or '(unnamed)'} "
        f"-> {new_name}"
    )


# =========================================================
# Prompt
# =========================================================

def prompt(sid):

    root = root_of(sid)

    pwd = read_text(
        root
        / "state"
        / "pwd",
        "?",
    )

    conda = read_text(
        root
        / "state"
        / "conda",
        "",
    )

    name = session_name(sid)

    label = (
        name
        if name
        else sid[:6]
    )

    env = (
        f"({conda}) "
        if conda
        else ""
    )
    
    display_pwd = (
        Path(pwd).name
        if pwd != "/"
        else "/"
    )

    return (
        f"{env}"
        f"H100[{label}]"
        f":{display_pwd}$ "
    )


# =========================================================
# Submit command
# =========================================================

def submit(sid, command):

    root = root_of(sid)

    jid = (
        f"{time.time_ns()}_"
        f"{uuid.uuid4().hex[:8]}"
    )

    stream = (
        root / "stream.log"
    )

    offset = (
        stream.stat().st_size
        if stream.exists()
        else 0
    )

    tmp = (
        root
        / f".{jid}.tmp"
    )

    target = (
        root
        / "queue"
        / f"{jid}.cmd"
    )

    tmp.write_text(
        command + "\n"
    )

    os.replace(
        tmp,
        target,
    )

    return jid, offset


# =========================================================
# Ctrl+C
# =========================================================

def interrupt(sid, jid):

    root = root_of(sid)

    token = (
        uuid.uuid4()
        .hex[:8]
    )

    (
        root
        / "control"
        / f"interrupt.{jid}.{token}"
    ).touch()
    
def ack_job_consumed(
    sid,
    jid,
):
    root = root_of(sid)

    try:
        (
            root
            / "control"
            / f"consumed.{jid}"
        ).touch()

    except OSError:
        pass


# =========================================================
# Wait / output
# =========================================================

def wait_for_job(
    sid,
    jid,
    offset,
):

    root = root_of(sid)

    stream = (
        root / "stream.log"
    )

    marker = (
        f"\x1eH100_DONE:"
        f"{jid}:"
    ).encode()

    end_marker = b"\x1f"

    keep = max(
        256,
        len(marker) + 64,
    )

    buffer = b""
    stdin_fd = None
    old_termios = None

    if sys.stdin.isatty():
        stdin_fd = sys.stdin.fileno()

        old_termios = termios.tcgetattr(
            stdin_fd
        )

        # 让 Ctrl+D 能被逐键读取。
        # Ctrl+C 的 signal 行为仍然保留。
        tty.setcbreak(
            stdin_fd
        )
    try:
        with stream.open(
            "rb",
            buffering=0,
        ) as f:

            f.seek(offset)

            while True:

                try:

                    # Ctrl+D = detach client
                    if stdin_fd is not None:

                        readable, _, _ = (
                            select.select(
                                [stdin_fd],
                                [],
                                [],
                                0,
                            )
                        )

                        if readable:

                            key = os.read(
                                stdin_fd,
                                1,
                            )

                            # Ctrl+D = 0x04
                            if key == b"\x04":
                                raise DetachRequested()

                    data = f.read(
                        65536
                    )

                except KeyboardInterrupt:

                    interrupt(
                        sid,
                        jid,
                    )

                    sys.stdout.write(
                        "^C\n"
                    )

                    sys.stdout.flush()

                    continue

                except KeyboardInterrupt:

                    interrupt(
                        sid,
                        jid,
                    )

                    sys.stdout.write(
                        "^C\n"
                    )

                    sys.stdout.flush()

                    continue

                if not data:

                    if not worker_alive(
                        sid
                    ):

                        print(
                            "\n[H100 worker "
                            "is no longer alive]"
                        )

                        return 1

                    try:

                        time.sleep(
                            0.03
                        )

                    except KeyboardInterrupt:

                        interrupt(
                            sid,
                            jid,
                        )

                        sys.stdout.write(
                            "^C\n"
                        )

                        sys.stdout.flush()

                    continue

                buffer += data

                pos = buffer.find(
                    marker
                )

                if pos < 0:

                    if len(buffer) > keep:

                        cut = (
                            len(buffer)
                            - keep
                        )

                        os.write(
                            sys.stdout.fileno(),
                            buffer[:cut],
                        )

                        buffer = (
                            buffer[cut:]
                        )

                    continue

                if pos > 0:

                    os.write(
                        sys.stdout.fileno(),
                        buffer[:pos],
                    )

                rest = buffer[
                    pos + len(marker):
                ]

                end = rest.find(
                    end_marker
                )

                if end < 0:

                    buffer = (
                        buffer[pos:]
                    )

                    continue

                try:
                    rc = int(
                        rest[:end]
                    )

                except ValueError:
                    rc = 1

                ack_job_consumed(
                    sid,
                    jid,
                )

                return rc
    finally:
        if (
            stdin_fd is not None
            and old_termios is not None
        ):
            termios.tcsetattr(
                stdin_fd,
                termios.TCSADRAIN,
                old_termios,
            )

# =========================================================
# List
# =========================================================

def list_sessions():

    if not SESSIONS.exists():

        print(
            "No H100 sessions."
        )

        return

    rows = []

    for root in sorted(
        SESSIONS.iterdir()
    ):

        if not root.is_dir():
            continue

        sid = root.name

        status = status_of(sid)

        name = session_name(sid)

        worker_pid = read_text(
            root
            / "state"
            / "worker_pid",
            "-",
        )

        conda = read_text(
            root
            / "state"
            / "conda",
            "-",
        )

        pwd = read_text(
            root
            / "state"
            / "pwd",
            "?",
        )

        age = heartbeat_age(
            root / "heartbeat"
        )

        if age is None:

            age_text = "-"

        elif age < 10:

            age_text = (
                f"{age:.1f}s"
            )

        else:

            age_text = "STALE"

        rows.append(
            (
                name or "-",
                sid,
                status,
                worker_pid,
                conda or "-",
                age_text,
                pwd,
            )
        )

    if not rows:

        print(
            "No H100 sessions."
        )

        return

    print(
        f"{'NAME':<16} "
        f"{'SESSION':<12} "
        f"{'STATUS':<12} "
        f"{'WORKER':<9} "
        f"{'ENV':<16} "
        f"{'AGE':<8} "
        f"PWD"
    )

    for row in rows:

        (
            name,
            sid,
            status,
            worker,
            conda,
            age,
            pwd,
        ) = row

        print(
            f"{name:<16} "
            f"{sid:<12} "
            f"{status:<12} "
            f"{worker:<9} "
            f"{conda:<16} "
            f"{age:<8} "
            f"{pwd}"
        )


# =========================================================
# Kill
# =========================================================

def kill_session(ref, quiet=False):

    sid = resolve_ref(ref)

    root = root_of(sid)

    name = session_name(sid)

    (
        root
        / "control"
        / "terminate"
    ).touch()

    if not quiet:

        print(
            f"Termination requested: "
            f"{name or sid}"
        )

    deadline = (
        time.time() + 5
    )

    while (
        time.time() < deadline
    ):

        status = status_of(sid)

        if status in {
            "dead",
            "error",
        }:

            if name:

                release_name(
                    name,
                    sid,
                )

                set_session_name(
                    sid,
                    "",
                )

            if not quiet:

                print(
                    f"Session stopped: "
                    f"{sid}"
                )

            return True

        time.sleep(0.1)

    if not quiet:

        print(
            "Termination requested, "
            "but worker has not exited yet."
        )

    return False


# =========================================================
# Kill all idle
# =========================================================

def kill_all_idle():

    if not SESSIONS.exists():

        print(
            "No H100 sessions."
        )

        return

    targets = []

    for root in SESSIONS.iterdir():

        if not root.is_dir():
            continue

        sid = root.name

        if (
            status_of(sid) == "idle"
            and worker_alive(sid)
        ):

            targets.append(sid)

    if not targets:

        print(
            "No live idle sessions."
        )

        return

    print(
        f"Killing {len(targets)} "
        f"idle session(s)..."
    )

    for sid in targets:

        name = (
            session_name(sid)
            or sid
        )

        print(
            f"  {name}"
        )

        kill_session(
            sid,
            quiet=True,
        )

    print("Done.")


# =========================================================
# Prune dead records
# =========================================================

def prune_dead():

    if not SESSIONS.exists():

        print(
            "Nothing to prune."
        )

        return

    removed = 0

    for root in list(
        SESSIONS.iterdir()
    ):

        if not root.is_dir():
            continue

        sid = root.name

        status = status_of(sid)

        # 只自动删明确标记 dead/error 的，
        # 不因为 STALE 就直接删。
        if status not in {
            "dead",
            "error",
        }:
            continue

        name = session_name(sid)

        if name:

            release_name(
                name,
                sid,
            )

        shutil.rmtree(
            root,
            ignore_errors=True,
        )

        removed += 1

    # 顺便清理指向不存在 session 的名字。
    if NAMES.exists():

        for path in NAMES.iterdir():

            if not path.is_file():
                continue

            sid = read_text(
                path,
                "",
            )

            if (
                not sid
                or not root_of(sid).exists()
            ):

                try:
                    path.unlink()
                except OSError:
                    pass

    print(
        f"Pruned {removed} "
        f"dead session(s)."
    )


# =========================================================
# Attach
# =========================================================

def run_shell(sid):

    root = root_of(sid)
    
    history_file = setup_readline(sid)

    if not root.exists():

        raise RuntimeError(
            f"Session does not exist: "
            f"{sid}"
        )

    if not worker_alive(sid):

        raise RuntimeError(
            f"H100 worker for session "
            f"{sid} is not alive."
        )

    name = session_name(sid)

    print(
        f"Attached to "
        f"{name or sid} "
        f"({sid})"
    )

    print(
        "Ctrl-D or 'exit' detaches; "
        "the H100 shell stays alive."
    )

    # -----------------------------------------------------
    # Existing foreground job
    # -----------------------------------------------------

    status = status_of(sid)

    current_job = read_text(
        root
        / "state"
        / "current_job",
        "",
    )

    current_offset = read_text(
        root
        / "state"
        / "current_job_offset",
        "",
    )

    if (
        status == "running"
        and current_job
        and current_offset
    ):

        print(
            "[reattaching to "
            "running command]"
        )

        try:

            rc = wait_for_job(
                sid,
                current_job,
                int(current_offset),
            )

        except DetachRequested:

            print()
            print(
                f"Detached from "
                f"{name or sid}"
            )

            print(
                "Reattach with:"
            )

            print(
                f"  h100-shell --attach "
                f"{name or sid}"
            )

            return

        if rc != 0:

            print(
                f"[exit {rc}]"
            )

    # -----------------------------------------------------
    # Normal line shell
    # -----------------------------------------------------

    while True:

        if not worker_alive(sid):

            print(
                "\n[H100 worker ended]"
            )

            return

        try:

            command = input(
                prompt(sid)
            )

        except EOFError:

            print()
            break

        except KeyboardInterrupt:

            print()
            continue

        stripped = (
            command.strip()
        )

        if not stripped:
            continue

        first = (
            stripped
            .split(None, 1)[0]
        )

        # exit = detach，不是真 exit bash
        if first in {
            "exit",
            "quit",
            "logout",
        }:

            break
        
        save_command_history(
            history_file,
            command,
        )

        jid, offset = submit(
            sid,
            command,
        )

        try:

            rc = wait_for_job(
                sid,
                jid,
                offset,
            )

        except DetachRequested:

            print()
            print(
                f"Detached from "
                f"{name or sid}"
            )

            print(
                "Reattach with:"
            )

            print(
                f"  h100-shell --attach "
                f"{name or sid}"
            )

            return

        if rc != 0:

            print(
                f"[exit {rc}]"
            )

    name = session_name(sid)

    print(
        f"Detached from "
        f"{name or sid}"
    )

    print(
        "Reattach with:"
    )

    print(
        f"  h100-shell --attach "
        f"{name or sid}"
    )


# =========================================================
# CLI
# =========================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "Persistent H100 shell "
            "over shared storage"
        )
    )

    group = (
        parser
        .add_mutually_exclusive_group()
    )

    group.add_argument(
        "--attach",
        metavar="SESSION_OR_NAME",
    )

    group.add_argument(
        "--list",
        action="store_true",
    )
    
    group.add_argument(
        "--delete",
        metavar="SESSION_OR_NAME",
    )

    group.add_argument(
        "--kill",
        metavar="SESSION_OR_NAME",
    )

    group.add_argument(
        "--rename",
        nargs=2,
        metavar=(
            "SESSION_OR_NAME",
            "NEW_NAME",
        ),
    )

    group.add_argument(
        "--kill-all-idle",
        action="store_true",
    )

    group.add_argument(
        "--prune-dead",
        action="store_true",
    )

    parser.add_argument(
        "--name",
        metavar="NAME",
        help=(
            "Name a newly created "
            "H100 session"
        ),
    )

    parser.add_argument(
        "--start-dir",
        default=None,
    )

    args = parser.parse_args()

    management = (
        args.attach
        or args.list
        or args.kill
        or args.delete
        or args.rename
        or args.kill_all_idle
        or args.prune_dead
    )

    if management and (
        args.name
        or args.start_dir
    ):

        parser.error(
            "--name/--start-dir are only "
            "valid when creating a new session"
        )

    try:

        if args.list:

            list_sessions()
            return

        if args.kill:

            kill_session(
                args.kill
            )
            return
                
        if args.delete:

            delete_session(
                args.delete
            )
            return

        if args.rename:

            rename_session(
                args.rename[0],
                args.rename[1],
            )
            return

        if args.kill_all_idle:

            kill_all_idle()
            return

        if args.prune_dead:

            prune_dead()
            return

        if args.attach:

            sid = resolve_ref(
                args.attach
            )

        else:

            sid = create_session(
                args.start_dir,
                args.name,
            )

            print(
                f"Created H100 session: "
                f"{sid}"
            )

            if args.name:

                print(
                    f"Name: {args.name}"
                )

        run_shell(sid)

    except (
        RuntimeError,
        ValueError,
    ) as exc:

        print(
            f"Error: {exc}",
            file=sys.stderr,
        )

        sys.exit(1)


if __name__ == "__main__":
    main()