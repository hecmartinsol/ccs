#!/usr/bin/env python3
"""ccs — minimal TUI to browse and resume Claude Code sessions.

Reads ~/.claude/projects/*/*.jsonl, caches the index by mtime/size and on
Enter does chdir to the session's folder + `claude --resume <id>`.
No dependencies: stdlib only (curses).
"""
import curses
import glob
import json
import locale
import os
import re
import subprocess
import sys
import textwrap
import unicodedata
import zlib
from datetime import datetime, timezone

__version__ = "0.1.0"

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CACHE_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "ccs", "index.json"
)
CACHE_VERSION = 3
MAX_TEXT = 300

NOISE_PREFIXES = ("<system-reminder>", "<local-command", "<bash-", "Caveat:", "<task-notification>",
                  "[Request interrupted")
IGNORED_COMMANDS = {"/clear", "/compact", "/exit", "/resume"}
CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


# ---------------------------------------------------------------- parsing

def _user_text(rec):
    """Visible text of a real user message, or None if it is noise."""
    if rec.get("isMeta") or rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text:
        return None
    m = CMD_NAME.search(text)
    if m:
        args = CMD_ARGS.search(text)
        text = (m.group(1) + " " + (args.group(1) if args else "")).strip()
        if text in IGNORED_COMMANDS:
            return None
    elif text.startswith(NOISE_PREFIXES):
        return None
    return " ".join(text.split())[:MAX_TEXT]


def parse_session(path):
    """Extract metadata from a session .jsonl. None if it has no real prompts."""
    s = {"id": os.path.basename(path)[:-len(".jsonl")], "title": "", "first": "", "last": "",
         "cwd": "", "created": "", "updated": "", "entrypoint": "", "prompts": 0}
    ai_title = custom_title = ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            if t == "custom-title":
                custom_title = rec.get("customTitle") or custom_title
            elif t == "ai-title":
                ai_title = rec.get("aiTitle") or ai_title
            ts = rec.get("timestamp")
            if ts:
                if not s["created"] or ts < s["created"]:
                    s["created"] = ts
                if ts > s["updated"]:
                    s["updated"] = ts
            if not s["cwd"] and rec.get("cwd"):
                s["cwd"] = rec["cwd"]
            # a session launched by the SDK and resumed by hand counts as interactive
            ep = rec.get("entrypoint")
            if ep and (not s["entrypoint"] or ep == "cli"):
                s["entrypoint"] = ep
            if t == "user":
                text = _user_text(rec)
                if text:
                    s["prompts"] += 1
                    s["first"] = s["first"] or text
                    s["last"] = text
    if not s["prompts"]:
        return None
    s["title"] = custom_title or ai_title
    return s


def load_sessions(projects_dir=PROJECTS_DIR, cache_file=CACHE_FILE, progress=None):
    """List of sessions, re-parsing only new or modified files."""
    try:
        with open(cache_file) as f:
            cache = json.load(f)
        if cache.get("v") != CACHE_VERSION:
            raise ValueError
    except (OSError, ValueError):
        cache = {"v": CACHE_VERSION, "files": {}}
    old = cache["files"]
    new = {}
    paths = glob.glob(os.path.join(glob.escape(projects_dir), "*", "*.jsonl"))
    changed = False
    for i, path in enumerate(paths):
        try:
            st = os.stat(path)
        except OSError:
            continue
        entry = old.get(path)
        if entry and entry["m"] == st.st_mtime and entry["s"] == st.st_size:
            new[path] = entry
            continue
        if progress:
            progress(i + 1, len(paths))
        new[path] = {"m": st.st_mtime, "s": st.st_size, "d": parse_session(path)}
        changed = True
    if changed or len(new) != len(old):
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp = cache_file + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"v": CACHE_VERSION, "files": new}, f)
        os.replace(tmp, cache_file)
    sessions = [e["d"] for e in new.values() if e["d"]]
    sessions.sort(key=lambda s: s["updated"], reverse=True)
    return sessions


def filter_rows(rows, query, project=None):
    terms = query.lower().split()
    out = []
    for r in rows:
        if project and r.get("project") != project:
            continue
        hay = " ".join((r["title"], r["first"], r["last"], r["cwd"], r["id"])).lower()
        if all(t in hay for t in terms):
            out.append(r)
    return out


