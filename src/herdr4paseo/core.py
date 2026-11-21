"""Pure helpers: build `paseo` argv lists, parse agent listings, style log lines.

Nothing here touches the terminal or starts a process, so it is all unit-testable.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pygments.token import Token
from rich.color import Color
from rich.style import Style
from rich.text import Text

BEHAVIORS = ("steer", "interrupt")
HISTORY_LIMIT = 200

# A tag is `[Name]` at the start of a line followed by whitespace or the end of the line, so a
# markdown link such as `[docs](https://x)` is not mistaken for one.
_TAG = re.compile(r"^\[([^\]\n]+)\](?=\s|$)")
_FOLLOW_BANNER = re.compile(r"^--- Following logs \(last (\d+) entries; Ctrl\+C to stop\) ---$")

# Semantic style tokens. The vocabulary (user message, thinking text, tool title/output,
# diff added/removed/context, markdown, syntax) is adapted from pi's theme tokens
# (https://github.com/earendil-works/pi, MIT, (c) 2025 Mario Zechner,
# packages/coding-agent/src/modes/interactive/theme/theme-tokens.ts). The colour values are the
# dark shades of herdr's built-in `kanagawa` palette (https://github.com/herdrdev/herdr,
# Apache-2.0, src/app/state.rs `Palette::kanagawa`; the Kanagawa colours are MIT, rebelot/kanagawa.nvim).
# Like herdr (`sidebar_bg: Color::Reset`) the app leaves the terminal's own background alone and
# only darkens the text colours: a deep red and a dull gold instead of the bright red and orange
# that the ANSI names give in many terminal palettes. They are hex; Rich downgrades them to 256
# or 16 colours on a terminal that cannot show them. Meaning must also survive NO_COLOR: tags
# stay in the text, diff lines keep their +/-, and the tag/thought/quote tokens carry bold or
# italic, which Textual's NO_COLOR filter keeps.
# The `status.*` tokens follow herdr's agent-state mapping (src/client/shell.rs `status_color`:
# working yellow, blocked red, idle green).
KANAGAWA = {
    "red": "#C34043",
    "yellow": "#C0A36E",
    "green": "#76946A",
    "mauve": "#957FB8",
    "blue": "#7E9CD8",
    "teal": "#7FB4CA",
    "subtext": "#C8C3AA",
    "overlay1": "#87867D",
    "overlay0": "#727169",
    "surface": "#2A2A37",
}
_K = KANAGAWA
THEME: dict[str, str] = {
    "user.tag": f"bold {_K['blue']}",
    "user.text": _K["blue"],
    "assistant.text": "",  # terminal default
    "thought": f"italic {_K['overlay1']}",
    "tool.tag": f"bold {_K['yellow']}",
    "tool.detail": _K["subtext"],
    "tool.result": _K["overlay1"],
    "tool.failed": f"bold {_K['red']}",  # reserved for a structured stream; `paseo logs` prints no results
    "permission": f"bold {_K['yellow']}",
    "error.tag": f"bold {_K['red']}",
    "error.text": _K["red"],
    "success": _K["green"],
    "status": _K["mauve"],
    "meta": _K["overlay0"],
    "diff.add": _K["green"],
    "diff.remove": _K["red"],
    "diff.hunk": f"bold {_K['blue']}",
    "diff.context": _K["overlay1"],
    "md.heading": f"bold {_K['teal']}",
    "md.code": _K["mauve"],
    "md.fence": _K["overlay0"],
    "md.bullet": _K["mauve"],
    "md.quote": f"italic {_K['overlay1']}",
    "status.running": _K["yellow"],
    "status.idle": _K["green"],
    "status.error": _K["red"],
    "status.closed": _K["overlay0"],
}
# Pygments style for lines inside a fenced code block (Pygments has no Kanagawa; its Nord style
# is the closest muted one). Only its foreground colour and bold/italic/underline are used: its
# background and default text colour are dropped so code sits on the terminal's own colours.
CODE_THEME = "nord"

# Bracketed lines `paseo logs` prints that are not tool calls. `Reconnected; ...` and
# `Timeline replaced; ...` carry detail after a semicolon, so they match by prefix.
META_TAGS = frozenset({"compacted", "earlier chat history omitted"})
_META_PREFIXES = ("reconnected;", "timeline replaced;")

_STATUS_TOKENS = {
    "running": "status.running",
    "idle": "status.idle",
    "error": "status.error",
    "closed": "status.closed",
    "archived": "status.closed",
    "stopped": "status.closed",
}


def style_for(token: str) -> str:
    """The Rich style string for a token; an unknown token is a bug and raises KeyError."""
    return THEME[token]


def status_style(status: str) -> str:
    """Style for an agent status word (running yellow, idle green, error red, closed dim)."""
    token = _STATUS_TOKENS.get(status.lower())
    return style_for(token) if token else ""


def paseo_bin() -> str:
    """The paseo executable; PASEO_BIN overrides it (tests use a fake)."""
    return os.environ.get("PASEO_BIN", "paseo")


@dataclass(frozen=True)
class Agent:
    id: str
    short_id: str
    name: str
    provider: str
    status: str
    cwd: str

    @property
    def label(self) -> str:
        where = f"  {self.cwd}" if self.cwd else ""
        return sanitize_line(f"{self.short_id}  {self.status:<8} {self.name}  ({self.provider}){where}")


def parse_agents(raw: str) -> list[Agent]:
    """Parse `paseo ls --json`; tolerate missing keys, reject non-list output."""
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array from `paseo ls --json`")
    agents = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict) or not item.get("id") or str(item["id"]) in seen:
            continue  # a malformed or duplicate entry must not take the picker (or the app) down
        seen.add(str(item["id"]))
        agent_id = str(item.get("id", ""))
        agents.append(
            Agent(
                id=agent_id,
                short_id=str(item.get("shortId") or agent_id[:7]),
                name=str(item.get("name", "")),
                provider=str(item.get("provider", "")),
                status=str(item.get("status", "?")),
                cwd=str(item.get("cwd", "")),
            )
        )
    return agents


def find_agent(agents: list[Agent], ident: str) -> Agent | None:
    """Match a full id, a short id, or an unambiguous id prefix."""
    exact = [a for a in agents if ident in (a.id, a.short_id)]
    if exact:
        return exact[0]
    prefixed = [a for a in agents if a.id.startswith(ident)]
    return prefixed[0] if len(prefixed) == 1 else None


# The follow file must hold a full agent UUID: a short id or prefix could match another agent
# (find_agent falls back to a unique prefix) if a writer is caught half way through.
FOLLOW_ID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
FOLLOW_FILE_MAX_BYTES = 4096


def read_follow_id(path: str) -> tuple[str | None, str]:
    """Read the one full agent id (lower-cased) held in `path`: (id, "") or (None, why it cannot be used yet).

    Never raises, and never waits for a FIFO's writer (it opens non-blocking and refuses anything but a
    regular file; a hung network mount can still stall the read). A missing, unreadable, special, empty
    or garbled file is a state to wait out, not a reason to close the tab. Surrounding whitespace and
    blank lines are ignored.
    """
    try:
        # O_NONBLOCK: opening a FIFO must not wait for a writer (it would freeze the event loop)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, f"follow file {path} does not exist yet; waiting for it"
    except OSError as exc:
        return None, f"cannot read follow file {path}: {exc.strerror or exc}; waiting"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, f"follow file {path} is not a regular file; waiting"
        raw = os.read(fd, FOLLOW_FILE_MAX_BYTES + 1)
    except OSError as exc:
        return None, f"cannot read follow file {path}: {exc.strerror or exc}; waiting"
    finally:
        os.close(fd)
    if len(raw) > FOLLOW_FILE_MAX_BYTES:
        return None, f"follow file {path} is over {FOLLOW_FILE_MAX_BYTES} bytes, not an agent id; waiting"
    try:
        lines = [line.strip() for line in raw.decode().splitlines() if line.strip()]
    except UnicodeDecodeError:
        return None, f"follow file {path} is not text, not an agent id; waiting"
    if not lines:
        return None, f"follow file {path} is empty; waiting for an agent id"
    if len(lines) > 1 or not FOLLOW_ID_RE.fullmatch(lines[0]):
        return None, f"follow file {path} does not hold a single agent id (a full UUID); waiting"
    return lines[0].lower(), ""


def find_followed_agent(agents: list[Agent], ident: str) -> Agent | None:
    """The agent whose full id is `ident`. Unlike find_agent there is no short-id or prefix match."""
    return next((a for a in agents if a.id.lower() == ident), None)


def filter_agents(agents: list[Agent], query: str) -> list[Agent]:
    """Agents whose label contains every whitespace-separated word of `query` (case-insensitive)."""
    words = query.lower().split()
    return [a for a in agents if all(w in a.label.lower() for w in words)]


def should_alert(old_status: str | None, new_status: str, old_permits: int, new_permits: int) -> str | None:
    """Why to ring the bell, or None. Only transitions alert, never a steady state."""
    if new_permits > old_permits:
        return "permission requested"
    if old_status == "running" and new_status == "idle":
        return "agent idle"
    return None


def history_path() -> Path:
    """Prompt history file; HERDR4PASEO_HISTORY overrides it (tests)."""
    override = os.environ.get("HERDR4PASEO_HISTORY")
    if override:
        return Path(override)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "herdr4paseo" / "history.json"


def load_history(path: Path, limit: int = HISTORY_LIMIT) -> list[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, str)][-limit:]


def save_history(path: Path, history: list[str], limit: int = HISTORY_LIMIT) -> None:
    """Best effort: a history file that cannot be written must never break sending."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(history[-limit:]))
        tmp.replace(path)
    except OSError:
        pass


