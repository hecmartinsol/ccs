#!/usr/bin/env python3
"""ccs — minimal TUI to browse and resume Claude Code sessions.

Reads ~/.claude/projects/*/*.jsonl, caches the index by mtime/size and on
Enter does chdir to the session's folder + `claude --resume <id>`.
No dependencies: stdlib only (curses).
"""
import curses
import fnmatch
import glob
import json
import locale
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
import unicodedata
import zlib
from datetime import datetime, timezone

__version__ = "0.1.0"

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CACHE_FILE = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "ccs", "index.json"
)
SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
TRASH_DIR = os.path.join(
    os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "ccs", "trash"
)
CACHE_VERSION = 4
REFRESH_SECS = 30
MAX_TEXT = 300

NOISE_PREFIXES = ("<system-reminder>", "<local-command", "<bash-", "Caveat:", "<task-notification>",
                  "[Request interrupted")
IGNORED_COMMANDS = {"/clear", "/compact", "/exit", "/resume"}
CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


# ---------------------------------------------------------------- parsing

def _user_text(rec, full=False):
    """Visible text of a real user message, or None if it is noise.
    By default one collapsed, truncated line; `full` keeps the text as written."""
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
    return text if full else " ".join(text.split())[:MAX_TEXT]


def _assistant_text(rec):
    """Text blocks of an assistant message (tool calls and thinking are left out), or None."""
    if rec.get("isSidechain"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    text = "\n".join(b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text").strip()
    return text or None


def read_preview(path, limit=100, max_chars=1500):
    """Last `limit` visible messages of a session as [(role, text)], role = "user" | "claude"."""
    msgs = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            t = rec.get("type")
            text = _user_text(rec, full=True) if t == "user" else _assistant_text(rec) if t == "assistant" else None
            if text:
                if len(text) > max_chars:
                    text = text[:max_chars] + "…"
                msgs.append(("user" if t == "user" else "claude", text))
    return msgs[-limit:]


def parse_session(path):
    """Extract metadata from a session .jsonl. None if it has no real prompts."""
    s = {"id": os.path.basename(path)[:-len(".jsonl")], "title": "", "first": "", "last": "",
         "cwd": "", "created": "", "updated": "", "entrypoint": "", "prompts": 0,
         "branch": "", "model": "", "usage": {}}
    ai_title = custom_title = ""
    by_msg = {}                         # message id -> (model, tokens): a message spans several records
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
            if rec.get("gitBranch"):
                s["branch"] = rec["gitBranch"]
            if t == "assistant":
                msg = rec.get("message") or {}
                u, model = msg.get("usage"), msg.get("model")
                if isinstance(u, dict) and model and model != "<synthetic>":
                    cw = u.get("cache_creation_input_tokens") or 0
                    cw1 = min(cw, (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
                    by_msg[msg.get("id") or rec.get("uuid")] = (model, [
                        u.get("input_tokens") or 0, u.get("output_tokens") or 0,
                        u.get("cache_read_input_tokens") or 0, cw - cw1, cw1])
                    s["model"] = model
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
    for model, toks in by_msg.values():
        tot = s["usage"].setdefault(model, [0] * 5)
        for i, n in enumerate(toks):
            tot[i] += n
    return s


def rename_session(path, session_id, title):
    """Append a custom-title record, the same one `/rename` writes."""
    rec = json.dumps({"type": "custom-title", "customTitle": title, "sessionId": session_id},
                     ensure_ascii=False)
    with open(path, "rb+") as f:
        f.seek(0, os.SEEK_END)
        lead = b""
        if f.tell():
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                lead = b"\n"
        f.write(lead + rec.encode("utf-8") + b"\n")


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
    sessions = [dict(e["d"], path=p) for p, e in new.items() if e["d"]]
    sessions.sort(key=lambda s: s["updated"], reverse=True)
    return sessions


def filter_rows(rows, query, project=None):
    terms = query.lower().split()
    out = []
    for r in rows:
        if project and r.get("project") != project:
            continue
        hay = " ".join((r["title"], r["first"], r["last"], r["cwd"], r["id"],
                        r.get("branch", ""), r.get("model", ""))).lower()
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


# ---------------------------------------------------------------- usage and columns

# $ per million tokens: (input, output, cache read). Cache writes are 1.25x input (5 min) or
# 2x input (1 h). Estimates from Anthropic's public API prices; override with "prices" in the config.
PRICES = {
    "claude-fable-5-1": (10, 50, 0.25), "claude-fable-5": (10, 50, 0.25),
    "claude-mythos-5-1": (10, 50, 0.25), "claude-mythos-5": (10, 50, 0.25),
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.50),
    "claude-opus-4-8": (5, 25, 0.50), "claude-opus-4-7": (5, 25, 0.50),
    "claude-opus-4-6": (5, 25, 0.50),
    "claude-sonnet-5-5": (2, 10, 0.20), "claude-sonnet-5": (2, 10, 0.20),
    "claude-sonnet-4-6": (3, 15, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.10),
}
USER_PRICES = {}


def price_for(model):
    """(input, output, cache read) $/MTok for a model id, or None if unknown."""
    base = re.sub(r"(-\d{8}|@\d{8}|\[1m\])$", "", model)
    for table in (USER_PRICES, PRICES):
        if base in table:
            return table[base]
    return None


def session_cost(s):
    """Estimated $ of a session, or None when none of its models has a known price."""
    total, known = 0.0, False
    for model, (inp, out, cr, cw5, cw1) in s.get("usage", {}).items():
        p = price_for(model)
        if not p:
            continue
        known = True
        total += (inp * p[0] + out * p[1] + cr * p[2] + cw5 * p[0] * 1.25 + cw1 * p[0] * 2) / 1e6
    return total if known else None


def session_tokens(s):
    return sum(sum(v) for v in s.get("usage", {}).values())


def session_secs(s):
    try:
        return int((parse_ts(s["updated"]) - parse_ts(s["created"])).total_seconds())
    except (KeyError, ValueError):
        return 0


def fmt_tokens(n):
    if n < 1000:
        return str(n)
    for div, unit in ((1e3, "k"), (1e6, "M"), (1e9, "G")):
        if n < div * 1000 or unit == "G":
            v = n / div
            return f"{v:.1f}{unit}" if v < 100 else f"{v:.0f}{unit}"


def fmt_cost(c):
    if c is None:
        return "—"
    if c < 0.005:
        return "<$0.01"
    return f"${c:.2f}" if c < 10 else f"${c:.1f}" if c < 100 else f"${c:.0f}"


def fmt_duration(secs):
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{secs % 3600 // 60:02d}m"
    return f"{secs // 86400}d{secs % 86400 // 3600}h"


def model_short(model):
    return re.sub(r"^claude-", "", model)


# key, title, width (fixed columns) or weight (flexible ones, width=None), cell text, sort key, default
COLS = [
    ("project", "PROJECT", None, lambda r: f"{project_emoji(r['project'])} {r['project']}",
     lambda r: r["project"].lower(), True),
    ("session", "SESSION", None, lambda r: r["title"] or r["id"][:8],
     lambda r: (r["title"] or r["id"]).lower(), True),
    ("branch", "BRANCH", 16, lambda r: r["branch"], lambda r: r["branch"].lower(), False),
    ("model", "MODEL", 12, lambda r: model_short(r["model"]), lambda r: r["model"], False),
    ("first", "DESCRIPTION", None, lambda r: r["first"], lambda r: r["first"].lower(), True),
    ("turns", "TURNS", 5, lambda r: str(r["prompts"]), lambda r: r["prompts"], False),
    ("tokens", "TOKENS", 7, lambda r: fmt_tokens(session_tokens(r)), session_tokens, False),
    ("cost", "COST", 7, lambda r: fmt_cost(session_cost(r)), lambda r: session_cost(r) or 0, False),
    ("duration", "DURATION", 8, lambda r: fmt_duration(session_secs(r)), session_secs, False),
    ("created", "CREATED", 10, lambda r: rel_time(r["created"]), lambda r: r["created"], True),
    ("updated", "UPDATED", 10, lambda r: rel_time(r["updated"]), lambda r: r["updated"], True),
]
COL = {c[0]: c for c in COLS}
FLEX_WEIGHTS = {"project": 18, "session": 28, "first": 54}    # shares of the free width
DEFAULT_COLUMNS = [c[0] for c in COLS if c[5]]
ORDER = [c[0] for c in COLS]            # every column, in display order (shown or not)
VISIBLE = list(DEFAULT_COLUMNS)         # the shown ones, in display order


def set_columns(order, shown):
    """Set the column order and which of them are shown (at least one stays)."""
    ORDER[:] = order
    VISIBLE[:] = [k for k in order if k in shown] or list(DEFAULT_COLUMNS)


def move_column(order, i, delta):
    """Move order[i] by `delta` places. Returns (new order, its new index)."""
    j = max(0, min(len(order) - 1, i + delta))
    out = list(order)
    out.insert(j, out.pop(i))
    return out, j


def column_widths(keys, total):
    """Width of each visible column so the row fits in `total` cells (flexible ones share the rest)."""
    fixed = sum(COL[k][2] for k in keys if COL[k][2])
    free = max(0, total - fixed - (len(keys) - 1))
    flex = [k for k in keys if not COL[k][2]]
    weight = sum(FLEX_WEIGHTS[k] for k in flex)
    width = {k: free * FLEX_WEIGHTS[k] // weight for k in flex} if weight else {}
    if "project" in width and width["project"] > 24:      # project names are short: give the rest away
        extra, width["project"] = width["project"] - 24, 24
        others = [k for k in flex if k != "project"]
        for i, k in enumerate(others):
            width[k] += extra // len(others) + (extra % len(others) if i == 0 else 0)
    if flex:
        width[flex[-1]] += free - sum(width.values())       # rounding leftovers
    return [COL[k][2] or width[k] for k in keys]


def sort_rows(rows, key, desc):
    return sorted(rows, key=COL[key][4], reverse=desc)


def pin_favorites(rows):
    """Favorites first, keeping the current order inside each group."""
    return sorted(rows, key=lambda r: r["id"] not in FAVORITES)


def is_hidden(project):
    name = project.lower()
    return any(fnmatch.fnmatchcase(name, p) for p in HIDDEN)


def toggle_favorite(session_id):
    """Flip a session in FAVORITES and persist it. Returns True if it is now a favorite."""
    favs = [i for i in FAVORITES if i != session_id]
    now = len(favs) == len(FAVORITES)
    if now:
        favs.append(session_id)
    save_config({"favorites": favs})
    FAVORITES[:] = favs
    return now


def live_session_ids(sessions_dir=SESSIONS_DIR):
    """Ids of sessions with a running Claude Code process."""
    ids = set()
    for path in glob.glob(os.path.join(glob.escape(sessions_dir), "*.json")):
        try:
            with open(path) as f:
                info = json.load(f)
            if is_alive(info):
                ids.add(info["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return ids


def running(session, live_ids):
    """True if the session has a live Claude Code process (interactive or background agent)."""
    agent = session.get("agent")
    return session["id"] in live_ids or bool(agent and is_alive(agent))


TERMINALS = {  # emulator -> arguments that make it run the rest of the command line
    "ghostty": ["-e"], "kitty": [], "alacritty": ["-e"], "wezterm": ["start", "--"],
    "gnome-terminal": ["--"], "konsole": ["-e"], "xfce4-terminal": ["-x"], "xterm": ["-e"],
    "x-terminal-emulator": ["-e"],
}


def session_command(session):
    """argv that opens a session: attach if it is a running agent, else resume."""
    agent = session.get("agent")
    verb = ["attach"] if agent and is_alive(agent) else ["--resume"]
    return ["claude", *verb, session["id"]]


def terminal_prefix(env=None):
    """Command prefix of a terminal emulator: config, then the one we run in, then any installed."""
    env = os.environ if env is None else env
    if TERMINAL:
        return list(TERMINAL)
    names = [env.get("TERM_PROGRAM", "").lower(), os.path.basename(env.get("TERMINAL", ""))]
    for name in names + list(TERMINALS):
        if name in TERMINALS and shutil.which(name):
            return [name, *TERMINALS[name]]
    return None


def spawn(argv, cwd, env=None):
    """Open `argv` in `cwd` in a new terminal window, without leaving ccs. Returns a status message."""
    prefix = terminal_prefix(env)
    if not prefix:
        return "No terminal found: set \"terminal\" in the config (e.g. \"ghostty -e\")"
    # cd first: some emulators (gnome-terminal) ignore the working directory of the caller
    full = [*prefix, "sh", "-c", 'cd "$0" && exec "$@"', cwd, *argv]
    try:
        subprocess.Popen(full, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return f"Could not start {full[0]}: {e.strerror or e}"
    return f"Opened in {os.path.basename(full[0])}"


def trash_session(path, trash_dir=TRASH_DIR):
    """Move a session transcript (and its <id>/ side folder, if any) into the trash.
    Returns the new path of the transcript."""
    dest_dir = os.path.join(trash_dir, os.path.basename(os.path.dirname(path)))
    os.makedirs(dest_dir, exist_ok=True)
    side = path[:-len(".jsonl")]
    if os.path.isdir(side):
        shutil.move(side, os.path.join(dest_dir, os.path.basename(side)))
    dest = os.path.join(dest_dir, os.path.basename(path))
    shutil.move(path, dest)
    return dest


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
HIDDEN = []
FAVORITES = []
TERMINAL = []                           # user's terminal command prefix, e.g. ["ghostty", "-e"]
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


def save_config(updates, path=None):
    """Merge `updates` into the user config file (other keys are kept)."""
    path = path or CONFIG_FILE
    cfg = load_config(path)
    cfg.update(updates)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def apply_config(cfg):
    """workspaces: folders whose top-level children are projects.
    emojis: {project: emoji}.  rules: [[substring, emoji], ...] (before the built-in ones).
    hidden: project-name patterns (fnmatch) to leave out.  favorites: session ids pinned on top.
    terminal: command that runs the rest of its arguments, for Ctrl-J (e.g. "ghostty -e").
    columns: shown column keys.  column_order: all column keys in display order.  prices: {model: [input, output, cache read]} in $/MTok."""
    global WORKSPACES, EMOJIS, RULES, HIDDEN, FAVORITES, TERMINAL, USER_PRICES
    TERMINAL = shlex.split(cfg.get("terminal") or "")
    shown = cfg.get("columns", DEFAULT_COLUMNS)
    lead = cfg.get("column_order") or (cfg["columns"] if "columns" in cfg else [])   # no config: natural order
    seen = [k for k in lead + shown if k in COL] if lead else []
    set_columns(list(dict.fromkeys(seen + [c[0] for c in COLS])), set(shown))
    USER_PRICES = {m: tuple(p) for m, p in cfg.get("prices", {}).items()
                   if isinstance(p, (list, tuple)) and len(p) == 3}
    HIDDEN = [str(p).lower() for p in cfg.get("hidden", [])]
    FAVORITES = [str(i) for i in cfg.get("favorites", [])]
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


HELP = [("↑↓", "move"), ("Enter", "resume"), ("Space", "preview"), ("^J", "window"), ("/", "search"),
        ("s", "sort"), ("c", "columns"), ("p", "project"), ("r", "rename"), ("d", "trash"), ("*", "pin"),
        ("^H", "hidden"), ("^R", "refresh"), ("x", "clear"), ("Tab", "view"), ("q", "quit")]
HELP_SORT = [("←→", "column"), ("↑", "asc"), ("↓", "desc"), ("Enter/Esc/s", "done")]
HELP_SEARCH = [("type", "to filter"), ("↑↓", "move"), ("Enter", "accept"), ("Esc", "cancel")]


def help_text(pairs):
    return " · ".join(f"{k} {d}" for k, d in pairs)


def put(win, y, x, text, attr=0):
    """addstr clipped to the window width, without crashing on the last cell."""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            win.addstr(y, x, fit(text, w - 1 - x), attr)
        except curses.error:
            pass


def prompt_text(stdscr, label, initial=""):
    """One-line input on the bottom row. Returns the text, or None if cancelled."""
    text = initial
    while True:
        h, w = stdscr.getmaxyx()
        stdscr.move(h - 1, 0)
        stdscr.clrtoeol()
        put(stdscr, h - 1, 0, f"{label}: {text}_  (Enter save · Esc cancel)",
            curses.color_pair(3) | curses.A_BOLD)
        stdscr.refresh()
        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        if k in ("\n", "\r", curses.KEY_ENTER):
            return text.strip()
        elif k == "\x1b":
            return None
        elif k in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            text = text[:-1]
        elif k == "\x15":                         # Ctrl-U
            text = ""
        elif isinstance(k, str) and k.isprintable():
            text += k


def confirm(stdscr, text):
    """Yes/no question on the bottom row; only `y` means yes."""
    h, w = stdscr.getmaxyx()
    stdscr.move(h - 1, 0)
    stdscr.clrtoeol()
    put(stdscr, h - 1, 0, f"{text} (y/N)", curses.color_pair(3) | curses.A_BOLD)
    stdscr.refresh()
    while True:
        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        if k != curses.KEY_RESIZE:
            return k in ("y", "Y")


def put_help(win, y, pairs):
    """Key hints on one row: keys in the header colour, descriptions dimmed."""
    x = 0
    for i, (k, d) in enumerate(pairs):
        for text, attr in ((" · " if i else "", curses.A_DIM),
                           (k, curses.color_pair(2) | curses.A_BOLD),
                           (" " + d, curses.A_DIM)):
            put(win, y, x, text, attr)
            x += disp_width(text)


def preview_lines(msgs, width):
    """Wrapped lines of a conversation: [(text, kind)].
    kind: 1 user header, 2 claude header, 3 user text, 0 claude text."""
    out = []
    for role, text in msgs:
        user = role == "user"
        out.append(("▌ You" if user else "▌ Claude", 1 if user else 2))
        for para in text.splitlines() or [""]:
            out.extend((ln, 3 if user else 0) for ln in
                       (textwrap.wrap(para, max(10, width), replace_whitespace=False) or [""]))
        out.append(("", 0))
    return out


def exchange_starts(lines):
    """Index of the first line of each question/answer exchange."""
    starts = [i for i, (_, k) in enumerate(lines) if k == 1]
    if lines and (not starts or starts[0] != 0):
        starts.insert(0, 0)
    return starts


def preview(stdscr, session):
    """Full-screen reader of a session, opened at the end. ↑↓ jump between question/answer
    exchanges, PgUp/PgDn scroll. Space/Esc/q closes."""
    try:
        msgs = read_preview(session["path"])
    except OSError as e:
        return f"Cannot read session: {e.strerror or e}"
    h, w = stdscr.getmaxyx()
    win = curses.newwin(h, w, 0, 0)
    win.keypad(True)
    lines = preview_lines(msgs, w - 6)
    starts = exchange_starts(lines)
    vis = max(1, h - 2)
    max_top = max(0, len(lines) - vis)
    cur = len(starts) - 1                               # current exchange
    top = max_top                                       # start at the very bottom
    title = f" {session['title'] or session['id'][:8]}  ·  {short_path(session['cwd'])} "
    keys = [("↑↓", "question"), ("PgUp/PgDn", "scroll"), ("g/G", "first/last"), ("Space/Esc", "close")]
    while True:
        win.erase()
        put(win, 0, 0, fit(title, w - 1), curses.color_pair(1) | curses.A_BOLD)
        cur_start = starts[cur] if starts else 0
        for i, (text, kind) in enumerate(lines[top:top + vis]):
            if kind == 1:
                attr = curses.color_pair(1) | curses.A_BOLD if top + i == cur_start \
                    else curses.color_pair(2) | curses.A_BOLD
            elif kind == 2:
                attr = curses.color_pair(3) | curses.A_BOLD
            else:
                attr = curses.A_BOLD if kind == 3 else curses.A_NORMAL
            put(win, 1 + i, 1 if kind in (1, 2) else 3, text, attr)
        if not msgs:
            put(win, 2, 3, "(no messages)", curses.A_DIM)
        put_help(win, h - 1, keys)
        pos = f"{cur + 1}/{len(starts)} "
        put(win, h - 1, max(0, w - 1 - len(pos)), pos, curses.color_pair(2) | curses.A_BOLD)
        win.refresh()
        k = win.get_wch()
        if k in (curses.KEY_UP, "k", "\x10"):
            if top > cur_start and cur_start <= max_top:
                top = cur_start                         # first back to this question
            elif cur > 0:
                cur -= 1
                top = starts[cur]
        elif k in (curses.KEY_DOWN, "j", "\x0e"):
            if cur < len(starts) - 1:
                cur += 1
                top = starts[cur]
            else:
                top = max_top
        elif k in (curses.KEY_PPAGE, "b"):
            top -= vis
        elif k in (curses.KEY_NPAGE, "f"):
            top += vis
        elif k in (curses.KEY_HOME, "g"):
            cur, top = 0, 0
        elif k in (curses.KEY_END, "G"):
            cur, top = len(starts) - 1, max_top
        elif k in (" ", "q", "\x1b"):
            return ""
        top = max(0, min(top, max_top))


def columns_menu(stdscr):
    """Popup to show/hide and reorder table columns; saved in the config. Returns a status message.
    ↑↓ select · Space show/hide · m grab, ↑↓ move, Enter drop (Esc undoes) · Esc close."""
    h, w = stdscr.getmaxyx()
    ph, pw = min(h - 2, len(COLS) + 3), min(w - 4, 50)
    win = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
    win.keypad(True)
    order, before = list(ORDER), None            # `before` = the order to restore while grabbing
    sel, top = 0, 0

    def save(shown):
        set_columns(order, shown)
        try:
            save_config({"columns": VISIBLE, "column_order": ORDER})
        except (OSError, SystemExit) as e:
            return f"Config not saved: {e}"
        return ""

    while True:
        vis = ph - 3
        top = min(max(top, sel - vis + 1), sel)
        win.erase()
        win.box()
        put(win, 0, 2, " Columns ", curses.A_BOLD)
        for i, key in enumerate(order[top:top + vis]):
            grabbed = before is not None and top + i == sel
            attr = curses.color_pair(3) | curses.A_BOLD | curses.A_REVERSE if grabbed \
                else curses.color_pair(1) if top + i == sel else curses.A_NORMAL
            put(win, 1 + i, 1, fit(f" {'≡' if grabbed else ' '}[{'x' if key in VISIBLE else ' '}] {COL[key][1]}", pw - 2), attr)
        put(win, ph - 1, 2, " ↑↓ move · Enter drop · Esc undo " if before is not None
            else " Space show · m move · Esc close ", curses.A_DIM)
        win.refresh()
        k = win.get_wch()
        if k in (curses.KEY_UP, "\x10") or (k == "k" and before is None):
            if before is None:
                sel = max(0, sel - 1)
            else:
                order, sel = move_column(order, sel, -1)
        elif k in (curses.KEY_DOWN, "\x0e") or (k == "j" and before is None):
            if before is None:
                sel = min(len(order) - 1, sel + 1)
            else:
                order, sel = move_column(order, sel, 1)
        elif before is not None:
            if k in ("\n", "\r", curses.KEY_ENTER):
                before = None
                msg = save(set(VISIBLE))
                if msg:
                    return msg
            elif k == "\x1b":
                order, before = before, None
        elif k == "m":
            before = list(order)
        elif k == " ":
            shown = set(VISIBLE) ^ {order[sel]}
            if shown:                               # keep at least one column
                msg = save(shown)
                if msg:
                    return msg
        elif k in ("\x1b", "q", "c"):
            return ""


def pick_project(stdscr, rows, current):
    """Popup with the distinct projects, filtered as you type.

    Returns the chosen one, None = all, False = cancelled.
    """
    all_items = [(None, len(rows))] + project_list(rows)
    spent = {}
    for r in rows:
        spent[r["project"]] = spent.get(r["project"], 0) + (session_cost(r) or 0)
    spent[None] = sum(spent.values())
    h, w = stdscr.getmaxyx()

    def label(p):
        return "   (all)" if p is None else f"{project_emoji(p)} {p}"

    pw = min(w - 4, max(38, max(disp_width(label(p)) for p, _ in all_items) + 18))
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
            put(win, 2 + i, 1, fit(f" {label(p)}", pw - 16) + f"{n:>5} {fmt_cost(spent[p]):>7} ", attr)
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


def build_sessions(progress=None):
    """Sessions with their project and background-agent info attached."""
    sessions = load_sessions(progress=progress)
    agents = load_agents()
    for s in sessions:
        s["project"] = project_key(s["cwd"])
        s["agent"] = agents.get(s["id"])
    return sessions


def reload_all(progress=None):
    """Re-read the config (if it parses) and the sessions."""
    try:
        apply_config(load_config())
    except SystemExit:
        pass                            # keep the previous config while the file is mid-edit
    return build_sessions(progress)


def run_ui(stdscr, sessions, query, project=None, reload=None):
    """`reload` () -> new session list; when given, the list refreshes every REFRESH_SECS."""
    curses.curs_set(0)
    curses.use_default_colors()
    curses.nonl()                       # Enter -> \r; Ctrl-J (Ctrl-Enter on some terminals) -> \n
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selection
    curses.init_pair(2, curses.COLOR_CYAN, -1)                   # header
    curses.init_pair(3, curses.COLOR_YELLOW, -1)                 # warning / filters
    mode = 0
    searching = False
    sorting = False
    show_hidden = False
    follow = None                       # session id the selection should stick to
    sort_col, sort_desc = "updated", True   # newest first
    saved_query = ""
    sel = top = 0
    msg = ""
    cur_id = None
    if reload:
        stdscr.timeout(1000)            # wake up every second to tick the countdown
    next_refresh = time.monotonic() + REFRESH_SECS
    while True:
        if reload and time.monotonic() >= next_refresh:
            sessions[:] = reload()
            follow = cur_id
            next_refresh = time.monotonic() + REFRESH_SECS
        base = [s for s in mode_rows(sessions, MODES[mode])
                if show_hidden or not is_hidden(s["project"])]
        rows = pin_favorites(sort_rows(filter_rows(base, query, project), sort_col, sort_desc))
        live_ids = live_session_ids()
        if follow:
            sel = next((i for i, r in enumerate(rows) if r["id"] == follow), sel)
            follow = None
        h, w = stdscr.getmaxyx()
        detail_h = 5
        list_h = max(1, h - 3 - detail_h)
        sel = max(0, min(sel, len(rows) - 1))
        cur_id = rows[sel]["id"] if rows else None
        if sel < top:
            top = sel
        elif sel >= top + list_h:
            top = sel - list_h + 1

        keys = list(VISIBLE)
        if sort_col not in keys:
            sort_col = keys[-1]
        cols = column_widths(keys, w - 1)

        stdscr.erase()
        filters = []
        if searching or query:
            filters.append(f"/{query}" + ("_" if searching else ""))
        if project:
            filters.append(f"{project_emoji(project)} {project}")
        if show_hidden and HIDDEN:
            filters.append("+hidden")
        head = "  ".join(filters) or "ccs"
        info = f"{len(rows)}/{len(base)} {MODES[mode]}"
        if reload:
            info += f"  ⟳ {max(0, int(next_refresh - time.monotonic() + 0.99))}s"
        gap = w - 1 - disp_width(head) - len(info)
        put(stdscr, 0, 0, head + " " * gap + info if gap >= 2 else head,
            curses.A_BOLD | (curses.color_pair(3) if filters else 0))
        x = 0
        for key, n in zip(keys, cols):
            name = COL[key][1]
            if key == sort_col:
                name += " ▼" if sort_desc else " ▲"
            attr = curses.color_pair(2) | curses.A_BOLD
            if sorting and key == sort_col:
                attr = curses.color_pair(1) | curses.A_BOLD
            put(stdscr, 1, x, fit(name, n), attr)
            x += n + 1
        for i, r in enumerate(rows[top:top + list_h]):
            mark = ("★ " if r["id"] in FAVORITES else "") + ("● " if running(r, live_ids) else "")
            cells = [(mark if key == "session" else "") + COL[key][3](r) for key in keys]
            attr = curses.color_pair(1) if top + i == sel else curses.A_NORMAL
            put(stdscr, 2 + i, 0, " ".join(fit(c, n) for c, n in zip(cells, cols)), attr)

        y = h - detail_h - 1
        stdscr.hline(y, 0, curses.ACS_HLINE, w)
        if rows:
            r = rows[sel]
            info_line = f"{r['id']}  ·  {short_path(r['cwd'])}  ·  {r['prompts']} prompts"
            if r["branch"]:
                info_line += f"  ·  ⎇ {r['branch']}"
            if r["model"]:
                info_line += (f"  ·  {model_short(r['model'])}  ·  {fmt_tokens(session_tokens(r))} tok"
                              f"  ·  {fmt_cost(session_cost(r))}  ·  {fmt_duration(session_secs(r))}")
            if not os.path.isdir(r["cwd"]):
                info_line += "  ·  ⚠ folder missing"
            put(stdscr, y + 1, 0, info_line, curses.A_BOLD)
            body = textwrap.wrap(f"First: {r['first']}", w - 1)[:2] + \
                textwrap.wrap(f"Last:  {r['last']}", w - 1)[:2]
            for j, txt in enumerate(body[:detail_h - 1]):
                put(stdscr, y + 2 + j, 0, txt, curses.A_DIM)
        if msg:
            put(stdscr, h - 1, 0, msg, curses.color_pair(3))
        else:
            put_help(stdscr, h - 1, HELP_SORT if sorting else HELP_SEARCH if searching else HELP)
        stdscr.refresh()

        msg = ""
        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        if sorting:
            if k == curses.KEY_LEFT:
                sort_col = keys[(keys.index(sort_col) - 1) % len(keys)]
            elif k == curses.KEY_RIGHT:
                sort_col = keys[(keys.index(sort_col) + 1) % len(keys)]
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
        elif k == "c":
            msg = columns_menu(stdscr)
        elif k == " ":
            if rows:
                msg = preview(stdscr, rows[sel])
        elif k == "*":
            if not rows:
                continue
            try:
                follow = rows[sel]["id"]
                now = toggle_favorite(follow)
                msg = "Pinned ★" if now else "Unpinned"
            except (OSError, SystemExit) as e:
                msg = f"Config not saved: {e}"
        elif k == "\x12" and reload:                 # Ctrl-R
            next_refresh = 0
        elif k == "\x08":                         # Ctrl-H
            show_hidden, sel = not show_hidden, 0
        elif k == "d":
            if not rows:
                continue
            r = rows[sel]
            if running(r, live_ids):
                msg = "Session is running, not deleting it"
                continue
            if confirm(stdscr, f"Move “{(r['title'] or r['id'][:8])[:40]}” to trash?"):
                try:
                    dest = trash_session(r["path"])
                    sessions.remove(r)
                    msg = f"Moved to {short_path(dest)}"
                except OSError as e:
                    msg = f"Delete failed: {e.strerror or e}"
        elif k == "r":
            if not rows:
                continue
            r = rows[sel]
            new = prompt_text(stdscr, "Rename", r["title"])
            if new:
                try:
                    rename_session(r["path"], r["id"], new)
                    r["title"] = new
                    msg = f"Renamed to “{new}”"
                except OSError as e:
                    msg = f"Rename failed: {e.strerror or e}"
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
            if running(r, live_ids) and not r["agent"]:
                msg = "Already open in another terminal"
                continue
            if not os.path.isdir(r["cwd"]):
                msg = f"Folder {r['cwd']} no longer exists"
                continue
            if k in ("\r", curses.KEY_ENTER):
                return r
            msg = spawn(session_command(r), r["cwd"])


def main(argv):
    if argv and argv[0] in ("-V", "--version"):
        print(f"ccs {__version__}")
        return 0
    if argv and argv[0] in ("-h", "--help"):
        print("usage: ccs [.|path] [text...]\n"
              "  .|path         start filtered by that folder's project\n"
              "  text           start with that search filter\n"
              "  -V, --version  show version\n\n"
              f"config: {CONFIG_FILE}\n\n" + help_text(HELP))
        return 0
    locale.setlocale(locale.LC_ALL, "")
    apply_config(load_config())
    query, project = initial_filters(argv, os.getcwd())

    def progress(i, n):
        sys.stderr.write(f"\rindexing sessions… {i}/{n}")
        sys.stderr.flush()

    sessions = build_sessions(progress)
    sys.stderr.write("\r\033[K")
    if not sessions:
        print("No sessions found in", PROJECTS_DIR)
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    chosen = curses.wrapper(run_ui, sessions, query, project, reload_all)
    if not chosen:
        return 0
    cmd = session_command(chosen)
    print(f"→ {short_path(chosen['cwd'])}  ·  {' '.join(cmd)}")
    os.chdir(chosen["cwd"])
    os.execvp("claude", cmd)


def cli():
    """Package entry point (`ccs` installed with pipx/uv)."""
    sys.exit(main(sys.argv[1:]))


if __name__ == "__main__":
    cli()