MODES = ("local", "crons", "agents")


def load_agents(timeout=5):
    """Background agents from `claude agents --json --all`, keyed by session id.

    Interactive sessions are not agents. Any failure (no claude, old version,
    bad output) just means no agents.
    """
    try:
        out = subprocess.run(["claude", "agents", "--json", "--all"], capture_output=True,
                             text=True, timeout=timeout, check=True).stdout
        data = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    if not isinstance(data, list):
        return {}
    return {a["sessionId"]: a for a in data
            if isinstance(a, dict) and a.get("sessionId") and a.get("kind") != "interactive"}


def is_alive(agent):
    """True if the agent's process is still running."""
    try:
        os.kill(int(agent["pid"]), 0)
    except (KeyError, TypeError, ValueError, OSError):
        return False
    return True


def mode_rows(sessions, mode):
    """Sessions shown in a Tab view: local (default), crons (sdk-cli), agents (background)."""
    if mode == "agents":
        return [s for s in sessions if s.get("agent")]
    crons = mode == "crons"
    return [s for s in sessions if not s.get("agent") and (s["entrypoint"] == "sdk-cli") == crons]


COLUMNS = ("PROJECT", "SESSION", "DESCRIPTION", "CREATED", "UPDATED")
SORT_KEYS = (
    lambda r: r["project"].lower(),
    lambda r: (r["title"] or r["id"]).lower(),
    lambda r: r["first"].lower(),
    lambda r: r["created"],
    lambda r: r["updated"],
)


def sort_rows(rows, col, desc):
    return sorted(rows, key=SORT_KEYS[col], reverse=desc)


# ---------------------------------------------------------------- projects

CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "ccs", "config.json"
)

# Single-codepoint, double-width emojis (no variation selector) so the table
# stays aligned. The user's config takes precedence over these.
DEFAULT_EMOJIS = {"~": "🏠"}
DEFAULT_RULES = [  # (substring of the name, emoji); first match wins
    ("agent", "🤖"), ("ai-", "🤖"), ("claude", "🧠"),
    ("cash", "🪙"), ("coin", "💰"), ("bank", "🏦"), ("finan", "💰"),
    ("voice", "🎤"), ("whisper", "🎤"), ("dictate", "🎤"),
    ("token", "📊"), ("digest", "📰"), ("release", "🚀"), ("deploy", "🚀"), ("script", "📜"),
    ("obsidian", "📝"), ("note", "📝"), ("doc", "📁"), ("download", "📥"), ("dropbox", "📦"),
    ("garmin", "🏃"), ("strava", "🏃"), ("sql", "💾"), ("db", "💾"),
    ("contract", "📑"), ("api", "🔌"), ("oncall", "🚨"), ("alert", "🚨"),
    ("infra", "🧱"), ("test", "🧪"), ("tmp", "🧪"), ("scratch", "🧪"), ("probe", "🧪"),
    ("web", "🌐"), ("site", "🌐"), ("blog", "📰"), ("game", "🎮"), ("bot", "🤖"),
]
EMOJI_POOL = ["🔵", "🟣", "🟠", "🟡", "🟤", "🔶", "🔷", "🍀", "🌵", "🐙", "🦊", "🐝",
              "🦉", "🍉", "🍋", "🌈", "⭐", "🎲", "🧩", "🛸", "🐳", "🦀", "🍄", "🌻"]

WORKSPACES = []
EMOJIS = dict(DEFAULT_EMOJIS)
RULES = list(DEFAULT_RULES)