@dataclass(frozen=True)
class Permission:
    id: str
    agent_id: str
    name: str
    description: str

    @property
    def label(self) -> str:
        return sanitize_line(f"{self.name}: {self.description}" if self.description not in ("", "-") else self.name)


def parse_permissions(raw: str, agent_id: str) -> list[Permission]:
    """Parse `paseo permit ls --json` (every agent's pending requests); keep one agent's."""
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("expected a JSON array from `paseo permit ls --json`")
    return [
        Permission(
            id=str(item.get("id", "")),
            agent_id=str(item.get("agentId", "")),
            name=str(item.get("name", "?")),
            description=str(item.get("description", "")),
        )
        for item in data
        if isinstance(item, dict) and item.get("agentId") == agent_id
    ]


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    argument_hint: str
    kind: str  # "command" or "skill"

    @property
    def label(self) -> str:
        hint = f" {self.argument_hint}" if self.argument_hint else ""
        return sanitize_line(f"/{self.name}{hint}  {self.description}")


_COMMAND_NAME = re.compile(r"[A-Za-z0-9_.:/-]+", re.ASCII)


PERMIT_BATCH = 1  # one allow-dialog shows (and answers) a single request, so it always fits the screen
PERMIT_LABEL_MAX = 200


def permit_batch(permits: list[Permission], decision: str) -> list[Permission]:
    """The requests one confirmation answers. Allow answers only requests whose full text the dialog
    can show (at most PERMIT_BATCH, each at most PERMIT_LABEL_MAX characters); the rest stay pending.
    Deny answers every request: denying one you have not read is safe."""
    if decision != "allow":
        return list(permits)
    return [p for p in permits if len(p.label) <= PERMIT_LABEL_MAX][:PERMIT_BATCH]


