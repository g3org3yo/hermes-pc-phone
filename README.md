# PC - Phone

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Hermes desktop plugin](https://img.shields.io/badge/Hermes-desktop%20plugin-6b46c1)](https://github.com/NousResearch/hermes-agent)

A one-click **PC - Phone** button in the Hermes Agent desktop status bar. It turns the
PC ↔ phone link (the *mesh* broker of the Hermes-agent Android companion) on and off
without leaving the app, and it sets that companion up for you the first time.

```
 statusbar →  [• PC - Phone]   ← click = on/off · right-click = new pairing QR
```

Green dot = the link is up. Grey = off. No dialogs, no terminal.

## Why

The Android companion ships as a CLI/binary: you start it, you copy a command, you keep a
terminal open. If all you want is *"is my phone connected to this PC, and can I toggle it?"*,
a status-bar chip is the shortest path. That is all this plugin is.

## Requirements

| Piece | What | Where |
|---|---|---|
| Hermes Agent **desktop** app | the host for this plugin | https://github.com/NousResearch/hermes-agent |
| **PC companion** (the mesh broker) | the PC half of the phone link — **AGPL-3.0**, not mine, not bundled here | [HenWorks/Hermes-agent-android-PC-companion-app](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app) |
| Hermes-agent **Android** app | the phone half | pairs with the companion |
| Python 3.10+ | only if you run the companion from source | the plugin builds its own venv |
| `git` | only for the one-click setup | any recent git |

You **do not** have to install the companion by hand: the plugin's first-run setup clones it
and builds an isolated venv for it (see below). If you already have a clone, the plugin finds
it and uses it.

## Install

```bash
hermes plugins install g3org3yo/hermes-pc-phone
```

Then restart the backend and the UI:

```bash
hermes gateway restart          # the /api/plugins/pc-phone/ routes mount with the backend
```

and reload the desktop UI: `Ctrl+K` → **Reload desktop plugins** (or just restart the app).
The chip appears at the bottom right of the status bar, next to the Dashboard chip.

> If the install finished with *"stays installed but disabled"* (another plugin in your profile
> demands a newer Hermes than you run), enable it yourself once that is resolved:
> `hermes plugins enable pc-phone`.

<details>
<summary>Manual install (no <code>hermes plugins</code>)</summary>

Clone into your Hermes **plugins** folder as `pc-phone`, then reload:

```bash
git clone https://github.com/g3org3yo/hermes-pc-phone "<hermes home>/plugins/pc-phone"
```

Settings → Plugins → **Open plugins folder** shows you the exact path. The desktop half is
copied out of `plugins/pc-phone/desktop/` automatically on load; if the chip does not show up,
`Ctrl+K` → **Reload desktop plugins**.
</details>

## First run

Click the chip, or open **PC - Phone** in the sidebar. If the companion is missing, the page
shows a **One-time setup** card with a `Run setup` button. It:

1. `git clone --depth 1` the companion into `<hermes home>/mesh/companion`
2. creates a private venv at `<hermes home>/mesh/venv` (never touches the agent's venv)
3. `pip install -r handoff/requirements.txt` (PyNaCl, zeroconf, qrcode, pillow, pystray)
4. verifies the imports

Every step streams into the card (and into `<hermes home>/mesh/setup.log`). Nothing else on
your machine is modified, and no service is registered — the link only runs while you leave it on.

## Usage

| Action | What happens |
|---|---|
| **Click the chip** | link ON → starts the broker; link OFF → stops it (process tree included) |
| **Right-click the chip** | restart with a fresh pairing QR (the QR window lasts 5 minutes) |
| **Sidebar → PC - Phone** | status, paired devices, task counters, Start / Stop / Restart + QR / Open console, last log lines |
| **Open console** | the companion's own browser console (random localhost port, re-read on every start) |

Pairing survives restarts — the phone only needs a new QR if you delete `<hermes home>/mesh/id.key`
or pair a different phone.

## How it works

* `desktop/plugin.js` — the statusbar chip and the page (plain ESM, `@hermes/plugin-sdk` only).
* `dashboard/plugin_api.py` — the backend routes `/api/plugins/pc-phone/{status,log,start,stop,restart,pair,setup,setup/install,setup/log}`.
  The renderer cannot spawn processes, so every action goes through here.
* `dashboard/manifest.json` — **required**: the Hermes backend only scans a plugin folder that ships
  `dashboard/manifest.json`, and only mounts `api` (here `plugin_api.py`) as `/api/plugins/pc-phone/`.
  Without it the chip loads but every call 404s (amber dot, "backend not loaded").
* The broker is started exactly as the companion documents it:
  `python -u -m handoff.mesh_broker`, cwd = the companion clone, `HERMES_HOME` inherited.
* Detection is **port-based** (`51379`, or `MESH_PORT`): an instance you started yourself — tray,
  scheduled task, `mesh-start.sh` — is found and can be toggled from the chip too.

## Configuration

All optional, read from the environment of the Hermes backend:

| Variable | Meaning |
|---|---|
| `MESH_REPO` | path to an existing companion clone (skips auto-detection and setup) |
| `MESH_PYTHON` | interpreter to run the broker with (default: the venv the setup builds) |
| `MESH_PORT` | broker port, default `51379` |

Auto-detection order: `MESH_REPO` → `<hermes home>/mesh/companion` → `~/hermes-companion`,
`~/Downloads/hermes-companion`, `C:\Projects\hermes-companion` → a companion installed as an
agent plugin (`<hermes home>/plugins/hermes-companion`).

## Troubleshooting

| Symptom | Fix |
|---|---|
| No chip after install | `Ctrl+K` → **Reload desktop plugins**; the folder is not watched |
| Chip has an amber dot, "backend not loaded" | the page's **Restart backend** button (routes mount with the backend, not the UI) |
| Chip stays grey, "backend not loaded", `/api/plugins/pc-phone/status` returns 404 | `dashboard/manifest.json` is missing (added in 1.1.1) — reinstall/update the plugin; then **Restart backend** |
| "did not open its port within 10 s" | read `broker.log` in the page; usually a stale process holding 51379 |
| Phone cannot pair | right-click the chip for a fresh QR — the window is 5 minutes, and the phone must be on the same LAN |
| `ModuleNotFoundError: zeroconf` | the broker ran with the wrong interpreter — run setup again so the venv is rebuilt |

## Dependency, licensing and credits

This plugin is **MIT** (see [LICENSE](LICENSE)) and contains **no code from the companion**: it
spawns it as a separate process and talks to it over its documented port, so the two are separate
works. The companion itself is **AGPL-3.0, © HenWorks** — if you redistribute *it*, or a modified
version of it, AGPL-3.0 applies to that work. Nothing here re-licenses their code, and the setup
step clones their repository as-is, unmodified.

Huge credit to [HenWorks](https://github.com/HenWorks) for the mesh broker, the pairing model
(NaCl, pair-once, LAN-only, no cloud) and the Android app it serves — this button is only a
remote control for their work. Upstream: [Hermes-agent-android-PC-companion-app](https://github.com/HenWorks/Hermes-agent-android-PC-companion-app).

Hermes Agent itself is MIT, © Nous Research.
