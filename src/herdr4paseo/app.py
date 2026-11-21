"""herdr4paseo: a log view plus prompt box for one Paseo agent, built on the `paseo` CLI.

It owns nothing. The agent keeps running in the Paseo daemon, so quitting never
interrupts it, and the Paseo app can be attached to the same agent at the same time.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable

from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import Footer, Header, Input, OptionList, RichLog, Static, TextArea
from textual.widgets.option_list import Option

from rich.text import Text

from .core import (
    BEHAVIORS,
    KANAGAWA,
    Agent,
    Command,
    LogStyler,
    Permission,
    agent_label_text,
    filter_agents,
    find_agent,
    history_path,
    load_history,
    logs_argv,
    MAX_LOG_LINE_BYTES,
    MAX_RECORD_BYTES,
    MAX_TIMELINE_ITEMS,
    PERMIT_BATCH,
    clip_label,
    permit_batch,
    sanitize_line,
    sanitize_text,
    trim_timeline,
    apply_timeline_item,
    iter_record_lines,
    timeline_argv,
    parse_timeline,
    render_timeline_item,
    ls_argv,
    match_commands,
    parse_agents,
    parse_permissions,
    permit_argv,
    permit_ls_argv,
    run_argv,
    save_history,
    send_argv,
    should_alert,
    status_style,
    stop_argv,
    style_for,
)
from .daemon import DaemonError, fetch_commands
from .poll import PollConfig, PollSchedule, inspect_argv, parse_inspect

MAX_LOG_LINES = 20_000  # per RichLog: older lines scroll off instead of growing memory forever
RENDER_BUDGET_CHARS = 512_000  # newest items rendered per chat redraw; older ones show as a count
RENDER_SECONDS = 0.1  # at most ~10 chat re-renders a second however fast records arrive
RECONNECT_SECONDS = 3.0
STREAM_SETTLE_SECONDS = 1.0  # log output this soon after (re)attaching is replay or an error, not a wake-up
# `y` in an ALLOW dialog is ignored (bell) for this long after the dialog mounts, so a fast
# repeated Ctrl+A, y cannot approve a request that was on screen for only a split second.
ALLOW_DELAY_SECONDS = 0.5


class PromptArea(TextArea):
    """Enter sends; Ctrl+J or Shift+Enter inserts a newline."""

    class Submitted(Message):
        pass

    # Set by the app: takes Tab/Up/Down, returns True when a slash suggestion consumed the key.
    slash_handler: Callable[[str], bool] | None = None

    async def _on_key(self, event: events.Key) -> None:
        if event.key in ("tab", "up", "down") and self.slash_handler and self.slash_handler(event.key):
            event.prevent_default()
            event.stop()
        elif event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted())
        elif event.key in ("ctrl+j", "shift+enter"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
        else:
            await super()._on_key(event)


class PickAgent(ModalScreen[str]):
    """Type to filter (every word must match id, status, name, provider or directory); Enter picks."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, agents: list[Agent]) -> None:
        super().__init__()
        self._agents = agents

    def compose(self) -> ComposeResult:
        with Vertical(id="pick-box"):
            yield Input(placeholder="filter agents", id="pick-filter")
            yield OptionList(*[Option(agent_label_text(a), id=a.id) for a in self._agents], id="pick-list")

    def on_input_changed(self, event: Input.Changed) -> None:
        shown = filter_agents(self._agents, event.value)
        options = self.query_one("#pick-list", OptionList)
        options.clear_options()
        options.add_options([Option(agent_label_text(a), id=a.id) for a in shown])
        if shown:
            options.highlighted = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        options = self.query_one("#pick-list", OptionList)
        if options.highlighted is not None and options.option_count:
            self.dismiss(options.get_option_at_index(options.highlighted).id or "")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "down"):
            options = self.query_one("#pick-list", OptionList)
            event.stop()
            event.prevent_default()
            (options.action_cursor_up if event.key == "up" else options.action_cursor_down)()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "")

    def action_dismiss_none(self) -> None:
        self.dismiss("")