def load_config(path=CONFIG_FILE):
    """Optional user JSON config; {} if missing."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SystemExit(f"ccs: invalid config in {path}: {e}")


def apply_config(cfg):
    """workspaces: folders whose top-level children are projects.
    emojis: {project: emoji}.  rules: [[substring, emoji], ...] (before the built-in ones)."""
    global WORKSPACES, EMOJIS, RULES
    WORKSPACES = [os.path.expanduser(w).rstrip("/") for w in cfg.get("workspaces", [])]
    EMOJIS = {**DEFAULT_EMOJIS, **cfg.get("emojis", {})}
    RULES = [tuple(r) for r in cfg.get("rules", [])] + DEFAULT_RULES


WORKTREE_MARK = "/.claude/worktrees/"


def project_key(cwd):
    """Project of a folder: top-level child of a workspace, or its basename.
    Claude Code worktrees (<repo>/.claude/worktrees/x) count as their repo."""
    if not cwd:
        return "?"
    cwd = cwd.rstrip("/").split(WORKTREE_MARK)[0]
    if cwd == os.path.expanduser("~"):
        return "~"
    for ws in WORKSPACES:
        if cwd.startswith(ws + "/"):
            return cwd[len(ws) + 1:].split("/")[0]
    return os.path.basename(cwd) or cwd


def project_emoji(name):
    if name in EMOJIS:
        return EMOJIS[name]
    low = name.lower()
    for needle, emoji in RULES:
        if needle in low:
            return emoji
    return EMOJI_POOL[zlib.crc32(name.encode()) % len(EMOJI_POOL)]


def initial_filters(argv, cwd):
    """`ccs . text` → (text, project of cwd). Args that are paths set the project."""
    project, words = None, []
    for arg in argv:
        if arg == "." or arg.startswith(("/", "./", "../", "~")):
            path = os.path.expanduser(arg)
            project = project_key(os.path.abspath(os.path.join(cwd, path)))
        else:
            words.append(arg)
    return " ".join(words), project


def project_list(rows):
    """[(project, session count)] sorted by each project's most recent session."""
    latest, count = {}, {}
    for r in rows:
        p = r["project"]
        count[p] = count.get(p, 0) + 1
        latest[p] = max(latest.get(p, ""), r["updated"])
    return [(p, count[p]) for p in sorted(latest, key=latest.get, reverse=True)]


def parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def rel_time(ts, now=None):
    if not ts:
        return ""
    now = now or datetime.now(timezone.utc)
    dt = parse_ts(ts)
    secs = (now - dt).total_seconds()
    if secs < 60:
        return "now"
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 30 * 86400:
        return f"{int(secs // 86400)}d"
    return dt.astimezone().strftime("%Y-%m-%d")


