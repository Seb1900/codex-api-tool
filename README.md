# codex-api-tool

A lightweight local proxy + GUI for Codex-compatible Responses API routing.

## What it does

- Runs a local proxy server.
- Lets you create multiple model rewrite rules in GUI.
- Each rule is exact-match:
  - If `incoming model == source model`, rewrite to `target model`.
  - If no rule matches, request is forwarded unchanged.
- Splits logs into two pages:
  - `Event Log`: startup/shutdown/config/rule/error + request summary
  - `Request Log`: per-request timestamp, source model, final model, matched rule, status

## Example

Rule:

- Source: `gpt-5.3-codex`
- Target: `gpt-5.3-codex-xhigh`

Result:

- Incoming `gpt-5.3-codex` -> rewritten to `gpt-5.3-codex-xhigh`
- Incoming `gpt-5.5` -> unchanged

## Run

```powershell
python rightcode_proxy_gui.py
```

Then set your Codex/ccswitch provider `base_url` to:

```text
http://127.0.0.1:8787/codex/v1
```

## Build EXE

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed --name rightcode-proxy-gui rightcode_proxy_gui.py
```

Output:

- `dist/rightcode-proxy-gui.exe`

## Generated files

- `rightcode_proxy_config.json`
- `rightcode_proxy_events.jsonl`
- `rightcode_proxy_requests.jsonl`

## Notes

- Authorization headers are forwarded to your configured upstream.
- Rules are order-sensitive; first exact match wins.

## License

MIT
