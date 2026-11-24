# GeoPort v5.0.3 source recovery

Recovered on 2026-11-24 from the official Ubuntu release binary
`GeoPort-ubuntu-v5.0.3.zip` (https://github.com/davesc63/GeoPort/releases/tag/v5.0.3).
The macOS DMG was not needed. The public GitHub tree only had the 2024 v4
`src/main.py`; v5.0.3 was published as binaries.

The official repo is GPLv3. This tree is a practical reconstruction of the
corresponding source, not a substitute for the author's unpublished tree.

## Packager and Python

- Packager: **PyInstaller, one-file**. Confirmed by the release notes, bootloader
  strings (`_MEIPASS`, `_pyinstaller_pyz`), and a successful extract with
  `pyinstxtractor-ng`. This is **not** Nuitka.
- Exact PyInstaller *pip* version was not labeled in the bundle (the extractor
  reports a modern 2.1+ CArchive).
- Python: **3.13** (`libpython3.13.so.1.0`, `.pyc` magic `0x0df3` / 3571,
  reported as 3.13.0rc3).
- Entry point: `main.pyc` → package `geoport`.
- Archive was not encrypted.

## How the source was reconstructed

Two decompilers (PyLingual and pycdc-rapid) plus a `pycdas` disassembly of every
app module were cross-checked. Control flow was taken from jump opcodes and
exception tables, not from either decompiler alone. pycdc often inverted
`is None` / `is not None` and `and` / `or`.

The raw `pylingual/`, `pycdc/`, and `disasm/` dumps are **not** in this
repository. They were produced locally from `GeoPort-ubuntu-v5.0.3.zip` during
the repair and were only used as working notes. `templates/` and `static/`
were copied verbatim from the bundle. `requirements.txt` is pinned from the
bundle's `.dist-info` METADATA. `docs/LXC-HEADLESS-GUIDE.md` and
`docs/geoport.service` shipped next to the Ubuntu binary.

## Layout in this repo

```
src/main.py
src/geoport/          # package
src/templates/        # map.html (classic) and map_modern.html
src/static/
requirements.txt
start.fish            # fish + Homebrew: venv, pip, run
```

`geoport.config.BASE_DIR` is the parent of the `geoport` package (`src/` when
running from source, `_MEIPASS` when frozen). That is why templates and static
files sit beside the package.

## Hand-repaired modules

These files were repaired by hand against the disassembly. The others
(`config`, `models`, `gpx`, and the package `__init__` modules) already
decompiled cleanly; decompiler banner comments were removed and their bytecode
still matches.

| Module | What was repaired |
|---|---|
| `src/main.py` | `_run_self_test` try/finally. The success print runs after `runtime.shutdown()`. |
| `src/geoport/runtime.py` | `AsyncRuntime._thread_main` try/finally: cancel tasks, shut down async generators, then `loop.close()`. |
| `src/geoport/core/mounter.py` | `try`/`finally` that closes a lockdown client this function created. Close errors are swallowed. |
| `src/geoport/core/developer_mode.py` | USB then Wi-Fi lockdown for `check_developer_mode` and `enable_developer_mode`, including the passcode reveal path. |
| `src/geoport/core/pair_records.py` | Headless usbmux patch, pair-record search, and pairing export. |
| `src/geoport/core/session.py` | Device/location session: USB and Wi-Fi tunnels, DVT keep-alive, disconnect watcher. |
| `src/geoport/discovery/mdns_advertiser.py` | `detect_lan_ipv4` (UDP route trick, then ifaddr, skipping virtual interfaces). |
| `src/geoport/discovery/scanner.py` | USB, mobdev2, and RemotePairing discovery. |
| `src/geoport/services/fuel.py` | Cache hit under the lock; a failed or non-region payload returns the cached body. |
| `src/geoport/services/updates.py` | Country lookup, map-layer refresh, and the incomplete `get_user_country` marker. |
| `src/geoport/web/app.py` | Both `# WARNING: Decompyle incomplete` markers. The tile proxy's successful OSM fallback continues into the cache write. Several inverted `None` checks and the developer-mode success JSON (`success: True`, `udid: <udid>`) were restored. |

## Behavior that looks surprising but matches the bytecode

- `persist_pair_record_copy` always writes the plist. Its docstring says "if not present"; there is no `exists` check.
- `_finalize_rsd_session_locked` looks up the peer name with the constant `' DeviceName'` (leading space) and then `'DeviceName'`.
- `discover_devices` calls `load_device_meta_cache()` twice and overwrites the first result.
- `connect_device` reports developer mode with the string `'True'`, not boolean `True`.
- A failed remote map-layer fetch still stamps `_map_fetched_at` so the next page load does not immediately retry.

## Compiler differences that are not logic changes

The bundle was built with CPython 3.13.0rc3. Recompiling with CPython 3.13.0
drops an inner `GET_ITER` inside generator expressions and list comprehensions,
and it fuses adjacent `LOAD_FAST` instructions into `LOAD_FAST_LOAD_FAST`.
Jump distances shift by those extra bytes. Opcode sequences otherwise match
for the repaired modules. `developer_mode.py`, `mounter.py`, `config.py`, and
the straight-line helpers match instruction-for-instruction aside from
`__firstlineno__`.

## Remaining uncertainty

Lowest confidence:

1. `geoport/discovery/scanner.py` — the mobdev2 probe loop (`continue` vs `break`) and the RemotePairing `usb_info` fallback. Instruction fusion moved jump distances, so those branches were checked by opcode shape rather than a byte-identical recompile.
2. `geoport/core/session.py` `_connect_wifi_locked` — transient-error retry versus `break` to the next host, and the RemotePairing fallback after the host loop. The recompiled opcodes match; this is still the largest function and the one most sensitive to a misread jump.

Higher confidence: `developer_mode.py`, `mounter.py`, `runtime.py`, `fuel.py`, `updates.py`, `mdns_advertiser.py`, `pair_records.py`, and `web/app.py` (the tile-proxy fallback and the inverted `None` checks were confirmed against the exception tables).

Device tunnel, pairing, and location simulation were not executed. This environment has no iPhone attached.