class NewAgent(ModalScreen[tuple[str, str, str]]):
    """Prompt, directory and provider for `paseo run`; Enter submits, Escape cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, provider: str) -> None:
        super().__init__()
        self._provider = sanitize_line(provider)  # prefilled from `paseo ls`, so untrusted

    def compose(self) -> ComposeResult:
        with Vertical(id="new-box"):
            yield Static("Start a new agent", markup=False)
            yield Input(placeholder="prompt (required)", id="new-prompt")
            yield Input(value=os.getcwd(), placeholder="directory", id="new-dir")
            yield Input(value=self._provider, placeholder="provider or provider/model, e.g. codex (required)", id="new-provider")
            yield Static("Enter starts it, Escape cancels", markup=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        prompt = self.query_one("#new-prompt", Input).value.strip()
        provider = self.query_one("#new-provider", Input).value.strip()
        if prompt and provider:  # `paseo run` refuses to start without a provider
            directory = os.path.expanduser(self.query_one("#new-dir", Input).value.strip() or os.getcwd())
            self.dismiss((prompt, directory, provider))

    def action_cancel(self) -> None:
        self.dismiss(("", "", ""))


class ConfirmStop(ModalScreen[bool]):
    """y stops the agent; n or Escape cancels."""

    BINDINGS = [
        Binding("y", "confirm(True)", "Yes"),
        Binding("n,escape", "confirm(False)", "No"),
    ]

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent

    def compose(self) -> ComposeResult:
        yield Static(
            clip_label(sanitize_line(f"STOP agent {self._agent.name} ({self._agent.short_id}, {self._agent.status})?"), 200) + "\n\n[y] yes   [n] no",
            id="confirm-box",
            markup=False,
        )

    def action_confirm(self, answer: bool) -> None:
        self.dismiss(answer)


class ConfirmPermits(ModalScreen[bool]):
    """Lists exactly the requests that will be answered; y confirms, n or Escape cancels."""

    BINDINGS = [
        Binding("y", "confirm(True)", "Yes"),
        Binding("n,escape", "confirm(False)", "No"),
    ]

    def __init__(
        self,
        decision: str,
        permits: list[Permission],
        pending: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__()
        self._decision = decision
        self._clock = clock
        self._mounted_at: float | None = None
        self._permits = permits
        self._pending = len(permits) if pending is None else pending

    def compose(self) -> ComposeResult:
        # Allow: every request listed here is shown in full and is exactly what y answers (permit_batch).
        # Deny may answer more than it lists (denying an unread request is safe), so it says so.
        shown = self._permits if self._decision == "allow" else self._permits[:PERMIT_BATCH]
        lines = "\n".join(f"  - {p.label if self._decision == 'allow' else clip_label(p.label)}" for p in shown)
        if self._decision == "allow" and self._pending > len(self._permits):
            lines += f"\n  {self._pending - len(self._permits)} other request(s) stay pending (not shown here; Ctrl+A again for the next)"
        elif len(self._permits) > len(shown):
            lines += f"\n  ... and {len(self._permits) - len(shown)} more (all {len(self._permits)} are denied)"
        yield Static(
            f"{self._decision.upper()} {len(self._permits)} of {self._pending} pending request(s)?\n\n{lines}\n\n[y] yes   [n] no",
            id="confirm-box",
            markup=False,
        )

    def on_mount(self) -> None:
        self._mounted_at = self._clock()

    def action_confirm(self, answer: bool) -> None:
        # Deny stays instant (denying an unread request is safe); allow waits out the delay.
        if answer and self._decision == "allow":
            if self._mounted_at is None or self._clock() - self._mounted_at < ALLOW_DELAY_SECONDS:
                self.app.bell()
                return
        self.dismiss(answer)


async def run_paseo(argv: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # missing or non-executable paseo: report it like any failed call
        return 127, "", f"cannot run {argv[0]}: {exc.strerror or exc} (set PASEO_BIN to its full path)"
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


# Textual theme for the widgets (permits banner, dialogs, borders). It is an ANSI theme, so the
# background and plain text stay the terminal's own (as herdr does with `Color::Reset`), and
# only the accents are set, to the same dark Kanagawa shades the log uses.
APP_THEME = Theme(
    name="herdr4paseo",
    primary=KANAGAWA["blue"],
    secondary=KANAGAWA["teal"],
    warning=KANAGAWA["yellow"],
    error=KANAGAWA["red"],
    success=KANAGAWA["green"],
    accent=KANAGAWA["mauve"],
    foreground="ansi_default",
    background="ansi_default",
    surface="ansi_default",
    panel="ansi_default",
    boost="ansi_default",
    dark=True,
    variables=dict(BUILTIN_THEMES["ansi-dark"].variables),
    ansi=True,
)

# Typed in the prompt, these quit herdr4paseo itself; the agent keeps running and never sees them.
QUIT_COMMANDS = frozenset({"/quit", "/exit"})


class Herdr4PaseoApp(App[None]):
    CSS = """
    #log { height: 1fr; scrollbar-size-vertical: 1; scrollbar-color: #363646; scrollbar-color-hover: #54546D; scrollbar-color-active: #54546D; scrollbar-background: ansi_default; scrollbar-background-hover: ansi_default; scrollbar-background-active: ansi_default; }
    #chat { height: 1fr; }
    #chat.hidden, #log.hidden { display: none; }
    #prompt { height: 6; border: round $primary; }
    #permits { height: auto; color: $warning; text-style: bold; padding: 0 1; border-left: thick $warning; }
    #permits.hidden { display: none; }
    #slash { height: auto; max-height: 9; padding: 0 1; background: $panel; }
    #slash.hidden { display: none; }
    PickAgent, ConfirmPermits, ConfirmStop, NewAgent { align: center middle; }
    #confirm-box { width: 80%; height: auto; border: round $warning; padding: 1 2; background: $surface; }
    #pick-box { width: 90%; height: 70%; border: round $primary; }
    #pick-list { height: 1fr; }
    #new-box { width: 90%; height: auto; border: round $primary; padding: 1 2; background: $surface; }
    """
    ENABLE_COMMAND_PALETTE = False  # Ctrl+P is prompt history here
    BINDINGS = [
        Binding("ctrl+t", "toggle_behavior", "steer/interrupt"),
        Binding("ctrl+v", "toggle_view", "log/chat", priority=True),
        Binding("ctrl+o", "toggle_tools", "tools only"),
        # Ctrl+G is macOS's Gemini shortcut, so the agent picker lives on Ctrl+L.
        Binding("ctrl+l", "switch_agent", "agents"),
        Binding("ctrl+q", "quit", "quit", priority=True),  # Textual's default, shown in the footer here
        Binding("ctrl+r", "new_agent", "new agent", priority=True),
        # Priority beats the prompt's own Ctrl+X (cut); a confirmation guards the stop.
        Binding("ctrl+x", "stop_agent", "stop agent", priority=True),
        # Priority beats the prompt's own Ctrl+A/Ctrl+D, but check_action only enables
        # these while a request is pending, so the prompt keeps its keys otherwise.
        Binding("ctrl+a", "answer_permits('allow')", "allow", priority=True),
        Binding("ctrl+d", "answer_permits('deny')", "deny", priority=True),
        Binding("ctrl+p", "history_prev", "prev", show=False),
        Binding("ctrl+n", "history_next", "next", show=False),
    ]

    def __init__(self, agent_ident: str | None, *, tail: int, behavior: str) -> None:
        super().__init__()
        self._ident = agent_ident
        self._tail = tail
        self.behavior = behavior
        self.tools_only = False
        self.chat_view = False
        self.timeline: list[dict] = []
        self._rendered: dict[int, tuple[dict, Text]] = {}  # render cache by timeline index
        self._render_timer = None  # pending coalesced render
        self._open_chain: int | None = None  # index of the assistant/reasoning text still receiving deltas
        self.agent: Agent | None = None
        self._history: list[str] = load_history(history_path())
        self._last_status: str | None = None
        self._history_pos = len(self._history)
        self.permits: list[Permission] = []
        self.permit_clock: Callable[[], float] = time.monotonic  # tests inject a fake clock
        self.commands: list[Command] = []
        self.slash_matches: list[Command] = []
        self._slash_idx = 0
        self._poll = PollSchedule(PollConfig.from_env())  # status polling cadence (poll.py)
        self._poll_timer = None
        self._poll_due = 0.0
        self._stream_up_at = 0.0  # when the log stream last (re)attached
        self._polling = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield RichLog(id="log", wrap=True, markup=False, highlight=False, auto_scroll=True, max_lines=MAX_LOG_LINES)
            yield RichLog(id="chat", classes="hidden", wrap=True, markup=False, highlight=False, max_lines=MAX_LOG_LINES)
            yield Static("", id="permits", classes="hidden", markup=False)
            yield Static("", id="slash", classes="hidden", markup=False)
            yield PromptArea(id="prompt")
        yield Footer()

    async def on_mount(self) -> None:
        self.register_theme(APP_THEME)
        self.theme = APP_THEME.name
        prompt = self.query_one("#prompt", PromptArea)
        prompt.slash_handler = self.handle_slash_key
        prompt.focus()
        self._arm_poll()
        self._choose_agent(self._ident)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "answer_permits":
            return bool(self.permits)
        return True

    # -- agent selection and status -------------------------------------------------

    async def _list_agents(self) -> list[Agent]:
        code, out, err = await run_paseo(ls_argv())
        if code != 0:
            self._note(f"paseo ls failed (rc={code}): {err.strip()}", "error.text")
            return []
        try:
            return parse_agents(out)
        except (ValueError, TypeError, AttributeError) as exc:
            self._note(f"could not read agent list: {exc}", "error.text")
            return []

    @work(exclusive=True, group="choose")  # push_screen_wait only runs inside a worker
    async def _choose_agent(self, ident: str | None) -> None:
        agents = await self._list_agents()
        picked = find_agent(agents, ident) if ident else None
        if picked is None and agents:
            choice = await self.push_screen_wait(PickAgent(agents))
            picked = find_agent(agents, choice) if choice else None
        if picked is None:
            self._note("no agent selected; Ctrl+L to pick one", "error.text")
            return
        self.agent = picked
        self._poll = PollSchedule(self._poll.config)  # a new agent starts a fresh schedule
        self._wake_poll()
        self._last_status = picked.status
        self.title = sanitize_line(f"{picked.name} ({picked.short_id})")
        self._update_subtitle(picked.status)
        self._start_stream()
        self._load_commands(picked.id)
        await self._refresh_permits()

    def _arm_poll(self, delay: float | None = None) -> None:
        """(Re)start the one-shot poll timer; delay defaults to the schedule's next interval."""
        if not self.is_running:
            return
        if self._poll_timer is not None:
            self._poll_timer.stop()
        delay = self._poll.delay() if delay is None else delay
        self._poll_due = time.monotonic() + delay
        self._poll_timer = self.set_timer(delay, self._poll_tick)

    def _wake_poll(self) -> None:
        """Poll sooner if the schedule now says so (never later); a poll in flight re-arms itself."""
        if not self._polling and time.monotonic() + self._poll.delay() < self._poll_due:
            self._arm_poll()

    async def _poll_tick(self) -> None:
        self._poll_timer, self._polling = None, True
        try:
            await self._refresh_status()
        finally:
            self._polling = False
            self._arm_poll()

    def _bump_poll(self) -> None:
        """A user action probably changed the agent: poll fast for a while."""
        self._poll.bump()
        self._wake_poll()

    async def _probe_agent(self, agent_id: str) -> tuple[str, bool] | None:
        """(status, request pending) from one `paseo agent inspect`; None sends the caller to `paseo ls`."""
        code, out, _err = await run_paseo(inspect_argv(agent_id))
        if code != 0:
            return None
        try:
            return parse_inspect(out, agent_id)
        except (ValueError, TypeError, AttributeError):
            return None

    async def _refresh_status(self) -> None:
        if self.agent is None:
            return
        agent_id, before = self.agent.id, len(self.permits)
        probe = await self._probe_agent(agent_id)
        if self.agent is None or self.agent.id != agent_id:
            return  # switched agent during the probe: its answer must not touch the new agent's banner
        if probe is not None:
            status, pending = probe
            if pending:
                await self._refresh_permits()  # the labels come from `permit ls`; run only when one waits
            elif self.permits:
                self._show_permits([])  # answered elsewhere
        else:  # old paseo or an odd answer: the full listing plus `permit ls`, as before
            pending = False
            current = find_agent(await self._list_agents(), agent_id)
            status = current.status if current is not None else None
            await self._refresh_permits()
        if self.agent is None or self.agent.id != agent_id:
            return  # switched agent during the poll
        if status is None:
            return
        self._update_subtitle(status)
        self._poll.observe(status, len(self.permits) or int(pending))
        reason = should_alert(self._last_status, status, before, len(self.permits))
        self._last_status = status
        if reason:
            self.bell()
            self._note(f"alert: {reason}", "permission")

    async def _refresh_permits(self) -> None:
        """Poll pending permission requests for the shown agent and update the banner."""
        if self.agent is None:
            return
        code, out, _err = await run_paseo(permit_ls_argv())
        if code != 0:
            return  # keep the last banner; a transient failure must not hide a pending request
        try:
            permits = parse_permissions(out, self.agent.id)
        except (ValueError, TypeError, AttributeError):
            return
        self._show_permits(permits)

    def _show_permits(self, permits: list[Permission]) -> None:
        self.permits = permits
        self.refresh_bindings()
        banner = self.query_one("#permits", Static)
        if self.permits:
            names = "; ".join(clip_label(p.label, 80) for p in self.permits[:3])
            if len(self.permits) > 3:
                names += f"; ... and {len(self.permits) - 3} more"
            banner.update(f"{len(self.permits)} pending: {names}  (Ctrl+A allow, Ctrl+D deny)")
            banner.remove_class("hidden")
        else:
            banner.add_class("hidden")

    def format_title(self, title: str, sub_title: str) -> Content:
        """Like Textual's, but the status word keeps its colour (herdr's running/idle/error mapping)."""
        status, sep, rest = sub_title.partition(" | ")
        style = status_style(status)
        if not sep or not style:
            return super().format_title(title, sub_title)
        return Content.assemble(
            Content(title),
            (" — ", "dim"),
            Content.from_rich_text(Text(status, style=style)),
            (sep + rest, "dim"),
        )

    def _update_subtitle(self, status: str) -> None:
        view = "tools only" if self.tools_only else "full log"
        status = sanitize_line(status)
        self.sub_title = f"{status} | send: {self.behavior} | {'chat' if self.chat_view else 'log'} | {view}"

    # -- log stream -----------------------------------------------------------------

    def _note(self, message: str, token: str) -> None:
        """Write one of herdr4paseo's own lines to the log, styled by a core.THEME token."""
        self.query_one("#log", RichLog).write(Text(sanitize_text(message), style=style_for(token)))

    @work(exclusive=True, group="stream")
    async def _start_stream(self) -> None:
        """Follow the log; when the stream ends on its own, reattach after a pause until replaced."""
        if self.agent is None:
            return
        log = self.query_one("#log", RichLog)
        agent_id, tools_only = self.agent.id, self.tools_only
        if self.chat_view:
            self._start_chat_stream()
            return
        self.workers.cancel_group(self, "chat-stream")
        while True:
            log.clear()
            styler = LogStyler()  # fresh state for each (re)attach: the tail is replayed from scratch
            try:
                proc = await asyncio.create_subprocess_exec(
                    *logs_argv(agent_id, tail=self._tail, tools_only=tools_only),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError as exc:  # paseo missing or not executable: note it and retry, never exit
                self._note(f"cannot run paseo: {exc.strerror or exc}; retrying in {RECONNECT_SECONDS:g}s", "error.text")
                await asyncio.sleep(RECONNECT_SECONDS)
                continue
            self._stream_up_at = time.monotonic()
            try:
                assert proc.stdout is not None
                async for line in iter_record_lines(proc.stdout, MAX_LOG_LINE_BYTES):
                    if line is None:  # no 64 KiB readline() limit: a huge line is skipped with a notice
                        self._note(f"skipped a log line over {MAX_LOG_LINE_BYTES >> 10} KiB", "error.text")
                        continue
                    log.write(styler.style(line.decode(errors="replace").rstrip("\n")))
                    self._log_activity()
                rc = await proc.wait()
            finally:
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                    await proc.wait()
            self._note(f"log stream ended (rc={rc}); reconnecting in {RECONNECT_SECONDS:g}s", "meta")
            await asyncio.sleep(RECONNECT_SECONDS)

    def _apply_timeline(self, raw: str) -> None:
        """Fold records into the timeline now; the visible re-render is coalesced (RENDER_SECONDS)."""
        # an envelope can hold thousands of items: only the newest ones can survive the trim anyway
        self._log_activity()
        for item in parse_timeline(raw)[-MAX_TIMELINE_ITEMS:]:
            self._open_chain = apply_timeline_item(self.timeline, item, self._open_chain)
        self._open_chain, dropped = trim_timeline(self.timeline, self._open_chain)
        if dropped:
            self._rendered = {}
        if self._render_timer is None:
            self._render_timer = self.set_timer(RENDER_SECONDS, self._render_timeline)

    def _render_timeline(self) -> None:
        """Redraw the newest items that fit RENDER_BUDGET_CHARS; unchanged items reuse their cached Text."""
        self._render_timer = None
        try:
            log = self.query_one("#chat", RichLog)
        except NoMatches:  # the timer fired while the app was shutting down
            return
        try:
            self._redraw_chat(log)
        except Exception as exc:  # a render bug must not take the app (and its permit banner) down
            self._note(f"chat render failed: {type(exc).__name__}", "error.text")

    def _redraw_chat(self, log: RichLog) -> None:
        cache, budget, shown = self._rendered, RENDER_BUDGET_CHARS, []
        hidden = 0
        for i in range(len(self.timeline) - 1, -1, -1):
            item = self.timeline[i]
            if self.tools_only and item["type"] != "tool_call":
                continue
            if budget <= 0:
                hidden += 1
                continue
            # apply_timeline_item swaps in a new dict for every change, so `is` finds the unchanged items
            if i in cache and cache[i][0] is item:
                text = cache[i][1]
            else:
                try:
                    text = render_timeline_item(item)
                except Exception:
                    text = Text("[unrenderable record]", style=style_for("error.text"))
                cache[i] = (item, text)
            budget -= len(text.plain)
            shown.append(text)
        log.clear()
        if hidden:
            log.write(Text(f"... {hidden} earlier items not shown", style=style_for("meta")))
        for text in reversed(shown):
            log.write(text)
            log.write("")

    def _reset_timeline(self) -> None:
        self.timeline, self._rendered, self._open_chain = [], {}, None
        if self._render_timer is not None:
            self._render_timer.stop()
            self._render_timer = None
        with contextlib.suppress(NoMatches):
            self.query_one("#chat", RichLog).clear()

    @work(exclusive=True, group="chat-stream")
    async def _start_chat_stream(self) -> None:
        if self.agent is None:
            return
        agent_id = self.agent.id
        while True:
            self._reset_timeline()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *timeline_argv(agent_id, tail=self._tail),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                self._note(f"cannot run paseo: {exc.strerror or exc}; retrying in {RECONNECT_SECONDS:g}s", "error.text")
                await asyncio.sleep(RECONNECT_SECONDS)
                continue
            async def notices() -> None:
                assert proc.stderr is not None
                async for line in iter_record_lines(proc.stderr, MAX_LOG_LINE_BYTES):
                    self._note("skipped a stderr line over %d KiB" % (MAX_LOG_LINE_BYTES >> 10) if line is None
                               else line.decode(errors="replace").rstrip(), "meta")
            reader = asyncio.create_task(notices())
            self._stream_up_at = time.monotonic()
            try:
                assert proc.stdout is not None
                async for line in iter_record_lines(proc.stdout, MAX_RECORD_BYTES):
                    if line is None:
                        self._note(f"skipped a timeline record over {MAX_RECORD_BYTES >> 10} KiB", "error.text")
                        continue
                    try:
                        self._apply_timeline(line.decode(errors="replace"))
                    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):  # malformed record: skip it, keep the stream
                        self._note("invalid timeline JSON record", "error.text")
                await proc.wait()
            finally:
                if proc.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                    await proc.wait()
                await reader
            self._note("chat stream ended; reconnecting", "meta")
            await asyncio.sleep(RECONNECT_SECONDS)

    def _log_activity(self) -> None:
        # the first second of a stream is its replayed tail or a failure notice, not new output
        if time.monotonic() - self._stream_up_at < STREAM_SETTLE_SECONDS:
            return
        if self._poll.log_activity():  # a settled agent is producing output: look now, not in a minute
            self._wake_poll()

    # -- slash commands -------------------------------------------------------------

    @work(exclusive=True, group="commands")
    async def _load_commands(self, agent_id: str) -> None:
        """Fetch the agent's slash commands from the daemon; completion is optional, so failure only notes."""
        self.commands = []
        try:
            self.commands = await fetch_commands(agent_id)
        except DaemonError as exc:
            self._note(f"slash completion unavailable ({exc}); typed /commands still work", "meta")
        self._update_slash()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._update_slash()

    def _update_slash(self) -> None:
        text = self.query_one("#prompt", PromptArea).text
        self.slash_matches = match_commands(self.commands, text)
        self._slash_idx = min(self._slash_idx, max(0, len(self.slash_matches) - 1))
        self._render_slash()

    def _render_slash(self) -> None:
        panel = self.query_one("#slash", Static)
        if not self.slash_matches:
            panel.add_class("hidden")
            return
        lines = [
            f"{'>' if i == self._slash_idx else ' '} {c.label}"[:100] for i, c in enumerate(self.slash_matches)
        ]
        panel.update("\n".join(lines + ["  Tab complete, Up/Down choose, Enter sends as typed"]))
        panel.remove_class("hidden")

    def handle_slash_key(self, key: str) -> bool:
        """Tab/Up/Down while suggestions show; True means the key was used."""
        if not self.slash_matches:
            return False
        if key == "tab":
            prompt = self.query_one("#prompt", PromptArea)
            prompt.text = f"/{self.slash_matches[self._slash_idx].name} "
            prompt.move_cursor(prompt.document.end)
            self._slash_idx = 0
            return True
        step = -1 if key == "up" else 1
        self._slash_idx = (self._slash_idx + step) % len(self.slash_matches)
        self._render_slash()
        return True

    # -- sending --------------------------------------------------------------------

    async def on_prompt_area_submitted(self, message: PromptArea.Submitted) -> None:
        message.stop()
        if self.agent is None:
            self._note("no agent selected; Ctrl+L to pick one", "error.text")
            return
        prompt = self.query_one("#prompt", PromptArea)
        text = prompt.text.strip()
        if not text:
            return
        if text.lower() in QUIT_COMMANDS:  # handled here, never sent to the agent
            self.exit()
            return
        code, _out, err = await run_paseo(send_argv(self.agent.id, text, self.behavior))
        if code != 0:
            self._note(f"send failed (rc={code}): {err.strip()}", "error.text")
            return  # keep the text so nothing typed is lost
        self._bump_poll()
        self._history.append(text)
        self._history_pos = len(self._history)
        save_history(history_path(), self._history)
        prompt.clear()

    # -- actions --------------------------------------------------------------------

    def action_toggle_behavior(self) -> None:
        self.behavior = BEHAVIORS[(BEHAVIORS.index(self.behavior) + 1) % len(BEHAVIORS)]
        self._update_subtitle(self.sub_title.split(" | ")[0])

    def action_toggle_view(self) -> None:
        self.chat_view = not self.chat_view
        self.query_one("#log").set_class(self.chat_view, "hidden")
        self.query_one("#chat").set_class(not self.chat_view, "hidden")
        self._update_subtitle(self.sub_title.split(" | ")[0])
        self._start_stream()

    def action_toggle_tools(self) -> None:
        self.tools_only = not self.tools_only
        self._update_subtitle(self.sub_title.split(" | ")[0])
        self._start_stream()

    def action_switch_agent(self) -> None:
        self._choose_agent(None)

    @work(exclusive=True, group="permit")  # push_screen_wait only runs inside a worker
    async def action_answer_permits(self, decision: str) -> None:
        if self.agent is None:
            return
        await self._refresh_permits()
        pending = list(self.permits)
        shown = permit_batch(pending, decision)  # answer exactly what the confirmation lists in full
        if not pending:
            self._note("no pending permission requests", "meta")
            return
        if not shown:
            self._note("pending request(s) too long to review here; answer them with `paseo permit allow` after reading them", "error.text")
            return
        if not await self.push_screen_wait(ConfirmPermits(decision, shown, len(pending), self.permit_clock)):
            return
        for permit in shown:
            code, _out, err = await run_paseo(permit_argv(decision, self.agent.id, permit.id))
            result = "ok" if code == 0 else f"failed (rc={code}): {err.strip()}"
            self._note(f"permit {decision} {permit.label}: {result}", "success" if code == 0 else "error.text")
        await self._refresh_permits()
        self._bump_poll()

    @work(exclusive=True, group="new")  # push_screen_wait only runs inside a worker
    async def action_new_agent(self) -> None:
        prompt, directory, provider = await self.push_screen_wait(NewAgent(self.agent.provider if self.agent else ""))
        if not prompt:
            return
        if not os.path.isdir(directory):
            self._note(f"new agent: {directory} is not a directory", "error.text")
            return
        code, out, err = await run_paseo(run_argv(prompt, provider), cwd=directory)
        agent_id = out.strip().splitlines()[-1] if out.strip() else ""
        if code != 0 or not agent_id:
            self._note(f"run failed (rc={code}): {(err or out).strip()}", "error.text")
            return
        self._note(f"started agent {agent_id[:7]} in {directory}", "success")
        self._choose_agent(agent_id)

    @work(exclusive=True, group="stop")  # push_screen_wait only runs inside a worker
    async def action_stop_agent(self) -> None:
        if self.agent is None:
            return
        if not await self.push_screen_wait(ConfirmStop(self.agent)):
            return
        code, _out, err = await run_paseo(stop_argv(self.agent.id))
        if code == 0:
            self._bump_poll()
            self._note(f"stopped {self.agent.name}", "status")
        else:
            self._note(f"stop failed (rc={code}): {err.strip()}", "error.text")

    def action_history_prev(self) -> None:
        self._recall(-1)

    def action_history_next(self) -> None:
        self._recall(1)

    def _recall(self, step: int) -> None:
        if not self._history:
            return
        self._history_pos = max(0, min(len(self._history), self._history_pos + step))
        prompt = self.query_one("#prompt", PromptArea)
        prompt.text = self._history[self._history_pos] if self._history_pos < len(self._history) else ""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="herdr4paseo",
        description="Log view and prompt box for one Paseo agent, via the paseo CLI.",
    )
    parser.add_argument("agent", nargs="?", help="agent id, short id or prefix; omit to pick from a list")
    parser.add_argument("--tail", type=int, default=50, help="log entries to show on attach (default 50)")
    parser.add_argument(
        "--behavior",
        choices=BEHAVIORS,
        default="steer",
        help="how sends treat a running turn (default steer; Ctrl+T toggles)",
    )
    args = parser.parse_args()
    Herdr4PaseoApp(args.agent, tail=args.tail, behavior=args.behavior).run()


if __name__ == "__main__":
    main()
