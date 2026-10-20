# ccs — Claude Code sessions

[English](README.md)

TUI mínima (Python stdlib + curses, sin dependencias) para listar sesiones de Claude Code y retomarlas.

![captura de ccs](https://raw.githubusercontent.com/hecmartinsol/ccs/master/docs/screenshot.svg)

```
ccs              # abre la tabla
ccs .            # solo sesiones del proyecto de la carpeta actual
ccs . pago       # ídem + filtro de texto
ccs ~/code/api   # solo sesiones de ese proyecto
ccs pago wallet  # arranca con ese filtro de texto
```

Columnas: proyecto (emoji + nombre) · sesión (título /rename o título IA) · descripción (primer prompt) ·
creado · última actualización. Panel inferior: id, carpeta, primer y último prompt.

Proyecto = nombre de la carpeta de la sesión. Los worktrees de Claude Code
(`<repo>/.claude/worktrees/x`) cuentan como su repo. Con `workspaces` en el config, cualquier carpeta
dentro de un workspace se agrupa por su hijo de primer nivel (`~/projects/shop/api` → `shop`).

Emoji por proyecto: primero `emojis` del config, luego `rules` del config, luego reglas de serie por
palabra clave (`voice` → 🎤, `doc` → 📁, `api` → 🔌…); si nada encaja, uno de una paleta elegido por hash
del nombre (estable entre ejecuciones). Usa emojis de un solo codepoint y ancho doble (🟢, no ⚙️) para
que la tabla no se desalinee.

## Instalación

```sh
pipx install claude-ccs          # o: uv tool install claude-ccs
```

Desde el código (último `master`):

```sh
pipx install git+https://github.com/hecmartinsol/ccs
# o enlaza el fichero suelto:
ln -sf "$PWD/ccs.py" ~/.local/bin/ccs
```

## Requisitos

- Python ≥ 3.8
- Linux o macOS. **Windows no está soportado**: su stdlib no trae `curses`. En WSL funciona.
- `claude` en el `PATH` (solo para retomar)

## Config (opcional)

`~/.config/ccs/config.json` (respeta `$XDG_CONFIG_HOME`). Ejemplo en `config.example.json`:

```json
{
  "workspaces": ["~/projects", "~/work"],
  "emojis": { "my-main-repo": "🟢", "side-project": "🪙" },
  "rules": [["shop", "🛒"]]
}
```

Teclas:

| Tecla | Acción |
|---|---|
| `↑↓` `j k` `PgUp PgDn` `g G` | mover |
| `/` | búsqueda de texto libre (Enter acepta, Esc cancela) |
| `p` | popup de proyectos (↑↓ + Enter, Esc cierra) |
| `x` | limpiar filtros |
| `Tab` | mostrar/ocultar sesiones `sdk-cli` (crons, `claude -p`) |
| `Enter` | `cd` a la carpeta + `claude --resume <id>` |
| `q` / `Esc` | salir |

Índice cacheado en `~/.cache/ccs/index.json`; solo reparsea ficheros cuyo mtime/tamaño cambió.

> **Ojo:** el formato de las sesiones es interno de Claude Code y no está documentado; una
> actualización de Claude Code puede romper ccs. Si pasa, abre un issue.

Tests: `python3 -m unittest` · Versión: `ccs --version` (semver, tags `vX.Y.Z`, vive en `ccs.__version__`).

Licencia: MIT.
