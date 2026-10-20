# ccs — Claude Code sessions

[Español](README.es.md)

Minimal TUI to browse your [Claude Code](https://claude.com/claude-code) sessions across every project and
resume one with a keystroke. Pure Python stdlib (curses), zero dependencies.

![ccs screenshot](https://raw.githubusercontent.com/hecmartinsol/ccs/master/docs/screenshot.svg)

```
ccs              # open the table
ccs .            # only sessions from the current folder's project
ccs . refund     # same + text filter
ccs ~/code/api   # only sessions from that project
ccs auth bug     # start with that text filter
```

`Enter` does `cd` into the session's folder and runs `claude --resume <id>`.

Columns: project (emoji + name) · session (`/rename` title or AI title) · description (first prompt) ·
created · last update. The bottom panel shows id, folder, and first and last prompt.

## Install

```sh
pipx install claude-ccs          # or: uv tool install claude-ccs
```

From source (latest `master`):

```sh
pipx install git+https://github.com/hecmartinsol/ccs
# or just symlink the single file:
ln -sf "$PWD/ccs.py" ~/.local/bin/ccs
```

## Requirements

- Python ≥ 3.8
- Linux or macOS. **Windows is not supported**: the stdlib has no `curses` there. WSL works.
- `claude` on your `PATH` (only needed to resume)

## Keys

| Key | Action |
|---|---|
| `↑↓` `j k` `PgUp PgDn` `g G` | move |
| `/` | free-text search (Enter accepts, Esc cancels) |
| `s` | sort: ←→ pick column, ↑ asc, ↓ desc, Enter/Esc done |
| `r` | rename the session (writes the same title as `/rename`) |
| `d` | move the session to the trash (asks `y/N`; refuses running sessions). Restore by moving the file back from `~/.local/share/ccs/trash/` |
| `*` | pin/unpin the session (★, stays on top; saved as `favorites` in the config) |
| `H` | show/hide projects matched by `hidden` |
| `p` | project picker: type to filter, ↑↓ + Enter, Esc clears/closes |
| `x` | clear filters |
| `Tab` | cycle local → crons (`sdk-cli`, `claude -p`) → background agents (`claude agents`; `●` = running, Enter attaches) |
| `Enter` | `cd` to the folder + `claude --resume <id>` |
| `q` / `Esc` | quit |

## Projects and emojis

Project = name of the session's folder. Claude Code worktrees (`<repo>/.claude/worktrees/x`) count as
their repo. With `workspaces` in the config, any folder inside a workspace is grouped by its top-level
child (`~/projects/shop/api` → `shop`).

Emoji per project: first `emojis` from the config, then `rules` from the config, then built-in keyword
rules (`voice` → 🎤, `doc` → 📁, `api` → 🔌…); otherwise one from a palette picked by hashing the name
(stable across runs). Use single-codepoint, double-width emojis (🟢, not ⚙️) so the table stays aligned.

## Config (optional)

`~/.config/ccs/config.json` (honours `$XDG_CONFIG_HOME`). See `config.example.json`:

```json
{
  "workspaces": ["~/projects", "~/work"],
  "emojis": { "my-main-repo": "🟢", "side-project": "🪙" },
  "rules": [["shop", "🛒"]],
  "hidden": ["tmp", "scratch*"],
  "favorites": []
}
```

`hidden` takes project-name patterns (`*` and `?` wildcards, case-insensitive). `favorites` is managed
by the `*` key (session ids).

## How it works

ccs reads `~/.claude/projects/*/*.jsonl` and caches the index in `~/.cache/ccs/index.json`; only files
whose mtime/size changed are re-parsed.

> **Note:** the session file format is internal to Claude Code and undocumented, so a Claude Code
> update may break ccs. Please open an issue if it does.

## Development

```sh
python3 -m unittest                                                   # tests
uv build                                                              # sdist + wheel
uv run --no-project --with pyte --with rich python scripts/screenshot.py   # regenerate docs/screenshot.svg
```

Versioning follows [semver](https://semver.org); releases are tagged `vX.Y.Z` and the version lives in
`ccs.__version__` (`ccs --version`).

## License

MIT
