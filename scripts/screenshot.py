"""Generate docs/screenshot.svg from fake sessions (no real data).

    uv run --with pyte --with rich python scripts/screenshot.py

Builds a throwaway $HOME with invented sessions, runs ccs in a pty,
feeds the output to a terminal emulator (pyte) and exports it with rich.
"""
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone

import pyte
from rich.console import Console
from rich.style import Style
from rich.text import Text

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLS, ROWS = 132, 22

# (project dir, title, prompts, minutes ago, entrypoint)
SESSIONS = [
    ("projects/shop-api", "Fix flaky checkout test",
     ["the checkout e2e test fails 1 in 10 runs, find out why", "ok, add a retry-free fix and a regression test"], 4, "cli"),
    ("projects/shop-web", "Dark mode for product page",
     ["add dark mode to the product page using the existing tokens", "contrast on the price badge is too low"], 38, "cli"),
    ("projects/voice-notes", "Whisper streaming prototype",
     ["prototype streaming transcription with whisper.cpp", "measure latency on a 5 min recording"], 95, "cli"),
    ("projects/shop-api/.claude/worktrees/refunds", "Partial refunds endpoint",
     ["design POST /orders/{id}/refunds with partial amounts", "write the OpenAPI contract first"], 180, "cli"),
    ("projects/infra", "Terraform state migration",
     ["move the terraform state from local to S3 with locking"], 60 * 5, "cli"),
    ("projects/blog", "Draft: TUIs with curses",
     ["help me outline a post about building TUIs with only the stdlib"], 60 * 26, "cli"),
    ("projects/garmin-sync", "Nightly sleep sync",
     ["sync sleep data to my calendar every night"], 60 * 30, "sdk-cli"),
    ("projects/dotfiles", "zsh startup is slow",
     ["profile my zsh startup, it takes 900ms", "lazy-load nvm"], 60 * 50, "cli"),
    ("projects/shop-web", "Bundle size audit",
     ["why is the main bundle 1.8MB?", "replace moment with date-fns"], 60 * 75, "cli"),
    ("projects/db-migrations", "Add index on orders.created_at",
     ["queries filtering orders by date are slow, propose an index"], 60 * 24 * 4, "cli"),
    ("projects/game-of-life", "",
     ["write game of life in rust with a wasm frontend"], 60 * 24 * 9, "cli"),
    ("", "Organize downloads folder",
     ["sort ~/Downloads into folders by file type"], 60 * 24 * 15, "cli"),
]


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def make_home(home):
    now = datetime.now(timezone.utc)
    for rel, title, prompts, ago, ep in SESSIONS:
        cwd = os.path.join(home, rel) if rel else home
        os.makedirs(cwd, exist_ok=True)
        pdir = os.path.join(home, ".claude", "projects", cwd.replace("/", "-"))
        os.makedirs(pdir, exist_ok=True)
        end = now - timedelta(minutes=ago)
        start = end - timedelta(minutes=20 * len(prompts))
        recs = []
        for i, p in enumerate(prompts):
            ts = start + (end - start) * (i / max(1, len(prompts) - 1)) if len(prompts) > 1 else end
            recs.append({"type": "user", "cwd": cwd, "entrypoint": ep, "timestamp": iso(ts),
                         "message": {"role": "user", "content": p}})
        if title:
            recs.append({"type": "ai-title", "aiTitle": title})
        with open(os.path.join(pdir, f"{uuid.uuid4()}.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in recs)
    os.makedirs(os.path.join(home, ".config", "ccs"), exist_ok=True)
    with open(os.path.join(home, ".config", "ccs", "config.json"), "w") as f:
        json.dump({"workspaces": [os.path.join(home, "projects")]}, f)


def capture(home):
    env = {"HOME": home, "PATH": os.environ["PATH"], "TERM": "xterm-256color",
           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NCURSES_NO_UTF8_ACS": "1", "COLUMNS": str(COLS), "LINES": str(ROWS)}
    master, slave = pty.openpty()
    import fcntl, struct, termios
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "ccs.py")], cwd=home, env=env,
                            stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([master], [], [], 0.05)
            if r:
                try:
                    stream.feed(os.read(master, 65536))
                except OSError:
                    return

    pump(1.5)
    os.write(master, b"j")      # move selection to the second row
    pump(0.5)
    os.write(master, b"q")
    pump(0.5)
    proc.wait(timeout=5)
    return screen


def color(c):
    if c == "default":
        return None
    if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
        return "#" + c
    return {"brown": "yellow"}.get(c, c)


def to_text(screen):
    out = Text()
    for y in range(screen.lines):
        row = screen.buffer[y]
        x = 0
        while x < screen.columns:
            ch = row[x]
            fg, bg = color(ch.fg), color(ch.bg)
            if ch.reverse:
                fg, bg = bg or "black", fg or "white"
            out.append(ch.data or " ", Style(color=fg, bgcolor=bg, bold=ch.bold))
            x += 2 if ch.data and pyte.screens.wcwidth(ch.data) == 2 else 1
        out.append("\n")
    return out


def main():
    with tempfile.TemporaryDirectory() as tmp:
        home = os.path.join(tmp, "home")
        make_home(home)
        screen = capture(home)
    console = Console(record=True, width=COLS, file=open(os.devnull, "w"))
    console.print(to_text(screen), end="")
    path = os.path.join(ROOT, "docs", "screenshot.svg")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    console.save_svg(path, title="ccs")
    print("wrote", path)


if __name__ == "__main__":
    main()
