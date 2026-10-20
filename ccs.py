#!/usr/bin/env python3
"""ccs — TUI mínima para listar y retomar sesiones de Claude Code.

Lee ~/.claude/projects/*/*.jsonl, cachea el índice por mtime/tamaño y al
pulsar Enter hace chdir a la carpeta de la sesión + `claude --resume <id>`.
Sin dependencias: solo stdlib (curses).
"""
import curses
import glob
import json
import locale
import os
import re
import sys
import textwrap
import unicodedata
import zlib
from datetime import datetime, timezone

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
    """Texto visible de un mensaje de usuario real, o None si es ruido."""
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
    """Extrae metadatos de un .jsonl de sesión. None si no tiene prompts reales."""
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
            # una sesión lanzada por SDK y retomada a mano cuenta como interactiva
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
    """Lista de sesiones, reparseando solo ficheros nuevos o modificados."""
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


# ---------------------------------------------------------------- proyectos

CONFIG_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "ccs", "config.json"
)

# Emojis de un solo codepoint y ancho doble (sin variation selector) para que
# la tabla no se desalinee. El config del usuario se antepone a estos.
DEFAULT_EMOJIS = {"~": "🏠"}
DEFAULT_RULES = [  # (subcadena en el nombre, emoji); gana la primera que encaje
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
    """Config JSON opcional del usuario; {} si no existe."""
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise SystemExit(f"ccs: config inválido en {path}: {e}")


def apply_config(cfg):
    """workspaces: carpetas cuyos hijos de primer nivel son proyectos.
    emojis: {proyecto: emoji}.  rules: [[subcadena, emoji], ...] (antes que las de serie)."""
    global WORKSPACES, EMOJIS, RULES
    WORKSPACES = [os.path.expanduser(w).rstrip("/") for w in cfg.get("workspaces", [])]
    EMOJIS = {**DEFAULT_EMOJIS, **cfg.get("emojis", {})}
    RULES = [tuple(r) for r in cfg.get("rules", [])] + DEFAULT_RULES


WORKTREE_MARK = "/.claude/worktrees/"


def project_key(cwd):
    """Proyecto de una carpeta: hijo de primer nivel de un workspace, o su basename.
    Los worktrees de Claude Code (<repo>/.claude/worktrees/x) cuentan como su repo."""
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
    """`ccs . texto` → (texto, proyecto de cwd). Args que son rutas fijan el proyecto."""
    project, words = None, []
    for arg in argv:
        if arg == "." or arg.startswith(("/", "./", "../", "~")):
            path = os.path.expanduser(arg)
            project = project_key(os.path.abspath(os.path.join(cwd, path)))
        else:
            words.append(arg)
    return " ".join(words), project


def project_list(rows):
    """[(proyecto, nº sesiones)] ordenado por la sesión más reciente de cada uno."""
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
        return "ahora"
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
    """Recorta/rellena a `width` columnas de terminal (emoji = 2)."""
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


HELP = "↑↓/jk mover · / buscar · p proyecto · x limpiar filtros · Tab crons · Enter retomar · q salir"
HELP_SEARCH = "escribe para filtrar · ↑↓ mover · Enter aceptar · Esc cancelar"


def put(win, y, x, text, attr=0):
    """addstr recortado al ancho de la ventana, sin petar en la última celda."""
    h, w = win.getmaxyx()
    if 0 <= y < h and x < w - 1:
        try:
            win.addstr(y, x, fit(text, w - 1 - x), attr)
        except curses.error:
            pass


def pick_project(stdscr, rows, current):
    """Popup con los proyectos distintos. Devuelve el elegido, None = todos, False = cancelado."""
    items = [(None, len(rows))] + project_list(rows)
    sel = next((i for i, (p, _) in enumerate(items) if p == current), 0)
    h, w = stdscr.getmaxyx()
    labels = ["   (todos)" if p is None else f"{project_emoji(p)} {p}" for p, _ in items]
    pw = min(w - 4, max(30, max(disp_width(l) for l in labels) + 10))
    ph = min(h - 2, len(items) + 2)
    win = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
    win.keypad(True)
    top = 0
    while True:
        vis = ph - 2
        top = min(max(top, sel - vis + 1), sel)
        win.erase()
        win.box()
        put(win, 0, 2, " Proyecto ", curses.A_BOLD)
        for i, (label, (_, n)) in enumerate(list(zip(labels, items))[top:top + vis]):
            attr = curses.color_pair(1) if top + i == sel else curses.A_NORMAL
            win.addstr(1 + i, 1, fit(f" {label}", pw - 8) + f"{n:>5} ", attr)
        win.refresh()
        k = win.get_wch()
        if k in (curses.KEY_UP, "k"):
            sel = max(0, sel - 1)
        elif k in (curses.KEY_DOWN, "j"):
            sel = min(len(items) - 1, sel + 1)
        elif k == curses.KEY_PPAGE:
            sel = max(0, sel - vis)
        elif k == curses.KEY_NPAGE:
            sel = min(len(items) - 1, sel + vis)
        elif k in ("\n", "\r", curses.KEY_ENTER):
            return items[sel][0]
        elif k in ("\x1b", "q", "p"):
            return False


def run_ui(stdscr, sessions, query, project=None):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selección
    curses.init_pair(2, curses.COLOR_CYAN, -1)                   # cabecera
    curses.init_pair(3, curses.COLOR_YELLOW, -1)                 # aviso / filtros
    show_all = False
    searching = False
    saved_query = ""
    sel = top = 0
    msg = ""
    while True:
        base = sessions if show_all else [s for s in sessions if s["entrypoint"] != "sdk-cli"]
        rows = filter_rows(base, query, project)
        h, w = stdscr.getmaxyx()
        detail_h = 5
        list_h = max(1, h - 3 - detail_h)
        sel = max(0, min(sel, len(rows) - 1))
        if sel < top:
            top = sel
        elif sel >= top + list_h:
            top = sel - list_h + 1

        # columnas: proyecto | sesión | descripción | creado | últ.
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
        info = f"{len(rows)}/{len(base)} sesiones{'  (incl. crons)' if show_all else ''}"
        gap = w - 1 - disp_width(head) - len(info)
        put(stdscr, 0, 0, head + " " * gap + info if gap >= 2 else head,
            curses.A_BOLD | (curses.color_pair(3) if filters else 0))
        put(stdscr, 1, 0, line(["PROYECTO", "SESIÓN", "DESCRIPCIÓN", "CREADO", "ÚLT."]),
            curses.color_pair(2) | curses.A_BOLD)
        for i, r in enumerate(rows[top:top + list_h]):
            cells = [f"{project_emoji(r['project'])} {r['project']}", r["title"] or r["id"][:8],
                     r["first"], rel_time(r["created"]), rel_time(r["updated"])]
            attr = curses.color_pair(1) if top + i == sel else curses.A_NORMAL
            put(stdscr, 2 + i, 0, line(cells), attr)

        y = h - detail_h - 1
        stdscr.hline(y, 0, curses.ACS_HLINE, w)
        if rows:
            r = rows[sel]
            info_line = f"{r['id']}  ·  {short_path(r['cwd'])}  ·  {r['prompts']} prompts"
            if not os.path.isdir(r["cwd"]):
                info_line += "  ·  ⚠ carpeta no existe"
            put(stdscr, y + 1, 0, info_line, curses.A_BOLD)
            body = textwrap.wrap(f"Inicio: {r['first']}", w - 1)[:2] + \
                textwrap.wrap(f"Último: {r['last']}", w - 1)[:2]
            for j, txt in enumerate(body[:detail_h - 1]):
                put(stdscr, y + 2 + j, 0, txt, curses.A_DIM)
        put(stdscr, h - 1, 0, msg or (HELP_SEARCH if searching else HELP),
            curses.color_pair(3) if msg else curses.A_DIM)
        stdscr.refresh()

        msg = ""
        try:
            k = stdscr.get_wch()
        except curses.error:
            continue
        # navegación común a ambos modos
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
        elif k == "p":
            choice = pick_project(stdscr, base, project)
            if choice is not False:
                project, sel = choice, 0
        elif k == "x":
            query, project, sel = "", None, 0
        elif k == "\t":
            show_all, sel = not show_all, 0
        elif k in ("q", "\x1b"):
            return None
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if not rows:
                continue
            r = rows[sel]
            if not os.path.isdir(r["cwd"]):
                msg = f"La carpeta {r['cwd']} ya no existe"
                continue
            return r


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        print("uso: ccs [.|ruta] [texto...]\n"
              "  .|ruta   arranca filtrado por el proyecto de esa carpeta\n"
              "  texto    arranca con ese filtro de búsqueda\n\n"
              f"config: {CONFIG_FILE}\n\n" + HELP)
        return 0
    locale.setlocale(locale.LC_ALL, "")
    apply_config(load_config())
    query, project = initial_filters(argv, os.getcwd())

    def progress(i, n):
        sys.stderr.write(f"\rindexando sesiones… {i}/{n}")
        sys.stderr.flush()

    sessions = load_sessions(progress=progress)
    sys.stderr.write("\r\033[K")
    for s in sessions:
        s["project"] = project_key(s["cwd"])
    if not sessions:
        print("No hay sesiones en", PROJECTS_DIR)
        return 1
    os.environ.setdefault("ESCDELAY", "25")
    chosen = curses.wrapper(run_ui, sessions, query, project)
    if not chosen:
        return 0
    print(f"→ {short_path(chosen['cwd'])}  ·  claude --resume {chosen['id']}")
    os.chdir(chosen["cwd"])
    os.execvp("claude", ["claude", "--resume", chosen["id"]])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