def clip_label(label: str, limit: int = 160) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "\u2026"


def parse_commands(items: list[object]) -> list[Command]:
    """Parse the `commands` array of a list_commands response; skip malformed entries."""
    commands = []
    for item in items:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        if not _COMMAND_NAME.fullmatch(str(item["name"])):
            continue  # Tab completion would insert this name into the prompt: letters, digits and . _ : / - only
        commands.append(
            Command(
                name=str(item["name"]),
                description=str(item.get("description") or ""),
                argument_hint=str(item.get("argumentHint") or ""),
                kind=str(item.get("kind") or "command"),
            )
        )
    return sorted(commands, key=lambda c: c.name.lower())


def match_commands(commands: list[Command], text: str, limit: int = 8) -> list[Command]:
    """Commands to suggest for a prompt of `/partial`: prefix matches first, then substrings.

    Only a single-line prompt that starts with "/" and has no space yet is a command being typed.
    """
    if not text.startswith("/") or any(ch.isspace() for ch in text):
        return []
    needle = text[1:].lower()
    prefix = [c for c in commands if c.name.lower().startswith(needle)]
    inside = [c for c in commands if needle in c.name.lower() and c not in prefix]
    return (prefix + inside)[:limit]


def permit_ls_argv() -> list[str]:
    return [paseo_bin(), "permit", "ls", "--json"]


