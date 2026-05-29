#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import http.client
import json
import os
import queue
import ssl
import threading
import tkinter as tk
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any
from urllib.parse import urlsplit


APP_TITLE = "Codex API Rule Proxy"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "rightcode_proxy_config.json"
EVENT_LOG_PATH = APP_DIR / "rightcode_proxy_events.jsonl"
REQUEST_LOG_PATH = APP_DIR / "rightcode_proxy_requests.jsonl"

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


def safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


@dataclass
class ModelRule:
    source_model: str
    target_model: str

    def to_dict(self) -> dict[str, str]:
        return {"source_model": self.source_model, "target_model": self.target_model}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ModelRule | None":
        source_model = str(data.get("source_model", "")).strip()
        target_model = str(data.get("target_model", "")).strip()
        if not source_model or not target_model:
            return None
        return ModelRule(source_model=source_model, target_model=target_model)


@dataclass
class ProxyConfig:
    listen_host: str = DEFAULT_LISTEN_HOST
    listen_port: int = DEFAULT_LISTEN_PORT
    upstream_base_url: str = DEFAULT_UPSTREAM
    rules: list[ModelRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "upstream_base_url": self.upstream_base_url,
            "rules": [rule.to_dict() for rule in self.rules],
        }

    def clone(self) -> "ProxyConfig":
        return ProxyConfig(
            listen_host=self.listen_host,
            listen_port=self.listen_port,
            upstream_base_url=self.upstream_base_url,
            rules=[ModelRule(rule.source_model, rule.target_model) for rule in self.rules],
        )


def load_config() -> ProxyConfig:
    cfg = ProxyConfig()
    if not CONFIG_PATH.exists():
        return cfg

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return cfg

    cfg.listen_host = str(data.get("listen_host", cfg.listen_host)).strip() or DEFAULT_LISTEN_HOST
    cfg.listen_port = safe_int(data.get("listen_port"), cfg.listen_port)
    cfg.upstream_base_url = str(data.get("upstream_base_url", cfg.upstream_base_url)).strip() or DEFAULT_UPSTREAM

    rules: list[ModelRule] = []
    rules_data = data.get("rules")
    if isinstance(rules_data, list):
        for item in rules_data:
            if isinstance(item, dict):
                rule = ModelRule.from_dict(item)
                if rule:
                    rules.append(rule)

    # Backward compatibility with legacy single-rule config shape.
    if not rules:
        legacy_base_model = str(data.get("base_model", "")).strip()
        legacy_target_model = str(data.get("target_model", "")).strip()
        legacy_mode = str(data.get("mode", "")).strip()
        if legacy_base_model and legacy_target_model:
            rules.append(ModelRule(legacy_base_model, legacy_target_model))
        elif legacy_base_model and legacy_mode:
            rules.append(ModelRule(legacy_base_model, f"{legacy_base_model}-{legacy_mode}"))

    cfg.rules = rules
    return cfg


