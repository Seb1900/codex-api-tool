#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import http.client
import json
import queue
import ssl
import threading
import tkinter as tk
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from urllib.parse import urlsplit


APP_TITLE = "RightCode Model Mode Proxy"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "rightcode_proxy_config.json"
EVENT_LOG_PATH = APP_DIR / "rightcode_proxy_events.jsonl"

MODES = ("low", "medium", "high", "xhigh")
DEFAULT_BASE_MODEL = "gpt-5.3-codex"
DEFAULT_UPSTREAM = "https://right.codes/codex/v1"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8787

HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class ProxyConfig:
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    upstream_base_url: str = DEFAULT_UPSTREAM
    base_model: str = DEFAULT_BASE_MODEL
    mode: str = "xhigh"
    rewrite_reasoning_effort: bool = True

    @property
    def target_model(self) -> str:
        return f"{self.base_model}-{self.mode}"


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config: ProxyConfig = load_config()
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.running = False
        self.last_event: dict[str, Any] | None = None
        self.last_error: str = ""
        self.total_requests = 0
        self.total_rewrites = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "last_error": self.last_error,
                "total_requests": self.total_requests,
                "total_rewrites": self.total_rewrites,
                "last_event": self.last_event,
                "config": asdict(self.config),
            }

    def update_config(self, cfg: ProxyConfig) -> None:
        with self.lock:
            self.config = cfg
        save_config(cfg)

    def record_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.last_event = event
            self.total_requests += 1
            if event.get("rewritten"):
                self.total_rewrites += 1
        self.event_queue.put(event)
        log_event(event)

    def set_running(self, running: bool) -> None:
        with self.lock:
            self.running = running
            if running:
                self.last_error = ""

    def set_error(self, msg: str) -> None:
        with self.lock:
            self.last_error = msg
        log_event({"ts": now_iso(), "kind": "error", "message": msg})


def load_config() -> ProxyConfig:
    if not CONFIG_PATH.exists():
        return ProxyConfig()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ProxyConfig()
    cfg = ProxyConfig()
    for key in asdict(cfg).keys():
        if key in data:
            setattr(cfg, key, data[key])
    if cfg.mode not in MODES:
        cfg.mode = "xhigh"
    return cfg


def save_config(cfg: ProxyConfig) -> None:
    ensure_parent(CONFIG_PATH)
    CONFIG_PATH.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")


