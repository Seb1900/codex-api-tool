# codex-api-tool

A lightweight local proxy + GUI for Codex-compatible API routing.

This tool lets you:

- run a local HTTP proxy for Responses API traffic
- choose one mode from `low / medium / high / xhigh`
- rewrite model suffix automatically:
  - `-low`
  - `-medium`
  - `-high`
  - `-xhigh`
- optionally keep `reasoning.effort` aligned with the selected mode
- view runtime status and recent rewrite events in GUI

## Why this tool

Some providers display or bill by `model` slug, while many Codex clients send:

- `model = gpt-5.3-codex`
- `reasoning.effort = xhigh`

This tool can rewrite to:

- `model = gpt-5.3-codex-xhigh`

so provider-side records are easier to align with your selected mode.

## Requirements

- Windows (tested)
- Python 3.10+ (Tkinter included in standard Python)

No third-party runtime dependency is required.

## Quick Start

```powershell
python rightcode_proxy_gui.py
```

Then in GUI:

1. Select mode (`low/medium/high/xhigh`)
2. Click `Start`
3. Point your Codex/ccswitch provider `base_url` to:
   `http://127.0.0.1:8787/codex/v1`

## Key Fields

- `Listen Host` / `Listen Port`: local proxy address
- `Upstream Base URL`: upstream provider URL (default `https://right.codes/codex/v1`)
- `Base Model`: e.g. `gpt-5.3-codex`
- `Mode`: one of `low/medium/high/xhigh`
- `Rewrite reasoning.effort`: keep effort synced with mode

## Build EXE (single file)

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name rightcode-proxy-gui rightcode_proxy_gui.py
```

Output:

- `dist/rightcode-proxy-gui.exe`

## Files generated at runtime

- `rightcode_proxy_config.json`
- `rightcode_proxy_events.jsonl`

Both are created in the same directory as the script/exe.

## Safety notes

- This tool forwards request headers (including Authorization) to your configured upstream.
- Review upstream URL before running in production environments.

## License

MIT