def save_config(cfg: ProxyConfig) -> None:
    ensure_parent(CONFIG_PATH)
    CONFIG_PATH.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config: ProxyConfig = load_config()
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.ui_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.running = False
        self.last_error = ""
        self.total_requests = 0
        self.total_rewrites = 0
        self.last_request: dict[str, Any] | None = None
        self.last_event: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "last_error": self.last_error,
                "total_requests": self.total_requests,
                "total_rewrites": self.total_rewrites,
                "last_request": self.last_request,
                "last_event": self.last_event,
                "config": self.config.to_dict(),
            }

    def get_config(self) -> ProxyConfig:
        with self.lock:
            return self.config.clone()

    def update_config(self, cfg: ProxyConfig) -> None:
        with self.lock:
            self.config = cfg.clone()
        save_config(cfg)
        self.record_event(
            {
                "kind": "config_saved",
                "listen": f"http://{cfg.listen_host}:{cfg.listen_port}",
                "upstream": cfg.upstream_base_url,
                "rule_count": len(cfg.rules),
            }
        )

    def set_running(self, running: bool) -> None:
        with self.lock:
            self.running = running
            if running:
                self.last_error = ""

    def set_error(self, message: str) -> None:
        with self.lock:
            self.last_error = message
        self.record_event({"kind": "error", "message": message})

    def record_event(self, event: dict[str, Any]) -> None:
        payload = {"ts": now_iso(), **event}
        with self.lock:
            self.last_event = payload
        append_jsonl(EVENT_LOG_PATH, payload)
        self.ui_queue.put({"channel": "event", "payload": payload})

    def record_request(self, request_event: dict[str, Any]) -> None:
        payload = {"ts": now_iso(), **request_event}
        with self.lock:
            self.total_requests += 1
            if payload.get("rewritten"):
                self.total_rewrites += 1
            self.last_request = payload
        append_jsonl(REQUEST_LOG_PATH, payload)
        self.ui_queue.put({"channel": "request", "payload": payload})

        # Keep a condensed event stream in the main event log.
        self.record_event(
            {
                "kind": "request",
                "status": payload.get("status"),
                "original_model": payload.get("original_model"),
                "final_model": payload.get("final_model"),
                "rewritten": payload.get("rewritten"),
                "request_id": payload.get("request_id"),
            }
        )


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexRuleProxy/2.0"

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
                    "time": now_iso(),
                    "running": snap["running"],
                    "total_requests": snap["total_requests"],
                    "total_rewrites": snap["total_rewrites"],
                    "last_error": snap["last_error"],
                    "config": snap["config"],
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
        state = self._app_state()
        cfg = state.get_config()

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""

        content_type = (self.headers.get("Content-Type") or "").lower()
        should_parse_json = method in {"POST", "PUT", "PATCH"} and "application/json" in content_type

        rewritten_raw = raw
        original_model: str | None = None
        final_model: str | None = None
        matched_source: str | None = None
        matched_target: str | None = None
        rewritten = False
        parse_error: str | None = None

        if should_parse_json and raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, dict):
                    original_model = payload.get("model")
                    final_model = original_model

                    if isinstance(original_model, str):
                        for rule in cfg.rules:
                            if original_model == rule.source_model:
                                matched_source = rule.source_model
                                matched_target = rule.target_model
                                payload["model"] = rule.target_model
                                final_model = rule.target_model
                                rewritten = True
                                break

                    rewritten_raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {exc}"

        upstream = urlsplit(cfg.upstream_base_url)
        if upstream.scheme not in {"http", "https"} or not upstream.netloc:
            msg = f"invalid upstream_base_url: {cfg.upstream_base_url}"
            state.set_error(msg)
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

        upstream_headers = {}
        for key, value in self.headers.items():
            if key.lower() in HOP_BY_HOP_HEADERS:
                continue
            upstream_headers[key] = value
        upstream_headers["Host"] = upstream.netloc
        upstream_headers["Content-Length"] = str(len(rewritten_raw))

        status = 502
        reason = "Bad Gateway"
        request_id: str | None = None
        try:
            conn.request(method, upstream_path, body=rewritten_raw, headers=upstream_headers)
            resp = conn.getresponse()
            status = resp.status
            reason = resp.reason
            request_id = resp.getheader("x-oneapi-request-id")

            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                lower = key.lower()
                if lower in HOP_BY_HOP_HEADERS or lower == "content-length":
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
            state.set_error(reason)
        finally:
            conn.close()

        state.record_request(
            {
                "kind": "request",
                "method": method,
                "path": self.path,
                "upstream": f"{upstream.scheme}://{upstream.netloc}{upstream_path}",
                "status": status,
                "reason": reason,
                "request_id": request_id,
                "original_model": original_model,
                "final_model": final_model,
                "rewritten": rewritten,
                "matched_source_model": matched_source,
                "matched_target_model": matched_target,
                "parse_error": parse_error,
            }
        )

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
        self.geometry("980x720")
        self.minsize(900, 640)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        cfg = state.get_config()
        self.listen_host_var = tk.StringVar(value=cfg.listen_host)
        self.listen_port_var = tk.StringVar(value=str(cfg.listen_port))
        self.upstream_var = tk.StringVar(value=cfg.upstream_base_url)

        self.rule_source_var = tk.StringVar(value="")
        self.rule_target_var = tk.StringVar(value="")

        self.status_var = tk.StringVar(value="Status: stopped")
        self.counter_var = tk.StringVar(value="Requests 0 / Rewritten 0")
        self.last_var = tk.StringVar(value="Last request: none")

        self._build_ui()
        self._load_rules_to_table(cfg.rules)
        self.after(250, self.refresh_status)
        self.after(400, self.poll_ui_queue)

    def _build_ui(self) -> None:
        root = ttk.Frame(self)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        cfg_box = ttk.LabelFrame(root, text="Proxy Config")
        cfg_box.pack(fill="x", pady=6)

        ttk.Label(cfg_box, text="Listen Host").grid(row=0, column=0, padx=8, pady=6, sticky="w")
        ttk.Entry(cfg_box, textvariable=self.listen_host_var, width=16).grid(row=0, column=1, padx=8, pady=6, sticky="w")

        ttk.Label(cfg_box, text="Listen Port").grid(row=0, column=2, padx=8, pady=6, sticky="w")
        ttk.Entry(cfg_box, textvariable=self.listen_port_var, width=10).grid(row=0, column=3, padx=8, pady=6, sticky="w")

        ttk.Label(cfg_box, text="Upstream Base URL").grid(row=1, column=0, padx=8, pady=6, sticky="w")
        ttk.Entry(cfg_box, textvariable=self.upstream_var, width=72).grid(row=1, column=1, columnspan=3, padx=8, pady=6, sticky="we")

        btn_row = ttk.Frame(root)
        btn_row.pack(fill="x", pady=6)
        ttk.Button(btn_row, text="Start", command=self.start_proxy).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Stop", command=self.stop_proxy).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Refresh Status", command=self.refresh_status).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Open Log Folder", command=self.open_log_dir).pack(side="left", padx=4)

        status_box = ttk.LabelFrame(root, text="Runtime Status")
        status_box.pack(fill="x", pady=6)
        ttk.Label(status_box, textvariable=self.status_var).pack(anchor="w", padx=8, pady=3)
        ttk.Label(status_box, textvariable=self.counter_var).pack(anchor="w", padx=8, pady=3)
        ttk.Label(status_box, textvariable=self.last_var).pack(anchor="w", padx=8, pady=3)

        rules_box = ttk.LabelFrame(root, text="Rewrite Rules (Exact Match)")
        rules_box.pack(fill="both", expand=False, pady=6)

        input_row = ttk.Frame(rules_box)
        input_row.pack(fill="x", padx=8, pady=6)
        ttk.Label(input_row, text="Source Model").pack(side="left", padx=4)
        ttk.Entry(input_row, textvariable=self.rule_source_var, width=34).pack(side="left", padx=4)
        ttk.Label(input_row, text="Target Model").pack(side="left", padx=4)
        ttk.Entry(input_row, textvariable=self.rule_target_var, width=34).pack(side="left", padx=4)

        rule_btn_row = ttk.Frame(rules_box)
        rule_btn_row.pack(fill="x", padx=8, pady=4)
        ttk.Button(rule_btn_row, text="Add Rule", command=self.add_rule).pack(side="left", padx=4)
        ttk.Button(rule_btn_row, text="Update Selected", command=self.update_rule).pack(side="left", padx=4)
        ttk.Button(rule_btn_row, text="Delete Selected", command=self.delete_rule).pack(side="left", padx=4)
        ttk.Button(rule_btn_row, text="Clear Inputs", command=self.clear_rule_inputs).pack(side="left", padx=4)
        ttk.Button(rule_btn_row, text="Save Rules", command=self.save_rules_only).pack(side="left", padx=4)

        table_wrap = ttk.Frame(rules_box)
        table_wrap.pack(fill="both", expand=True, padx=8, pady=6)

        self.rules_table = ttk.Treeview(
            table_wrap,
            columns=("source_model", "target_model"),
            show="headings",
            height=8,
        )
        self.rules_table.heading("source_model", text="Source Model")
        self.rules_table.heading("target_model", text="Target Model")
        self.rules_table.column("source_model", width=360, anchor="w")
        self.rules_table.column("target_model", width=360, anchor="w")
        self.rules_table.pack(side="left", fill="both", expand=True)
        self.rules_table.bind("<<TreeviewSelect>>", self.on_rule_selected)

        scroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.rules_table.yview)
        self.rules_table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        logs_box = ttk.LabelFrame(root, text="Logs")
        logs_box.pack(fill="both", expand=True, pady=6)

        notebook = ttk.Notebook(logs_box)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        event_tab = ttk.Frame(notebook)
        request_tab = ttk.Frame(notebook)
        notebook.add(event_tab, text="Event Log")
        notebook.add(request_tab, text="Request Log")

        self.event_text = tk.Text(event_tab, wrap="word", state="disabled", height=12)
        self.event_text.pack(fill="both", expand=True)
        self.request_text = tk.Text(request_tab, wrap="word", state="disabled", height=12)
        self.request_text.pack(fill="both", expand=True)

    def _load_rules_to_table(self, rules: list[ModelRule]) -> None:
        self.rules_table.delete(*self.rules_table.get_children())
        for rule in rules:
            self.rules_table.insert("", "end", values=(rule.source_model, rule.target_model))

    def _collect_rules_from_table(self) -> list[ModelRule]:
        rules: list[ModelRule] = []
        for iid in self.rules_table.get_children():
            source_model, target_model = self.rules_table.item(iid, "values")
            source_model = str(source_model).strip()
            target_model = str(target_model).strip()
            if source_model and target_model:
                rules.append(ModelRule(source_model, target_model))
        return rules

    def _build_config_from_form(self) -> ProxyConfig:
        listen_host = self.listen_host_var.get().strip() or DEFAULT_LISTEN_HOST
        upstream = self.upstream_var.get().strip() or DEFAULT_UPSTREAM
        port_text = self.listen_port_var.get().strip()

        if not port_text:
            raise ValueError("Listen port is required.")
        try:
            listen_port = int(port_text)
        except ValueError as exc:
            raise ValueError("Listen port must be an integer.") from exc
        if not (1 <= listen_port <= 65535):
            raise ValueError("Listen port must be between 1 and 65535.")

        rules = self._collect_rules_from_table()
        return ProxyConfig(
            listen_host=listen_host,
            listen_port=listen_port,
            upstream_base_url=upstream,
            rules=rules,
        )

    def on_rule_selected(self, _event: Any) -> None:
        selected = self.rules_table.selection()
        if not selected:
            return
        values = self.rules_table.item(selected[0], "values")
        if len(values) >= 2:
            self.rule_source_var.set(str(values[0]))
            self.rule_target_var.set(str(values[1]))

    def clear_rule_inputs(self) -> None:
        self.rule_source_var.set("")
        self.rule_target_var.set("")

    def add_rule(self) -> None:
        source_model = self.rule_source_var.get().strip()
        target_model = self.rule_target_var.get().strip()
        if not source_model or not target_model:
            messagebox.showerror(APP_TITLE, "Source model and target model are both required.")
            return

        # Deterministic behavior: one source model maps to one target model.
        for iid in self.rules_table.get_children():
            row_source, _row_target = self.rules_table.item(iid, "values")
            if str(row_source).strip() == source_model:
                self.rules_table.item(iid, values=(source_model, target_model))
                self.rules_table.selection_set(iid)
                self.state_obj.record_event(
                    {
                        "kind": "rule_updated",
                        "source_model": source_model,
                        "target_model": target_model,
                    }
                )
                self.clear_rule_inputs()
                return

        self.rules_table.insert("", "end", values=(source_model, target_model))
        self.state_obj.record_event(
            {
                "kind": "rule_added",
                "source_model": source_model,
                "target_model": target_model,
            }
        )
        self.clear_rule_inputs()

    def update_rule(self) -> None:
        selected = self.rules_table.selection()
        if not selected:
            messagebox.showerror(APP_TITLE, "Select one rule to update.")
            return
        source_model = self.rule_source_var.get().strip()
        target_model = self.rule_target_var.get().strip()
        if not source_model or not target_model:
            messagebox.showerror(APP_TITLE, "Source model and target model are both required.")
            return
        iid = selected[0]
        self.rules_table.item(iid, values=(source_model, target_model))
        self.state_obj.record_event(
            {
                "kind": "rule_updated",
                "source_model": source_model,
                "target_model": target_model,
            }
        )
        self.clear_rule_inputs()

    def delete_rule(self) -> None:
        selected = self.rules_table.selection()
        if not selected:
            messagebox.showerror(APP_TITLE, "Select one rule to delete.")
            return
        iid = selected[0]
        source_model, target_model = self.rules_table.item(iid, "values")
        self.rules_table.delete(iid)
        self.state_obj.record_event(
            {
                "kind": "rule_deleted",
                "source_model": source_model,
                "target_model": target_model,
            }
        )
        self.clear_rule_inputs()

    def save_rules_only(self) -> None:
        try:
            cfg = self._build_config_from_form()
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.state_obj.update_config(cfg)
        messagebox.showinfo(APP_TITLE, "Rules saved.")
        self.refresh_status()

    def start_proxy(self) -> None:
        if self.state_obj.running:
            messagebox.showinfo(APP_TITLE, "Proxy is already running.")
            return
        try:
            cfg = self._build_config_from_form()
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
            messagebox.showerror(APP_TITLE, f"Start failed: {exc}")
            return

        def run_server() -> None:
            try:
                server.serve_forever(poll_interval=0.4)
            except Exception as exc:
                self.state_obj.set_error(str(exc))
            finally:
                server.server_close()
                self.state_obj.set_running(False)

        thread = threading.Thread(target=run_server, daemon=True, name="codex-rule-proxy")
        self.state_obj.server = server
        self.state_obj.server_thread = thread
        self.state_obj.set_running(True)
        thread.start()

        self.state_obj.record_event(
            {
                "kind": "startup",
                "listen": f"http://{cfg.listen_host}:{cfg.listen_port}",
                "upstream": cfg.upstream_base_url,
                "rule_count": len(cfg.rules),
            }
        )
        self.refresh_status()

    def stop_proxy(self) -> None:
        if not self.state_obj.running or not self.state_obj.server:
            self.state_obj.record_event({"kind": "stop_requested_while_stopped"})
            self.refresh_status()
            return
        self.state_obj.server.shutdown()
        self.state_obj.server = None
        self.state_obj.server_thread = None
        self.state_obj.set_running(False)
        self.state_obj.record_event({"kind": "shutdown"})
        self.refresh_status()

    def refresh_status(self) -> None:
        snap = self.state_obj.snapshot()
        cfg = snap["config"]

        if snap["running"]:
            status_line = (
                f"Status: running at http://{cfg['listen_host']}:{cfg['listen_port']} "
                f"-> {cfg['upstream_base_url']}"
            )
        else:
            status_line = "Status: stopped"
        if snap["last_error"]:
            status_line += f" | Error: {snap['last_error']}"
        self.status_var.set(status_line)

        self.counter_var.set(
            f"Requests {snap['total_requests']} / Rewritten {snap['total_rewrites']} / Rules {len(cfg['rules'])}"
        )

        last_request = snap.get("last_request")
        if last_request:
            self.last_var.set(
                "Last request: "
                f"{last_request.get('original_model')} -> {last_request.get('final_model')} "
                f"(rewritten={last_request.get('rewritten')}, status={last_request.get('status')})"
            )
        else:
            self.last_var.set("Last request: none")

    def poll_ui_queue(self) -> None:
        changed = False
        while True:
            try:
                msg = self.state_obj.ui_queue.get_nowait()
            except queue.Empty:
                break

            channel = msg.get("channel")
            payload = msg.get("payload", {})
            changed = True
            if channel == "event":
                self.append_event_line(self.format_event_line(payload))
            elif channel == "request":
                self.append_request_line(self.format_request_line(payload))

        if changed:
            self.refresh_status()
        self.after(450, self.poll_ui_queue)

    def format_event_line(self, event: dict[str, Any]) -> str:
        kind = event.get("kind")
        ts = event.get("ts")
        if kind == "request":
            return (
                f"[{ts}] request status={event.get('status')} "
                f"{event.get('original_model')} -> {event.get('final_model')} "
                f"rewritten={event.get('rewritten')} id={event.get('request_id')}"
            )
        if kind in {"rule_added", "rule_updated", "rule_deleted"}:
            return (
                f"[{ts}] {kind} "
                f"{event.get('source_model')} -> {event.get('target_model')}"
            )
        if kind == "startup":
            return f"[{ts}] startup listen={event.get('listen')} upstream={event.get('upstream')}"
        if kind == "shutdown":
            return f"[{ts}] shutdown"
        if kind == "error":
            return f"[{ts}] error {event.get('message')}"
        if kind == "config_saved":
            return (
                f"[{ts}] config_saved listen={event.get('listen')} "
                f"upstream={event.get('upstream')} rules={event.get('rule_count')}"
            )
        return f"[{ts}] {json.dumps(event, ensure_ascii=False)}"

    def format_request_line(self, req: dict[str, Any]) -> str:
        return (
            f"[{req.get('ts')}] status={req.get('status')} method={req.get('method')} "
            f"model={req.get('original_model')} -> {req.get('final_model')} "
            f"rewritten={req.get('rewritten')} "
            f"matched={req.get('matched_source_model')}->{req.get('matched_target_model')} "
            f"id={req.get('request_id')}"
        )

    def append_event_line(self, line: str) -> None:
        self.event_text.configure(state="normal")
        self.event_text.insert("end", line + "\n")
        self.event_text.see("end")
        self.event_text.configure(state="disabled")

    def append_request_line(self, line: str) -> None:
        self.request_text.configure(state="normal")
        self.request_text.insert("end", line + "\n")
        self.request_text.see("end")
        self.request_text.configure(state="disabled")

    def open_log_dir(self) -> None:
        try:
            ensure_parent(EVENT_LOG_PATH)
            ensure_parent(REQUEST_LOG_PATH)
            os.startfile(str(APP_DIR))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Open log folder failed: {exc}")

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
