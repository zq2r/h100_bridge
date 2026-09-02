#!/usr/bin/env python3

import os
import re
from pathlib import Path

from prompt_toolkit import Application
from prompt_toolkit.completion import (
    Completer,
    Completion,
)
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (
    Float,
    FloatContainer,
    HSplit,
    Layout,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.controls import (
    FormattedTextControl,
)
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea


DONE_MARKER_RE = re.compile(
    rb"\x1eH100_DONE:"
    rb"([^:\x1f]+):"
    rb"(-?\d+)"
    rb"\x1f"
    rb"(?:\r?\n)?"
)

DONE_PREFIX = b"\x1eH100_DONE:"


# Basic ANSI escape sequence removal.
#
# First TUI version renders plain terminal text.
# We can restore ANSI colors later without touching
# the H100 transport/protocol.
ANSI_RE = re.compile(
    rb"\x1b(?:"
    rb"[@-Z\\-_]"
    rb"|"
    rb"\[[0-?]*[ -/]*[@-~]"
    rb")"
)


class PathCompleter(Completer):

    def __init__(
        self,
        match_func,
    ):
        self.match_func = match_func

    def get_completions(
        self,
        document,
        complete_event,
    ):
        # WORD=True means whitespace-separated token,
        # so paths like foo/bar remain one word.
        word = (
            document
            .get_word_before_cursor(
                WORD=True
            )
        )

        if not word:
            return

        for candidate in (
            self.match_func(word)
        ):
            yield Completion(
                candidate,
                start_position=-len(word),
            )


def read_tail_bytes(
    path,
    max_lines,
):

    path = Path(path)

    if not path.exists():
        return b"", 0

    try:
        with path.open("rb") as f:

            f.seek(
                0,
                os.SEEK_END,
            )

            end_offset = f.tell()
            pos = end_offset

            chunks = []
            line_breaks = 0

            while (
                pos > 0
                and line_breaks <= max_lines
            ):

                size = min(
                    65536,
                    pos,
                )

                pos -= size

                f.seek(pos)

                chunk = f.read(size)

                chunks.append(chunk)

                line_breaks += (
                    chunk.count(b"\n")
                    + chunk.count(b"\r")
                )

        data = b"".join(
            reversed(chunks)
        )

        lines = data.splitlines(
            keepends=True
        )

        # If we started from the middle of a file,
        # the first line may be incomplete.
        if pos > 0 and lines:
            lines = lines[1:]

        data = b"".join(
            lines[-max_lines:]
        )

        return data, end_offset

    except OSError:
        return b"", 0


class H100UI:

    def __init__(
        self,
        *,
        sid,
        label,
        stream_path,
        prompt_func,
        submit_func,
        interrupt_func,
        ack_func,
        worker_alive_func,
        current_job_func,
        save_history_func,
        history_items,
        completion_func,
        max_output_lines=1000,
    ):

        self.sid = sid
        self.label = label

        self.stream_path = Path(
            stream_path
        )

        self.prompt_func = (
            prompt_func
        )

        self.submit_func = (
            submit_func
        )

        self.interrupt_func = (
            interrupt_func
        )

        self.ack_func = (
            ack_func
        )

        self.worker_alive_func = (
            worker_alive_func
        )

        self.current_job_func = (
            current_job_func
        )

        self.save_history_func = (
            save_history_func
        )

        self.completion_func = (
            completion_func
        )

        self.max_output_lines = (
            max_output_lines
        )

        self.stream_offset = 0

        # Bytes that may contain an incomplete
        # H100_DONE marker split across reads.
        self.marker_pending = b""

        self.current_job = (
            current_job_func()
            or None
        )

        self.status_message = ""

        self.follow_output = True

        # Ctrl+G 等显式操作要求下一次 render
        # 强制回到底部。不能只依赖旧 render_info。
        self.force_bottom = False

        # -------------------------------------------------
        # History
        # -------------------------------------------------

        self.history = (
            InMemoryHistory()
        )

        for command in history_items:
            self.history.append_string(
                command
            )

        # -------------------------------------------------
        # Output pane
        # -------------------------------------------------

        self.output_area = TextArea(
            text="",
            read_only=True,
            multiline=True,
            scrollbar=True,
            focusable=True,
            focus_on_click=False,
            wrap_lines=False,
            style="class:output",
        )

        # -------------------------------------------------
        # Input pane
        #
        # multiline=True is intentional:
        # bracketed multi-line paste remains one command.
        # Enter is rebound below to submit.
        # -------------------------------------------------

        self.input_area = TextArea(
            text="",
            multiline=True,
            height=Dimension(
                min=1,
                max=5,
            ),
            prompt=self.prompt_func,
            history=self.history,
            completer=PathCompleter(
                self.completion_func
            ),
            complete_while_typing=False,
            wrap_lines=True,
            style="class:input",
        )

        # -------------------------------------------------
        # Status bar
        # -------------------------------------------------

        self.status_control = (
            FormattedTextControl(
                self._status_text
            )
        )

        self.status_window = Window(
            content=self.status_control,
            height=1,
            style="class:status",
        )

        # -------------------------------------------------
        # Completion menu
        # -------------------------------------------------

        body = FloatContainer(

            content=HSplit(
                [
                    self.output_area,
                    self.status_window,
                    self.input_area,
                ]
            ),

            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(
                        max_height=10,
                        scroll_offset=1,
                    ),
                )
            ],
        )

        # -------------------------------------------------
        # Key bindings
        # -------------------------------------------------

        self.kb = KeyBindings()

        self._install_bindings()

        self.layout = Layout(
            body,
            focused_element=(
                self.input_area
            ),
        )

        self.style = Style.from_dict(
            {
                "status": "reverse",
                "output": "",
                "input": "",
            }
        )

        self.app = Application(

            layout=self.layout,

            key_bindings=self.kb,

            style=self.style,

            # prompt_toolkit now owns the alternate screen.
            full_screen=True,

            # Required for mouse wheel / scrollbar.
            mouse_support=True,

            # Poll shared stream.log every 100ms.
            refresh_interval=0.10,

            enable_page_navigation_bindings=True,

            before_render=(
                self._before_render
            ),
        )

        self._load_initial_output()

    # =====================================================
    # Status
    # =====================================================

    def _status_text(self):

        if not self.worker_alive_func():
            state = "DEAD"

        elif self.current_job:
            state = "RUNNING"

        else:
            state = "IDLE"

        follow = (
            "FOLLOW"
            if self.follow_output
            else "SCROLL"
        )

        extra = ""

        if self.status_message:
            extra = (
                " | "
                + self.status_message
            )

        return (
            f" {self.label}"
            f" | {state}"
            f" | {follow}"
            f" | Ctrl-D detach"
            f" | Ctrl-C interrupt"
            f" | Alt-G bottom"
            f"{extra} "
        )

    # =====================================================
    # Output decoding
    # =====================================================

    def _decode_output(
        self,
        data,
    ):

        if not data:
            return ""

        data = ANSI_RE.sub(
            b"",
            data,
        )

        text = data.decode(
            "utf-8",
            errors="replace",
        )

        # PTY / interactive bash may emit sequences like
        # "\r\r\n". Treat any CR run followed by LF
        # as a single logical newline.
        text = re.sub(
            r"\r+\n",
            "\n",
            text,
        )

        # Lone CR is typically used by tqdm/progress bars.
        # Current TUI renders those as separate lines.
        text = text.replace(
            "\r",
            "\n",
        )

        return text

    def _strip_and_handle_markers(
        self,
        data,
    ):

        if not data:
            return b""

        combined = (
            self.marker_pending
            + data
        )

        self.marker_pending = b""

        # If a marker started but its terminating
        # \x1f has not arrived yet, keep it for
        # the next poll.
        last_start = combined.rfind(
            DONE_PREFIX
        )

        if (
            last_start >= 0
            and b"\x1f"
            not in combined[
                last_start:
            ]
        ):
            safe = combined[
                :last_start
            ]

            self.marker_pending = (
                combined[last_start:]
            )

        else:
            safe = combined

        for match in (
            DONE_MARKER_RE.finditer(
                safe
            )
        ):

            try:
                jid = (
                    match.group(1)
                    .decode(
                        errors="replace"
                    )
                )

                rc = int(
                    match.group(2)
                )

            except Exception:
                continue

            if (
                self.current_job
                and jid
                == self.current_job
            ):

                self.ack_func(
                    jid
                )

                self.current_job = None

                if rc == 0:
                    self.status_message = ""
                else:
                    self.status_message = (
                        f"exit {rc}"
                    )

        return DONE_MARKER_RE.sub(
            b"",
            safe,
        )

    # =====================================================
    # Output buffer
    # =====================================================

    def _set_output(
        self,
        text,
        *,
        follow=True,
        anchor_row=0,
    ):

        lines = text.splitlines()

        lines = lines[
            -self.max_output_lines:
        ]

        new_text = "\n".join(
            lines
        )

        if text.endswith("\n"):
            new_text += "\n"

        if follow:

            document = Document(
                new_text,
                cursor_position=len(
                    new_text
                ),
            )

        else:

            tmp = Document(
                new_text
            )

            if tmp.line_count <= 0:
                cursor = 0

            else:
                row = min(
                    anchor_row,
                    tmp.line_count - 1,
                )

                cursor = (
                    tmp
                    .translate_row_col_to_index(
                        row,
                        0,
                    )
                )

            document = Document(
                new_text,
                cursor_position=cursor,
            )

        self.output_area.buffer.set_document(
            document,
            bypass_readonly=True,
        )

    def _append_output(
        self,
        text,
    ):

        if not text:
            return

        render_info = (
            self.output_area
            .window
            .render_info
        )

        follow = self.follow_output
        anchor_row = 0

        if render_info is not None:

            # Ctrl+G 刚刚被按下时，
            # 不允许旧 render_info 把 FOLLOW
            # 又覆盖成 SCROLL。
            if self.force_bottom:

                follow = True

            else:

                follow = (
                    render_info.bottom_visible
                )

            if not follow:

                anchor_row = (
                    render_info
                    .first_visible_line()
                )

        current = (
            self.output_area.text
        )

        old_line_count = len(
            current.splitlines()
        )

        combined = (
            current + text
        )

        all_lines = (
            combined.splitlines()
        )

        dropped = max(
            0,
            len(all_lines)
            - self.max_output_lines,
        )

        if not follow:
            anchor_row = max(
                0,
                anchor_row - dropped,
            )

        self.follow_output = follow

        self._set_output(
            combined,
            follow=follow,
            anchor_row=anchor_row,
        )

    def _jump_to_bottom(self):

        self.follow_output = True
        self.force_bottom = True

        text = self.output_area.text

        # 1. Buffer cursor 放到最后。
        self.output_area.buffer.cursor_position = (
            len(text)
        )

        # 2. 更重要：
        # Window 本身的 viewport 直接移到底部。
        info = (
            self.output_area
            .window
            .render_info
        )

        if info is not None:

            max_scroll = max(
                0,
                info.content_height
                - info.window_height,
            )

            self.output_area.window.vertical_scroll = (
                max_scroll
            )

            self.output_area.window.vertical_scroll_2 = 0

    # =====================================================
    # Initial stream
    # =====================================================

    def _load_initial_output(self):

        data, end_offset = (
            read_tail_bytes(
                self.stream_path,
                self.max_output_lines,
            )
        )

        self.stream_offset = (
            end_offset
        )

        visible = (
            self
            ._strip_and_handle_markers(
                data
            )
        )

        text = self._decode_output(
            visible
        )

        self._set_output(
            text,
            follow=True,
        )

    # =====================================================
    # Stream polling
    # =====================================================

    def _reload_after_trim(self):

        data, end_offset = (
            read_tail_bytes(
                self.stream_path,
                self.max_output_lines,
            )
        )

        self.stream_offset = (
            end_offset
        )

        self.marker_pending = b""

        visible = (
            self
            ._strip_and_handle_markers(
                data
            )
        )

        text = self._decode_output(
            visible
        )

        self._set_output(
            text,
            follow=True,
        )

    def _poll_stream(self):

        try:
            size = (
                self.stream_path
                .stat()
                .st_size
            )

        except OSError:
            return

        # Worker trimmed stream.log.
        if size < self.stream_offset:

            self._reload_after_trim()

            return

        if size == self.stream_offset:
            return

        try:

            with self.stream_path.open(
                "rb"
            ) as f:

                f.seek(
                    self.stream_offset
                )

                data = f.read()

                self.stream_offset = (
                    f.tell()
                )

        except OSError:
            return

        visible = (
            self
            ._strip_and_handle_markers(
                data
            )
        )

        text = self._decode_output(
            visible
        )

        self._append_output(
            text
        )

    def _sync_job_state(self):

        remote_job = (
            self.current_job_func()
            or None
        )

        # This is mainly a fallback.
        #
        # Normally H100_DONE marker is what clears
        # self.current_job.
        if (
            self.current_job
            and not remote_job
        ):
            self.current_job = None

        elif (
            not self.current_job
            and remote_job
        ):
            self.current_job = (
                remote_job
            )

    def _before_render(
        self,
        app,
    ):

        self._poll_stream()

        self._sync_job_state()

        info = (
            self.output_area
            .window
            .render_info
        )

        if self.force_bottom:

            # 每次 render 前再保险一次。
            self.output_area.buffer.cursor_position = (
                len(
                    self.output_area.text
                )
            )

            if info is not None:

                self.output_area.window.vertical_scroll = max(
                    0,
                    info.content_height
                    - info.window_height,
                )

                self.output_area.window.vertical_scroll_2 = 0

                # 上一次 render 已经成功到底，
                # 可以退出强制模式。
                if info.bottom_visible:
                    self.force_bottom = False

        elif info is not None:

            # 鼠标滚动也会反映到状态栏。
            self.follow_output = (
                info.bottom_visible
            )

    # =====================================================
    # Command handling
    # =====================================================

    def _submit_command(
        self,
        event,
    ):

        buffer = (
            self.input_area.buffer
        )

        command = buffer.text

        stripped = command.strip()

        if not stripped:
            buffer.reset()
            return

        first = (
            stripped
            .split(None, 1)[0]
        )

        if first in {
            "exit",
            "quit",
            "logout",
        }:

            event.app.exit(
                result="detach"
            )

            return

        if not self.worker_alive_func():

            self.status_message = (
                "worker is not alive"
            )

            return

        if self.current_job:

            self.status_message = (
                "foreground job is running"
            )

            return

        self.save_history_func(
            command
        )

        self.history.append_string(
            command
        )

        try:

            jid, _ = self.submit_func(
                command
            )

        except Exception as exc:

            self.status_message = (
                f"submit failed: {exc}"
            )

            return

        self.current_job = jid

        self.status_message = ""

        self.follow_output = True

        buffer.reset()

        self._jump_to_bottom()

        event.app.invalidate()

    # =====================================================
    # Scrolling
    # =====================================================

    def _scroll_page(
        self,
        delta_pages,
    ):

        info = (
            self.output_area
            .window
            .render_info
        )

        if info is None:
            page = 20
            current_row = 0

        else:

            page = max(
                1,
                info.window_height - 2,
            )

            current_row = (
                info.first_visible_line()
            )

        document = (
            self.output_area
            .buffer
            .document
        )

        target = (
            current_row
            + delta_pages * page
        )

        target = max(
            0,
            min(
                target,
                document.line_count - 1,
            ),
        )

        position = (
            document
            .translate_row_col_to_index(
                target,
                0,
            )
        )

        self.output_area.buffer.cursor_position = (
            position
        )

        self.follow_output = (
            target
            >= document.line_count - 1
        )

    # =====================================================
    # Key bindings
    # =====================================================

    def _install_bindings(self):

        @self.kb.add("enter")
        def _enter(event):

            # Only execute Enter from the input pane.
            if event.app.layout.has_focus(
                self.input_area
            ):
                self._submit_command(
                    event
                )

        @self.kb.add("c-d")
        def _detach(event):

            event.app.exit(
                result="detach"
            )

        @self.kb.add("c-c")
        def _interrupt(event):

            if self.current_job:

                self.interrupt_func(
                    self.current_job
                )

                self.status_message = (
                    "interrupt requested"
                )

            else:

                self.input_area.buffer.reset()

            event.app.invalidate()

        @self.kb.add(
            "escape",
            "g",
            eager=True,
        )
        def _bottom(event):

            self._jump_to_bottom()

            event.app.layout.focus(
                self.input_area
            )

            event.app.invalidate()

        @self.kb.add("pageup")
        def _page_up(event):

            self._scroll_page(-1)

            event.app.invalidate()

        @self.kb.add("pagedown")
        def _page_down(event):

            self._scroll_page(1)

            event.app.invalidate()

        @self.kb.add("tab")
        def _tab(event):

            if event.app.layout.has_focus(
                self.input_area
            ):

                self.input_area.buffer.start_completion(
                    insert_common_part=True
                )

    # =====================================================
    # Run
    # =====================================================

    def run(self):

        return self.app.run()


def run_h100_ui(**kwargs):

    ui = H100UI(**kwargs)

    return ui.run()