def short_path(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


# ---------------------------------------------------------------- UI

def char_width(ch):
    if unicodedata.combining(ch) or ch in "‍️":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def disp_width(text):
    return sum(char_width(c) for c in text)


def fit(text, width):
    """Truncate/pad to `width` terminal columns (emoji = 2)."""
    if width <= 0:
        return ""
    if disp_width(text) > width:
        out, used = "", 0
        for c in text:
            cw = char_width(c)
            if used + cw > width - 1:
                break
            out, used = out + c, used + cw
        text = out + "…"
    return text + " " * (width - disp_width(text))


HELP = "↑↓/jk move · / search · s sort · p project (type to filter) · x clear filters · Tab local/crons/agents · Enter resume · q quit"
HELP_SORT = "←→ column · ↑ asc · ↓ desc · Enter/Esc/s done"
HELP_SEARCH = "type to filter · ↑↓ move · Enter accept · Esc cancel"


def put(win, y, x, text, attr=0):
    """addstr clipped to the window width, without crashing on the last cell."""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            win.addstr(y, x, fit(text, w - 1 - x), attr)
        except curses.error:
            pass


def pick_project(stdscr, rows, current):
    """Popup with the distinct projects, filtered as you type.

    Returns the chosen one, None = all, False = cancelled.
    """
    all_items = [(None, len(rows))] + project_list(rows)
    h, w = stdscr.getmaxyx()

    def label(p):
        return "   (all)" if p is None else f"{project_emoji(p)} {p}"

    pw = min(w - 4, max(30, max(disp_width(label(p)) for p, _ in all_items) + 10))
    ph = min(h - 2, len(all_items) + 3)
    win = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
    win.keypad(True)
    text = ""
    items = all_items
    sel = next((i for i, (p, _) in enumerate(items) if p == current), 0)
    top = 0
    while True:
        items = [it for it in all_items if it[0] and text.lower() in it[0].lower()] \
            if text else all_items
        sel = max(0, min(sel, len(items) - 1))
        vis = ph - 3
        top = min(max(top, sel - vis + 1), sel)
        win.erase()
        win.box()
        put(win, 0, 2, " Project ", curses.A_BOLD)
        put(win, 1, 1, f" > {text}_", curses.color_pair(3))
        for i, (p, n) in enumerate(items[top:top + vis]):
            attr = curses.color_pair(1) if top + i == sel else curses.A_NORMAL
            put(win, 2 + i, 1, fit(f" {label(p)}", pw - 8) + f"{n:>5} ", attr)
        win.refresh()
        k = win.get_wch()
        if k in (curses.KEY_UP, "\x10"):
            sel -= 1
        elif k in (curses.KEY_DOWN, "\x0e"):
            sel += 1
        elif k == curses.KEY_PPAGE:
            sel -= vis
        elif k == curses.KEY_NPAGE:
            sel += vis
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if items:
                return items[sel][0]
        elif k == "\x1b":
            if not text:
                return False
            text, sel = "", 0
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            text, sel = text[:-1], 0
        elif k == "\x15":                         # Ctrl-U
            text, sel = "", 0
        elif isinstance(k, str) and k.isprintable():
            text, sel = text + k, 0


def run_ui(stdscr, sessions, query, project=None):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selection
    curses.init_pair(2, curses.COLOR_CYAN, -1)                   # header
    curses.init_pair(3, curses.COLOR_YELLOW, -1)                 # warning / filters
    mode = 0
    searching = False
    sorting = False
    sort_col, sort_desc = 4, True       # UPDATED, newest first
    saved_query = ""
    sel = top = 0
    msg = ""
    while True:
        base = mode_rows(sessions, MODES[mode])
        rows = sort_rows(filter_rows(base, query, project), sort_col, sort_desc)
        h, w = stdscr.getmaxyx()
        detail_h = 5
        list_h = max(1, h - 3 - detail_h)
        sel = max(0, min(sel, len(rows) - 1))
        if sel < top:
            top = sel
        elif sel >= top + list_h:
            top = sel - list_h + 1

        # columns: project | session | description | created | updated
        tw = 10
        rest = max(0, w - 2 * tw - 4 - 1)
        cw_proj, cw_title = min(24, rest * 18 // 100), rest * 28 // 100
        cols = [cw_proj, cw_title, rest - cw_proj - cw_title, tw, tw]

        def line(cells):
            return " ".join(fit(c, n) for c, n in zip(cells, cols))

        stdscr.erase()
        filters = []
        if searching or query:
            filters.append(f"/{query}" + ("_" if searching else ""))
        if project:
            filters.append(f"{project_emoji(project)} {project}")
        head = "  ".join(filters) or "ccs"
        info = f"{len(rows)}/{len(base)} {MODES[mode]}"
        gap = w - 1 - disp_width(head) - len(info)
        put(stdscr, 0, 0, head + " " * gap + info if gap >= 2 else head,
            curses.A_BOLD | (curses.color_pair(3) if filters else 0))
        x = 0
        for i, (name, n) in enumerate(zip(COLUMNS, cols)):
            if i == sort_col:
                name += " ▼" if sort_desc else " ▲"
            attr = curses.color_pair(2) | curses.A_BOLD
            if sorting and i == sort_col:
                attr = curses.color_pair(1) | curses.A_BOLD
            put(stdscr, 1, x, fit(name, n), attr)
            x += n + 1
        for i, r in enumerate(rows[top:top + list_h]):
            live = "● " if r["agent"] and is_alive(r["agent"]) else ""
            cells = [f"{project_emoji(r['project'])} {r['project']}", live + (r["title"] or r["id"][:8]),
                     r["first"], rel_time(r["created"]), rel_time(r["updated"])]
            attr = curses.color_pair(1) if top + i == sel else curses.A_NORMAL
            put(stdscr, 2 + i, 0, line(cells), attr)

        y = h - detail_h - 1
        stdscr.hline(y, 0, curses.ACS_HLINE, w)
        if rows:
            r = rows[sel]
            info_line = f"{r['id']}  ·  {short_path(r['cwd'])}  ·  {r['prompts']} prompts"
            if not os.path.isdir(r["cwd"]):
                info_line += "  ·  ⚠ folder missing"
            put(stdscr, y + 1, 0, info_line, curses.A_BOLD)
            body = textwrap.wrap(f"First: {r['first']}", w - 1)[:2] + \
                textwrap.wrap(f"Last:  {r['last']}", w - 1)[:2]
            for j, txt in enumerate(body[:detail_h - 1]):
                put(stdscr, y + 2 + j, 0, txt, curses.A_DIM)
        put(stdscr, h - 1, 0, msg or (HELP_SORT if sorting else HELP_SEARCH if searching else HELP),
            curses.color_pair(3) if msg else curses.A_DIM)
        stdscr.refresh()

        msg = ""
        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        if sorting:
            if k == curses.KEY_LEFT:
                sort_col = (sort_col - 1) % len(COLUMNS)
            elif k == curses.KEY_RIGHT:
                sort_col = (sort_col + 1) % len(COLUMNS)
            elif k == curses.KEY_UP:
                sort_desc = False
            elif k == curses.KEY_DOWN:
                sort_desc = True
            elif k in ("\n", "\r", curses.KEY_ENTER, "\x1b", "s"):
                sorting = False
            sel = 0
            continue
        # navigation shared by both modes
        if k in (curses.KEY_UP, "\x10"):          # ↑ / Ctrl-P
            sel -= 1
        elif k in (curses.KEY_DOWN, "\x0e"):      # ↓ / Ctrl-N
            sel += 1
        elif k == curses.KEY_PPAGE:
            sel -= list_h
        elif k == curses.KEY_NPAGE:
            sel += list_h
        elif k == curses.KEY_HOME:
            sel = 0
        elif k == curses.KEY_END:
            sel = len(rows) - 1
        elif k == curses.KEY_RESIZE:
            pass
        elif searching:
            if k in ("\n", "\r", curses.KEY_ENTER):
                searching = False
            elif k == "\x1b":
                searching, query, sel = False, saved_query, 0
            elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                query, sel = query[:-1], 0
            elif k == "\x15":                     # Ctrl-U
                query, sel = "", 0
            elif isinstance(k, str) and k.isprintable():
                query, sel = query + k, 0
        elif k == "k":
            sel -= 1
        elif k == "j":
            sel += 1
        elif k == "g":
            sel = 0
        elif k == "G":
            sel = len(rows) - 1
        elif k == "/":
            searching, saved_query = True, query
        elif k == "s":
            sorting, sel = True, 0
        elif k == "p":
            choice = pick_project(stdscr, base, project)
            if choice is not False:
                project, sel = choice, 0
        elif k == "x":
            query, project, sel = "", None, 0
        elif k == "\t":
            mode, sel = (mode + 1) % len(MODES), 0
        elif k in ("q", "\x1b"):
            return None
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if not rows:
                continue
            r = rows[sel]
            if not os.path.isdir(r["cwd"]):
                msg = f"Folder {r['cwd']} no longer exists"
                continue
            return r


def main(argv):
    if argv and argv[0] in ("-V", "--version"):
        print(f"ccs {__version__}")
        return 0
    if argv and argv[0] in ("-h", "--help"):
        print("usage: ccs [.|path] [text...]\n"
              "  .|path         start filtered by that folder's project\n"
              "  text           start with that search filter\n"
              "  -V, --version  show version\n\n"
              f"config: {CONFIG_FILE}\n\n" + HELP)
        return 0
    locale.setlocale(locale.LC_ALL, "")
    apply_config(load_config())
    query, project = initial_filters(argv, os.getcwd())

    def progress(i, n):
        sys.stderr.write(f"\rindexing sessions… {i}/{n}")
        sys.stderr.flush()

    sessions = load_sessions(progress=progress)
    sys.stderr.write("\r\033[K")
    for s in sessions:
        s["project"] = project_key(s["cwd"])
    agents = load_agents()
    for s in sessions:
        s["agent"] = agents.get(s["id"])
    if not sessions:
        print("No sessions found in", PROJECTS_DIR)
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    chosen = curses.wrapper(run_ui, sessions, query, project)
    if not chosen:
        return 0
    cmd = ["claude", "--resume", chosen["id"]]
    if chosen["agent"] and is_alive(chosen["agent"]):
        cmd = ["claude", "attach", chosen["id"]]
    print(f"→ {short_path(chosen['cwd'])}  ·  {' '.join(cmd)}")
    os.chdir(chosen["cwd"])
    os.execvp("claude", cmd)


def cli():
    """Package entry point (`ccs` installed with pipx/uv)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
