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
  "rules": [["shop", "🛒"]],
  "hidden": ["tmp", "scratch*"],
  "terminal": "ghostty -e",
  "favorites": []
}
```

`hidden` acepta patrones de nombre de proyecto (comodines `*` y `?`, sin distinguir mayúsculas).
`favorites` lo gestiona la tecla `*` (ids de sesión). `terminal` es el prefijo que usa `Ctrl-J` para lanzar `claude` en una ventana
nueva (`"ghostty -e"`, `"gnome-terminal --"`…); sin él, ccs prueba `$TERM_PROGRAM`, `$TERMINAL` y los
emuladores habituales.

Teclas:

| Tecla | Acción |
|---|---|
| `↑↓` `j k` `PgUp PgDn` `g G` | mover |
| `/` | búsqueda de texto libre (Enter acepta, Esc cancela) |
| `s` | ordenar: ←→ elegir columna, ↑ asc, ↓ desc, Enter/Esc terminar |
| `r` | renombrar la sesión (escribe el mismo título que `/rename`) |
| `d` | mover la sesión a la papelera (pide `y/N`; rechaza sesiones en curso). Restaurar: devolver el fichero desde `~/.local/share/ccs/trash/` |
| `*` | fijar/desfijar la sesión (★, queda arriba; se guarda como `favorites` en la config) |
| `H` | mostrar/ocultar los proyectos de `hidden` |
| `p` | popup de proyectos: escribe para filtrar, ↑↓ + Enter, Esc limpia/cierra |
| `R` | refrescar ya (la lista también se refresca sola cada 30 s; la cabecera muestra una cuenta atrás `⟳`) |
| `x` | limpiar filtros |
| `Tab` | rota local → crons (`sdk-cli`, `claude -p`) → agentes en segundo plano (`claude agents`; `●` = activo, Enter hace attach) |
| `Space` | ver la conversación (últimos 100 mensajes; ↑↓ PgUp/PgDn g/G para moverte, Space/Esc cierra) |
| `Ctrl-J` (`Ctrl-Enter` si la terminal lo envía) | abrir la sesión en una ventana de terminal nueva sin cerrar ccs (`terminal` en la config, si no se autodetecta) |
| `Ctrl-T` | abrir la sesión en una ventana nueva de tmux (solo dentro de tmux) |
| `Enter` | `cd` a la carpeta + `claude --resume <id>` (las marcadas con `●` ya están en marcha: se rechaza, salvo agentes, que hacen attach) |
| `q` / `Esc` | salir |

Índice cacheado en `~/.cache/ccs/index.json`; solo reparsea ficheros cuyo mtime/tamaño cambió.

> **Ojo:** el formato de las sesiones es interno de Claude Code y no está documentado; una
> actualización de Claude Code puede romper ccs. Si pasa, abre un issue.

Tests: `python3 -m unittest` · Versión: `ccs --version` (semver, tags `vX.Y.Z`, vive en `ccs.__version__`).

Licencia: MIT.