def permit_argv(decision: str, agent_id: str, request_id: str) -> list[str]:
    """Answer exactly one request. Never `allow <agent>` alone: that approves everything
    pending, including requests that arrived after the user looked."""
    if decision not in ("allow", "deny"):
        raise ValueError(f"decision must be allow or deny, got {decision!r}")
    return [paseo_bin(), "permit", decision, agent_id, request_id]


def run_argv(prompt: str, provider: str) -> list[str]:
    """Start an agent without waiting; --quiet makes paseo print just the new agent's id.

    `paseo run` errors with "Provider is required" without --provider (seen live 2026-10-30).

    The working directory comes from where the process runs, not a flag.
    """
    if prompt.startswith("-"):
        prompt = " " + prompt  # same flag-lookalike guard as send_argv
    return [paseo_bin(), "run", "--no-wait", "--quiet", "--provider", provider, prompt]


def stop_argv(agent_id: str) -> list[str]:
    return [paseo_bin(), "stop", agent_id]


def ls_argv() -> list[str]:
    # -g: all workspaces; -a: archived too, so an agent you attached to stays findable.
    return [paseo_bin(), "ls", "-g", "-a", "--json"]


def ls_active_argv() -> list[str]:
    # Without -a, archived agents are left out: an id in `ls_argv` output but not here is archived.
    return [paseo_bin(), "ls", "-g", "--json"]


def logs_argv(agent: str, *, tail: int, tools_only: bool) -> list[str]:
    argv = [paseo_bin(), "logs", agent, "--no-color", "--tail", str(tail), "-f"]
    if tools_only:
        argv += ["--filter", "tools"]
    return argv


def send_argv(agent: str, text: str, behavior: str) -> list[str]:
    if behavior not in BEHAVIORS:
        raise ValueError(f"behavior must be one of {BEHAVIORS}, got {behavior!r}")
    # A prompt that starts with "-" would be parsed as a flag; a leading space is
    # harmless to the agent and keeps the argv free of a "--" the CLI may not accept.
    if text.startswith("-"):
        text = " " + text
    return [paseo_bin(), "send", agent, "--no-wait", "--behavior", behavior, text]


_HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])(?=\s)")
_QUOTE = re.compile(r"^\s*>")
_INLINE_CODE = re.compile(r"`[^`\n]+`")
_LINK = re.compile(r"\[(?P<label>[^\[\]\n]+)\]\([^()\s]+\)")  # only the label is highlighted, not the [](url) syntax
# A bare URL: only the host (the FQDN, without a leading "www." or a port) is highlighted.
_BARE_URL = re.compile(r"https?://(?P<host>[^\s/:?#)\]>\"'<`,;]+)", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```\s*([\w+#.-]*)")
_TASK = re.compile(r"^\s*- \[[ xX]\]")
_TASK_DONE = re.compile(r"^\s*- \[[xX]\]")
_PATH_OR_URL = re.compile(r"https?://[^\s)\]>\"']+|(?<![\w/.])(?:~|\.{1,2})?/[\w.@+\-/]+")
_DIFF_LANGS = frozenset({"diff", "patch", "udiff"})


def _brighten(color: Color, amount: float) -> Color:
    """The colour mixed `amount` of the way toward white."""
    r, g, b = color.get_truecolor()
    return Color.from_rgb(*(round(c + (255 - c) * amount) for c in (r, g, b)))


# "Vibrant" colour for emphasised text that has no colour of its own: herdr's Kanagawa blue, brightened.
_VIBRANT = _brighten(Color.parse(KANAGAWA["blue"]), 0.3)


def escalate(surrounding: Style) -> Style:
    """The style for a highlighted link, one step stronger than the text around it.

    normal -> italic, italic -> bold italic, bold -> vibrant bold, bold italic -> vibrant bold
    italic. If the surrounding text has its own colour, the highlight keeps it but brighter; if
    it has none (the terminal's default text colour), only the bold steps add a colour, so a
    highlighted link in plain text is italic without changing colour. Bold and italic survive
    NO_COLOR, so the highlight is still visible there.
    """
    own = surrounding.color if surrounding.color is not None and not surrounding.color.is_default else None
    if surrounding.bold:
        return Style(color=_brighten(own, 0.35) if own else _VIBRANT, bold=True, italic=bool(surrounding.italic))
    return Style(color=_brighten(own, 0.3) if own else None, bold=bool(surrounding.italic), italic=True)


def _style_at(text: Text, offset: int) -> Style:
    """The style a character of `text` has so far: the text's base style plus every span over it."""
    style = text.style if isinstance(text.style, Style) else Style.parse(text.style or "")
    for span in text.spans:
        if span.start <= offset < span.end:
            style += span.style if isinstance(span.style, Style) else Style.parse(span.style)
    return style


MAX_HIGHLIGHT_LINE = 5_000


def _highlight_links(text: Text, line: str) -> None:
    """Escalate the label of `[label](url)` links and the host of bare URLs; the rest stays as it was."""
    if len(line) > MAX_HIGHLIGHT_LINE:  # the span scan below is quadratic in the number of URLs
        return
    markdown = [m.span() for m in _LINK.finditer(line)]
    code = [m.span() for m in _INLINE_CODE.finditer(line)]
    spans = [m.span("label") for m in _LINK.finditer(line)]
    for found in _BARE_URL.finditer(line):
        if any(a <= found.start() < b for a, b in markdown + code):
            continue
        start, end = found.span("host")
        end = start + len(found.group("host").rstrip("."))
        if line[start : start + 4].lower() == "www.":
            start += 4
        if start < end:
            spans.append((start, end))
    for start, end in spans:
        text.stylize(escalate(_style_at(text, start)), start, end)


def _style(text: Text, token: str, start: int = 0, end: int | None = None) -> None:
    style = style_for(token)
    if style:
        text.stylize(style, start, len(text) if end is None else end)


class LogStyler:
    """Colour `paseo logs` lines one at a time, remembering what the previous line was.

    `paseo logs` tags only user, thought and tool entries; assistant prose has no tag. So the
    state is the kind of the last tagged block (continuation lines of a `[User]` or `[Thought]`
    entry carry no tag) plus the language of the open code fence, if any. One styler per
    stream: make a new one whenever the log view is cleared.

    A multi-line `[User]` prompt followed directly by untagged assistant text cannot be told
    apart, so that text keeps the user colour until the next tag.
    """

    def __init__(self) -> None:
        self.block = "assistant"  # user, thought, tasks or assistant
        self.fence_lang: str | None = None  # None outside a fence; "" for a fence without a language
        self._syntax = None

    def style(self, line: str) -> Text:
        line = sanitize_text(line)
        banner = _FOLLOW_BANNER.match(line)
        if banner:
            return Text(f"--- following live (last {banner.group(1)} entries) ---", style=style_for("meta"))
        tag = _TAG.match(line)
        if tag:
            return self._tagged(line, tag)
        return self._untagged(line)

    # -- tagged entries -------------------------------------------------------------

    def _tagged(self, line: str, tag: re.Match[str]) -> Text:
        name = tag.group(1).lower()
        text = Text(line)
        end = tag.end()
        self.fence_lang = None
        self._syntax = None
        if name in META_TAGS or name.startswith(_META_PREFIXES):
            self.block = "assistant"
            _style(text, "meta")
        elif name == "user":
            self.block = "user"
            _style(text, "user.tag", 0, end)
            _style(text, "user.text", end)
            _highlight_links(text, line)
        elif name == "thought":
            self.block = "thought"
            _style(text, "thought")
            _highlight_links(text, line)
        elif name == "error":
            self.block = "assistant"
            _style(text, "error.tag", 0, end)
            _style(text, "error.text", end)
        elif name == "tasks":
            self.block = "tasks"
            _style(text, "tool.tag", 0, end)
            _style(text, "tool.detail", end)
        else:  # a tool call: Shell, Read, Edit, an MCP tool name, ...
            self.block = "assistant"  # tool entries are one line
            _style(text, "tool.tag", 0, end)
            _style(text, "tool.detail", end)
            for found in _PATH_OR_URL.finditer(line, end):
                text.stylize("underline", found.start(), found.end())
        return text

    # -- continuation and assistant lines -------------------------------------------

    def _untagged(self, line: str) -> Text:
        text = Text(line)
        if self.block == "user":
            _style(text, "user.text")
            _highlight_links(text, line)
            return text
        if self.block == "thought":
            _style(text, "thought")
            _highlight_links(text, line)
            return text
        if self.block == "tasks":
            if _TASK.match(line):
                if _TASK_DONE.match(line):
                    _style(text, "success")
                return text
            self.block = "assistant"
        return self._assistant(line, text)

    def _assistant(self, line: str, text: Text) -> Text:
        fence = _FENCE.match(line)
        if fence:
            if self.fence_lang is None:
                self.fence_lang = fence.group(1).lower()
                self._syntax = None
            else:
                self.fence_lang = None
                self._syntax = None
            _style(text, "md.fence")
            return text
        if self.fence_lang is not None:
            return self._in_fence(line, text)
        _style(text, "assistant.text")
        if _HEADING.match(line):
            _style(text, "md.heading")
        elif _QUOTE.match(line):
            _style(text, "md.quote")
        else:
            bullet = _BULLET.match(line)
            if bullet:
                _style(text, "md.bullet", 0, bullet.end())
        text.highlight_regex(_INLINE_CODE, style_for("md.code"))
        _highlight_links(text, line)
        return text

    def _in_fence(self, line: str, text: Text) -> Text:
        lang = self.fence_lang or ""
        if lang in _DIFF_LANGS:
            if line.startswith("@@"):
                _style(text, "diff.hunk")
            elif line.startswith(("+++", "---")):
                _style(text, "diff.hunk")
            elif line.startswith("+"):
                _style(text, "diff.add")
            elif line.startswith("-"):
                _style(text, "diff.remove")
            else:
                _style(text, "diff.context")
            return text
        if lang:
            if self._syntax is None:
                from rich.syntax import Syntax

                self._syntax = Syntax("", lang, theme=CODE_THEME)
            default = self._syntax.get_theme(CODE_THEME).get_style_for_token(Token.Text).color
            for span in self._syntax.highlight(line).spans:
                if span.start < len(line) and isinstance(span.style, Style):
                    plain = not (span.style.bold or span.style.italic or span.style.underline)
                    if plain and span.style.color == default:
                        continue  # the theme's default text colour: leave it to the terminal's own foreground
                    fg = Style(
                        color=span.style.color,
                        bold=span.style.bold,
                        italic=span.style.italic,
                        underline=span.style.underline,
                    )
                    text.stylize(fg, span.start, min(span.end, len(line)))
        return text


def style_line(line: str) -> Text:
    """Style one log line with no memory of earlier lines (see LogStyler for the stateful form)."""
    return LogStyler().style(line)


def agent_label_text(agent: Agent) -> Text:
    """The picker row for an agent, with the status word coloured like the header."""
    text = Text(agent.label)
    start = len(agent.short_id) + 2
    style = status_style(agent.status)
    if style:
        text.stylize(style, start, start + len(agent.status))
    return text


def timeline_argv(agent: str, *, tail: int, follow: bool = True) -> list[str]:
    """Structured timeline through the same CLI boundary as the text log."""
    return [paseo_bin(), "logs", agent, "--json", "--tail", str(tail)] + (["-f"] if follow else [])


MAX_RECORD_DEPTH = 64  # records nest a few levels; deeper is hostile (and recursion limits differ per platform)


def _nested_deeper_than(value: object, limit: int) -> bool:
    """Iterative depth check, so it needs no recursion and holds on every Python/platform."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children = node.values()
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return False


def parse_timeline(raw: str) -> list[dict]:
    """Accept one-shot envelopes and individual follow NDJSON records."""
    value = json.loads(raw)  # may itself raise RecursionError on a platform with a low C limit
    if not isinstance(value, dict):
        raise ValueError("timeline record is not an object")
    if len(raw) > 2 * MAX_RECORD_DEPTH and _nested_deeper_than(value, MAX_RECORD_DEPTH):
        raise ValueError("timeline record is nested too deeply")
    items = value.get("items", [value])
    if not isinstance(items, list) or any(not isinstance(i, dict) or not isinstance(i.get("type"), str) for i in items):
        raise ValueError("invalid timeline items")
    return items


# C0 (bar \n \t), DEL, C1, plus characters that reorder or hide text: bidi marks/embeddings/overrides/
# isolates, zero-width space, word joiner, BOM, line/paragraph separators. ZWJ/ZWNJ stay (emoji, scripts).
def _invisible_chars() -> str:
    """Every Cf (format) character except ZWJ/ZWNJ, variation selectors other than the emoji/text
    pair FE0E/FE0F, the Hangul fillers, plus the explicit bidi/separator/tag set. A category scan
    instead of a hand-written list, which kept having gaps."""
    keep = {"\u200c", "\u200d"}
    chars = {chr(c) for c in range(0x10000) if unicodedata.category(chr(c)) == "Cf"} - keep
    for lo, hi in ((0xFE00, 0xFE0D), (0x180B, 0x180D), (0xE0100, 0xE01EF), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
                   (0x13430, 0x1343F), (0xE0000, 0xE007F), (0x2028, 0x2029)):
        chars |= {chr(c) for c in range(lo, hi + 1)}
    chars |= set("\u115f\u1160\u3164\uffa0\u17b4\u17b5\u180e\u180f\u034f\u00ad")
    return "".join(re.escape(c) for c in sorted(chars))


_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f" + _invisible_chars() + "]")
MAX_TEXT_CHARS = 256_000  # displayed per timeline item; the full text stays in memory
MAX_DETAIL_CHARS = 64_000  # displayed tool-call detail


def _visible_control(match: re.Match[str]) -> str:
    code = ord(match.group())
    if code < 0x20:
        return chr(0x2400 + code)  # the Control Pictures glyph, e.g. U+241B for ESC
    return "\u2421" if code == 0x7F else "\ufffd"  # DEL, C1


def sanitize_text(text: str) -> str:
    """Make agent/tool text inert before it reaches Rich and the terminal.

    Rich strips only BEL/BS/VT/FF/CR and Textual writes the rest as is, so ESC, C1 and other
    control characters would let untrusted text drive the terminal (CSI, OSC 0/2 titles, OSC 8
    links, OSC 52 clipboard writes). They become visible stand-ins; `\n` and `\t` stay, CR is dropped.
    """
    return _CONTROLS.sub(_visible_control, text.replace("\r", ""))


def sanitize_line(text: str) -> str:
    """sanitize_text for one-line labels: newlines, tabs and CR become spaces (same length)."""
    return sanitize_text(re.sub(r"[\r\n\t]", " ", text))


def _clean(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    """str(value), clipped to a display budget, then sanitized (never raises on a wrong-typed field)."""
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        text = f"{text[:limit]}\n... {len(text) - limit} more characters not shown"
    return sanitize_text(text)


def render_timeline_item(item: dict) -> Text:
    """Role labels survive NO_COLOR; assistant markdown shares the log styler. All text is sanitized."""
    kind = item["type"]
    if kind == "assistant_message":
        result = Text("Assistant\n", style=style_for("md.heading"))
        styler = LogStyler()
        for line in _clean(item.get("text", "")).splitlines():
            result.append_text(styler.style(line))
            result.append("\n")
        return result
    if kind in {"user_message", "reasoning"}:
        label, token = ("You", "user.text") if kind == "user_message" else ("Reasoning", "thought")
        return Text(f"{label}\n{_clean(item.get('text', ''))}", style=style_for(token))
    if kind == "tool_call":
        status = _clean(item.get("status", "running"), 100)
        result = Text(f"{_clean(item.get('name', 'Tool'), 200)} · {status}", style=style_for("tool.failed" if status == "failed" else "tool.tag"))
        if item.get("detail"):
            detail = json.dumps(item["detail"], ensure_ascii=False, indent=2, default=str)
            result.append("\n" + _clean(detail, MAX_DETAIL_CHARS), style=style_for("tool.detail"))
        return result
    if kind == "todo":
        entries = item.get("items", [])
        rows = [t if isinstance(t, dict) else {"text": t} for t in (entries if isinstance(entries, list) else [entries])]
        body = "\n".join(f"[{'x' if t.get('completed') else ' '}] {_clean(t.get('text', t.get('description', '')), 2_000)}" for t in rows)
        return Text("Tasks\n" + body, style=style_for("tool.detail"))
    if kind in {"error", "notification"}:
        level = _clean(item.get("level", kind), 100)
        return Text(f"[{level}] {_clean(item.get('message', ''))}", style=style_for("error.text" if kind == "error" or level == "error" else "permission"))
    rest = json.dumps({k: v for k, v in item.items() if k != "type"}, ensure_ascii=False, default=str)
    return Text(f"[{_clean(kind, 100)}] " + _clean(rest, MAX_DETAIL_CHARS), style=style_for("meta"))


STORE_TEXT_LIMIT = 2 * MAX_TEXT_CHARS  # stored per streamed message; the display shows MAX_TEXT_CHARS of it


def _compact(item: dict) -> dict:
    """One size gate for every stored record: long strings are clipped to STORE_TEXT_LIMIT and any
    nested value whose dump passes 4*MAX_DETAIL_CHARS becomes its clipped dump (the display clips
    far below both)."""
    out = None
    for key, value in item.items():
        if isinstance(value, str):
            if len(value) > STORE_TEXT_LIMIT:
                out = out or dict(item)
                out[key] = value[:STORE_TEXT_LIMIT]
        elif isinstance(value, (dict, list)):
            dumped = json.dumps(value, ensure_ascii=False, default=str)
            if len(dumped) > 4 * MAX_DETAIL_CHARS:
                out = out or dict(item)
                out[key] = dumped[: 4 * MAX_DETAIL_CHARS] + "..."
    return out or item


STREAMING_TEXT_TYPES = frozenset({"assistant_message", "reasoning"})


def apply_timeline_item(timeline: list[dict], item: dict, open_chain: int | None) -> int | None:
    """Fold one `paseo logs --json` record into `timeline`; returns the new open text chain index.

    Follow-mode records are deltas, not snapshots (upstream `createFollowTranscriptWriter` does
    `chain.buffer += item.text`, `createFollowJsonWriter` emits each item unchanged), so
    assistant/reasoning text is appended. A chain continues only while the previous record was
    the same type; an assistant fragment continues it when it has no `messageId` or the same one
    (upstream `continuesChain`); any other record closes it. Tool calls are snapshots keyed by
    `callId`: an update replaces its earlier record in place.
    """
    kind = item["type"]
    if kind in STREAMING_TEXT_TYPES:
        if open_chain is not None:
            old = timeline[open_chain]
            same_message = kind != "assistant_message" or not item.get("messageId") or item.get("messageId") == old.get("messageId")
            if old["type"] == kind and same_message:
                if len(old.get("text", "")) >= STORE_TEXT_LIMIT:
                    return open_chain  # stored text is capped (the display clips earlier); stop growing it
                timeline[open_chain] = {**old, "text": (old.get("text", "") + str(item.get("text", "")))[:STORE_TEXT_LIMIT]}
                return open_chain
        timeline.append(dict(_compact(item)))
        return len(timeline) - 1
    item = _compact(item)
    call_id = item.get("callId")
    if kind == "tool_call" and call_id:
        for i, old in enumerate(timeline):
            if old["type"] == kind and old.get("callId") == call_id:
                timeline[i] = item
                return None
    timeline.append(item)
    return None


MAX_TIMELINE_ITEMS = 500  # kept in memory; the oldest 100 go at a time so the render cache stays valid
TRIM_BATCH = 100


def trim_timeline(timeline: list[dict], open_chain: int | None) -> tuple[int | None, int]:
    """Drop the oldest TRIM_BATCH items once the timeline passes MAX_TIMELINE_ITEMS + TRIM_BATCH."""
    if len(timeline) <= MAX_TIMELINE_ITEMS + TRIM_BATCH:
        return open_chain, 0
    drop = (len(timeline) - MAX_TIMELINE_ITEMS) // TRIM_BATCH * TRIM_BATCH
    del timeline[:drop]
    return (None if open_chain is None or open_chain < drop else open_chain - drop), drop


MAX_RECORD_BYTES = 8 * 1024 * 1024  # one `paseo logs --json` record; larger ones are skipped, not buffered
MAX_LOG_LINE_BYTES = 1024 * 1024  # one plain-log line or stderr line


async def iter_record_lines(stream, max_bytes: int = MAX_RECORD_BYTES, chunk_size: int = 1 << 16):
    """Yield newline-terminated lines from an asyncio stream with no 64 KiB `readline()` limit.

    A line longer than `max_bytes` is dropped (memory stays bounded) and yielded as `None` once,
    so the caller can show a notice and keep going. A final unterminated line is still yielded.
    """
    pending = bytearray()
    skipping = False
    while chunk := await stream.read(chunk_size):
        while chunk:
            end = chunk.find(b"\n")
            piece, chunk = (chunk, b"") if end < 0 else (chunk[: end + 1], chunk[end + 1 :])
            if not skipping:
                pending += piece
                if len(pending) > max_bytes + 1:
                    pending.clear()
                    skipping = True
            if end >= 0:
                if skipping:
                    skipping = False
                    yield None
                else:
                    yield bytes(pending)
                    pending.clear()
    if skipping:
        yield None
    elif pending:
        yield bytes(pending)
