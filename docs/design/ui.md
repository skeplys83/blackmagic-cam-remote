# UI design

Dark, quiet, camera-tool look; the live view is the hero. All styles are in `src/web/css/app.css` (no framework).

## Tokens
| Token | Value | Use |
|---|---|---|
| `--bg` | `#0d0f12` | Page background |
| `--panel` | `#171a1f` | Tiles |
| `--line` | `#262a31` | Borders, dividers |
| `--text` / `--dim` | `#e8eaed` / `#8b929c` | Text / labels, secondary text |
| `--accent` | `#3d9bff` | Active buttons, sliders, checkboxes |
| `--rec` | `#e5303a` | Recording, errors |
| `--ok` / `--warn` | `#3ccf7a` / `#f2b33d` | Status |
| `--ctl` | `44px` | Height of every control (touch target) |
| `--ctl-radius` | `10px` | Control corners; tiles use 12px |

Tile headings: 12px, uppercase, `letter-spacing: .08em`, `--dim`.

## Controls (touch friendly, iPad)
- Buttons, selects, text/number inputs: 44px high, 15px/500 text, 10px radius. `.btn.small` is the same size.
- Segmented choices: `.seg` of `.btn`, active one gets `.active` (accent fill).
- Checkboxes: custom 26px box in a 44px row (`label.check`).
- Sliders: always in a `.field` (label + value on top, slider edge to edge below), 28px thumb, 44px hit area.
- Selects use a custom chevron; native appearance is off.
- Overlays on the live view (record button, messages) sit in a rounded dark box: `rgba(13,15,18,.72)`, 1px light
  border, blur, shadow.

## Layout
```
┌──────┬──────────────────────────────────────────┬───────────────┐
│ icon │ Live view tile (fits the window height)   │ Lens & zoom   │
│ ──── │  heading · centered red timecode          │ Focus         │
│phone │  picture (exact aspect, no bars)          │ Rec. format   │
│ info │  toolbar: view, sound, rotation, preview  │               │
├──────┼──────────────┬──────────────┬────────────┬───────────────┤
│      │ Exposure     │ White bal.   │ Audio      │ Phone         │
│      │ Monitoring overlays (2 cols)│ Presets (2 cols)            │
│      │ Recordings (full width)                                   │
└──────┴──────────────────────────────────────────────────────────┘
```
- Sidebar (`aside.sidebar`): app icon (link to GitHub, no hover effect) above the phone info box; as narrow as the
  box's longest word (`min-content`), grows downwards; sticky. Hidden on the setup screen. Under 900px it becomes a
  top bar.
- Live view and key controls are always side by side (right column `clamp(260px, 26vw, 360px)`).
- Second-level settings: fixed CSS grid areas (`.settings`, 4 columns; 2 columns under 1300px).
- Setup screen: centered, max 1100px, three step cards side by side.

## Behaviour rules
- The live-view tile never exceeds the window: `fitLiveTile()` subtracts the tile's top offset and its chrome.
- Recording: tile border/glow red, heading red, timecode pill centered in the heading (above the picture).
- Rotation: Auto follows the phone's screen rotation; 0/90/180/270 override (remembered per browser).
- Only one live view per server; a replaced tab shows "Show it here".
- Labels and statuses are short; full text goes in `title` (e.g. sidebar pills "Wi-Fi", "App open").