def log_event(event: dict[str, Any]) -> None:
    ensure_parent(EVENT_LOG_PATH)
    with EVENT_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "RightCodeModeProxy/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return

    def _app_state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            snap = self._app_state().snapshot()
            body = json.dumps(
                {
                    "ok": True,
                    "running": snap["running"],
                    "total_requests": snap["total_requests"],
                    "total_rewrites": snap["total_rewrites"],
                    "config": snap["config"],
                    "time": now_iso(),
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        self._proxy_request("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._proxy_request("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._proxy_request("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._proxy_request("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._proxy_request("OPTIONS")

    def _proxy_request(self, method: str) -> None:
        app_state = self._app_state()
        cfg = app_state.config

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        rewritten_raw = raw
        original_model: str | None = None
        rewritten_model: str | None = None
        reasoning_effort: str | None = None
        rewritten = False
        parse_error: str | None = None

        content_type = (self.headers.get("Content-Type") or "").lower()
        should_parse_json = method in {"POST", "PUT", "PATCH"} and "application/json" in content_type

        if should_parse_json and raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, dict):
                    original_model = payload.get("model")
                    rewritten_model = original_model

                    if isinstance(payload.get("reasoning"), dict):
                        reasoning_effort = payload["reasoning"].get("effort")

                    target_model = cfg.target_model
                    if original_model == cfg.base_model:
                        payload["model"] = target_model
                        rewritten_model = target_model
                        rewritten = True

                    if cfg.rewrite_reasoning_effort:
                        if not isinstance(payload.get("reasoning"), dict):
                            payload["reasoning"] = {}
                        payload["reasoning"]["effort"] = cfg.mode
                        reasoning_effort = cfg.mode

                    rewritten_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {exc}"

        upstream = urlsplit(cfg.upstream_base_url)
        if upstream.scheme not in {"http", "https"} or not upstream.netloc:
            msg = f"invalid upstream_base_url: {cfg.upstream_base_url}"
            app_state.set_error(msg)
            self._write_text(500, msg)
            return

        upstream_path = self.path
        base_path = upstream.path.rstrip("/")
        if base_path and not upstream_path.startswith(base_path):
            if not upstream_path.startswith("/"):
                upstream_path = "/" + upstream_path
            upstream_path = base_path + upstream_path

        if upstream.scheme == "https":
            conn = http.client.HTTPSConnection(
                upstream.hostname,
                upstream.port or 443,
                timeout=180,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                upstream.hostname,
                upstream.port or 80,
                timeout=180,
            )

        headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            headers[key] = value
        headers["Host"] = upstream.netloc
        headers["Content-Length"] = str(len(rewritten_raw))

        status = 502
        reason = "Bad Gateway"
        request_id = None
        try:
            conn.request(method, upstream_path, body=rewritten_raw, headers=headers)
            resp = conn.getresponse()
            status = resp.status
            reason = resp.reason
            request_id = resp.getheader("x-oneapi-request-id")

            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                lk = key.lower()
                if lk in HOP_BY_HOP_HEADERS or lk == "content-length":
                    continue
                self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()

            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self._write_text(502, f"proxy error: {reason}")
            app_state.set_error(reason)
        finally:
            conn.close()

        event = {
            "ts": now_iso(),
            "kind": "request",
            "method": method,
            "path": self.path,
            "upstream": f"{upstream.scheme}://{upstream.netloc}{upstream_path}",
            "status": status,
            "reason": reason,
            "request_id": request_id,
            "mode": cfg.mode,
            "base_model": cfg.base_model,
            "target_model": cfg.target_model,
            "original_model": original_model,
            "rewritten_model": rewritten_model,
            "rewritten": rewritten,
            "reasoning_effort": reasoning_effort,
            "parse_error": parse_error,
        }
        app_state.record_event(event)

    def _write_text(self, code: int, text: str) -> None:
        body = (text + "\n").encode("utf-8", errors="replace")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class ProxyApp(tk.Tk):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state_obj = state
        self.title(APP_TITLE)
        self.geometry("760x560")
        self.minsize(720, 520)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.listen_host_var = tk.StringVar(value=state.config.listen_host)
        self.listen_port_var = tk.StringVar(value=str(state.config.listen_port))
        self.upstream_var = tk.StringVar(value=state.config.upstream_base_url)
        self.base_model_var = tk.StringVar(value=state.config.base_model)
        self.mode_var = tk.StringVar(value=state.config.mode)
        self.rewrite_reasoning_var = tk.BooleanVar(value=state.config.rewrite_reasoning_effort)

        self.status_var = tk.StringVar(value="状态：未启动")
        self.counter_var = tk.StringVar(value="请求 0 / 改写 0")
        self.last_var = tk.StringVar(value="最后事件：无")

        self._build_ui()
        self.after(300, self.refresh_status)
        self.after(600, self.poll_events)

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)

        ttk.Label(top, text="监听地址").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.listen_host_var, width=18).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(top, text="监听端口").grid(row=0, column=2, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.listen_port_var, width=10).grid(row=0, column=3, sticky="w", **pad)

        ttk.Label(top, text="上游地址").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.upstream_var, width=62).grid(row=1, column=1, columnspan=3, sticky="we", **pad)

        ttk.Label(top, text="基础模型").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(top, textvariable=self.base_model_var, width=24).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(top, text="模式").grid(row=2, column=2, sticky="w", **pad)
        mode_box = ttk.Combobox(top, textvariable=self.mode_var, values=MODES, state="readonly", width=12)
        mode_box.grid(row=2, column=3, sticky="w", **pad)

        ttk.Checkbutton(
            top,
            text="同步改写 reasoning.effort",
            variable=self.rewrite_reasoning_var,
            onvalue=True,
            offvalue=False,
        ).grid(row=3, column=0, columnspan=2, sticky="w", **pad)

        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=4)
        ttk.Button(btn_row, text="启动", command=self.start_proxy).pack(side="left", padx=6)
        ttk.Button(btn_row, text="停止", command=self.stop_proxy).pack(side="left", padx=6)
        ttk.Button(btn_row, text="刷新状态", command=self.refresh_status).pack(side="left", padx=6)
        ttk.Button(btn_row, text="打开日志目录", command=self.open_log_dir).pack(side="left", padx=6)

        status = ttk.LabelFrame(self, text="状态")
        status.pack(fill="x", padx=10, pady=8)
        ttk.Label(status, textvariable=self.status_var).pack(anchor="w", padx=10, pady=4)
        ttk.Label(status, textvariable=self.counter_var).pack(anchor="w", padx=10, pady=4)
        ttk.Label(status, textvariable=self.last_var).pack(anchor="w", padx=10, pady=4)

        tips = ttk.LabelFrame(self, text="使用提示")
        tips.pack(fill="x", padx=10, pady=6)
        tip_text = (
            "1. 先点“启动”，再把 Codex/ccswitch 的 base_url 指到 http://监听地址:监听端口/codex/v1\n"
            "2. 模式 low/medium/high/xhigh 会对应模型后缀 -low/-medium/-high/-xhigh\n"
            "3. 代理会保留原请求头并转发 SSE；事件会写入 rightcode_proxy_events.jsonl"
        )
        ttk.Label(tips, text=tip_text, justify="left").pack(anchor="w", padx=10, pady=6)

        log_box = ttk.LabelFrame(self, text="最近事件")
        log_box.pack(fill="both", expand=True, padx=10, pady=8)
        self.log_text = tk.Text(log_box, wrap="word", height=14, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=8)

    def read_form_config(self) -> ProxyConfig:
        listen_host = self.listen_host_var.get().strip() or DEFAULT_LISTEN_HOST
        listen_port_text = self.listen_port_var.get().strip()
        upstream = self.upstream_var.get().strip() or DEFAULT_UPSTREAM
        base_model = self.base_model_var.get().strip() or DEFAULT_BASE_MODEL
        mode = self.mode_var.get().strip()
        if mode not in MODES:
            raise ValueError(f"mode 必须是 {MODES}")
        try:
            listen_port = int(listen_port_text)
        except ValueError as exc:
            raise ValueError("监听端口必须是整数") from exc
        if not (1 <= listen_port <= 65535):
            raise ValueError("监听端口必须在 1-65535")

        return ProxyConfig(
            listen_host=listen_host,
            listen_port=listen_port,
            upstream_base_url=upstream,
            base_model=base_model,
            mode=mode,
            rewrite_reasoning_effort=bool(self.rewrite_reasoning_var.get()),
        )

    def start_proxy(self) -> None:
        if self.state_obj.running:
            messagebox.showinfo(APP_TITLE, "代理已经在运行。")
            return
        try:
            cfg = self.read_form_config()
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.state_obj.update_config(cfg)
        self.state_obj.set_error("")

        try:
            server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), ProxyHandler)
            server.app_state = self.state_obj  # type: ignore[attr-defined]
        except Exception as exc:
            self.state_obj.set_error(str(exc))
            self.refresh_status()
            messagebox.showerror(APP_TITLE, f"启动失败：{exc}")
            return

        def run_server() -> None:
            try:
                server.serve_forever(poll_interval=0.4)
            except Exception as exc:
                self.state_obj.set_error(str(exc))
            finally:
                server.server_close()
                self.state_obj.set_running(False)

        thread = threading.Thread(target=run_server, daemon=True, name="rightcode-proxy-server")
        self.state_obj.server = server
        self.state_obj.server_thread = thread
        self.state_obj.set_running(True)
        thread.start()

        log_event(
            {
                "ts": now_iso(),
                "kind": "startup",
                "listen": f"http://{cfg.listen_host}:{cfg.listen_port}",
                "upstream": cfg.upstream_base_url,
                "base_model": cfg.base_model,
                "mode": cfg.mode,
                "target_model": cfg.target_model,
            }
        )
        self.append_log(f"[{now_iso()}] 启动成功: http://{cfg.listen_host}:{cfg.listen_port}")
        self.refresh_status()

    def stop_proxy(self) -> None:
        if not self.state_obj.running or not self.state_obj.server:
            self.append_log(f"[{now_iso()}] 停止请求：代理未运行")
            self.refresh_status()
            return
        self.state_obj.server.shutdown()
        self.state_obj.server = None
        self.state_obj.server_thread = None
        self.state_obj.set_running(False)
        log_event({"ts": now_iso(), "kind": "shutdown"})
        self.append_log(f"[{now_iso()}] 代理已停止")
        self.refresh_status()

    def refresh_status(self) -> None:
        snap = self.state_obj.snapshot()
        cfg = snap["config"]
        running = snap["running"]
        status = (
            f"状态：运行中 监听 http://{cfg['listen_host']}:{cfg['listen_port']}"
            if running
            else "状态：未启动"
        )
        if snap["last_error"]:
            status += f" | 错误：{snap['last_error']}"
        self.status_var.set(status)
        self.counter_var.set(f"请求 {snap['total_requests']} / 改写 {snap['total_rewrites']}")

        last_event = snap.get("last_event")
        if last_event:
            last_line = (
                f"最后事件：{last_event.get('status')} "
                f"{last_event.get('original_model')} -> {last_event.get('rewritten_model')} "
                f"(mode={last_event.get('mode')})"
            )
        else:
            last_line = "最后事件：无"
        self.last_var.set(last_line)

    def poll_events(self) -> None:
        processed = False
        while True:
            try:
                event = self.state_obj.event_queue.get_nowait()
            except queue.Empty:
                break
            processed = True
            self.append_log(
                f"[{event.get('ts')}] {event.get('status')} {event.get('original_model')} -> "
                f"{event.get('rewritten_model')} rewritten={event.get('rewritten')} id={event.get('request_id')}"
            )
        if processed:
            self.refresh_status()
        self.after(500, self.poll_events)

    def append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def open_log_dir(self) -> None:
        try:
            ensure_parent(EVENT_LOG_PATH)
            EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            import os

            os.startfile(str(EVENT_LOG_PATH.parent))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"打开目录失败：{exc}")

    def on_close(self) -> None:
        try:
            self.stop_proxy()
        except Exception:
            pass
        self.destroy()


def main() -> int:
    state = AppState()
    app = ProxyApp(state)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
