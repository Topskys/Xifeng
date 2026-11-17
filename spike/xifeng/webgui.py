"""Web GUI：标准库 HTTP 服务 + Edge 应用模式窗口。

为什么不用 pywebview/Tauri：pywebview 依赖 pythonnet（本机 .NET 运行时加载失败），
Tauri 要 ~6GB Rust 工具链（D6 推迟）。Edge `--app=URL` 是无地址栏的独立窗口，
WebView2 由系统自带，宿主保持纯 Python。

- 服务只绑定 127.0.0.1，数据不出本机（AGENTS 隐私约束）
- 前端构建产物：源码 `spike/frontend/dist`；冻结 `<exe 同级>/frontend`
- API 全部 JSON；错误以 500 + {"error": ...} 返回
"""

from __future__ import annotations

import ctypes
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, autostart, correction, paths
from . import settings as user_settings
from .store import Store

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",   # 回退
)
REPO = os.environ.get("IME_REPO", "Topskys/Xifeng")   # 更新检查 / GitHub 跳转用
PORT = 8642  # 与 frontend/vite.config.ts 的 dev 代理一致
PAGES = ("stats", "models", "config", "history", "about")


def version_key(text: str) -> tuple[int, ...]:
    """`v1.2.3-rc1` → `(1, 2, 3)`；按数字段比较，避免字符串比较把 0.10 判小于 0.9。"""
    out: list[int] = []
    for part in str(text).strip().lstrip("vV").split(".")[:4]:
        head = ""
        for ch in part:
            if not ch.isdigit():
                break
            head += ch
        out.append(int(head) if head else 0)
    out += [0] * (4 - len(out))   # 补齐，1.0 与 1.0.0 必须相等
    return tuple(out)


def find_browser() -> str | None:
    """界面窗口用 Chrome（用户指定）；Chrome 不存在才回退 Edge。"""
    for p in CHROME_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def front_dist() -> Path:
    if paths.is_frozen():
        return paths.app_dir() / "frontend"
    return Path(__file__).resolve().parents[1] / "frontend" / "dist"


# ---------------------------------------------------------------- 与 gui.py 同逻辑的只读助手（不依赖 tkinter）


class _Runtime:
    """CPU / 内存读数：只用 Win32。与 gui.py 的实现保持一致。"""

    def __init__(self) -> None:
        self._prev = None
        self._k32 = None
        if sys.platform == "win32":
            try:
                self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                self._k32.GetSystemTimes.argtypes = (
                    ctypes.POINTER(ctypes.c_ulonglong),
                    ctypes.POINTER(ctypes.c_ulonglong),
                    ctypes.POINTER(ctypes.c_ulonglong),
                )
            except Exception:
                self._k32 = None

    def cpu_percent(self) -> int | None:
        if self._k32 is None:
            return None
        idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
        try:
            if not self._k32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
        except Exception:
            return None
        total = kernel.value + user.value
        if self._prev is None:
            self._prev = (idle.value, total)
            return None
        prev_idle, prev_total = self._prev
        self._prev = (idle.value, total)
        delta = total - prev_total
        if delta <= 0:
            return None
        return max(0, min(100, round((delta - (idle.value - prev_idle)) * 100 / delta)))

    @staticmethod
    def memory() -> tuple[float, float]:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            total = st.ullTotalPhys / 1024**3
            used = (st.ullTotalPhys - st.ullAvailPhys) / 1024**3
            return used, total
        return 0.0, 0.0


def _model_files(d: Path) -> dict[str, bool]:
    return {
        "int8": (d / "model.int8.onnx").exists(),
        "onnx": (d / "model.onnx").exists(),
        "tokens": (d / "tokens.txt").exists(),
    }


def _model_ready(d: Path) -> bool:
    f = _model_files(d)
    return f["tokens"] and (f["int8"] or f["onnx"])


def _model_quant(d: Path) -> str:
    f = _model_files(d)
    if f["int8"]:
        return "int8"
    return "fp32" if f["onnx"] else "—"


def _model_size(d: Path) -> int:
    total = 0
    try:
        for e in d.rglob("*"):
            if e.is_file():
                total += e.stat().st_size
    except OSError:
        pass
    return total


def _discover(store: Store) -> dict:
    raw = (user_settings.load(store).model_dir or "").strip()
    current = Path(raw) if raw else paths.models_dir()
    found: dict[str, Path] = {}
    if current.exists():
        found[str(current.resolve())] = current
    for parent in {current.parent, paths.models_dir().parent}:
        try:
            if not parent.exists():
                continue
            for child in parent.iterdir():
                if child.is_dir() and _model_ready(child):
                    found.setdefault(str(child.resolve()), child)
        except OSError:
            continue
    lst = [
        {
            "path": str(p),
            "name": p.name,
            "quant": _model_quant(p),
            "size": _model_size(p),
            "ready": _model_ready(p),
        }
        for p in sorted(found.values(), key=lambda p: (not _model_ready(p), p.name.lower()))
    ]
    cur = str(current.resolve()) if current.exists() else str(current)
    for m in lst:
        m["current"] = os.path.normcase(m["path"]) == os.path.normcase(cur)
    return {"current": cur, "list": lst}


# ---------------------------------------------------------------- API


class Api:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.settings = user_settings.load(store)
        self.runtime = _Runtime()
        self.last_ping: float | None = None

    def ping(self, _body: dict | None = None) -> dict:
        """前端心跳：窗口还开着就续命，关掉 30s 后服务自动退出（不留下僵尸进程）。"""
        self.last_ping = time.monotonic()
        return {"ok": True}

    # -- 内部

    def _boot(self) -> dict:
        return {
            "version": __version__,
            "dataDir": str(self.store.path.parent),
            "settings": self.settings.__dict__ | {},
            "models": _discover(self.store),
            "correction": {
                "available": correction.available(),
                "name": "Qwen2.5-0.5B-Instruct Q4_K_M",
                "sizeMB": 469,
                "dir": str(correction.model_path().parent),
            },
            "autostart": autostart.is_enabled(),
        }

    def _settings(self) -> dict:
        return dict(self.settings.__dict__)

    # -- 端点（方法名 = URL 路径）

    def bootstrap(self) -> dict:
        return self._boot()

    def stats(self, period: str = "today") -> dict:
        days = {"today": 1, "week": 7, "all": None}.get(period, 1)
        s = self.store.stats(days)
        today = self.store.stats(1)
        all_stats = self.store.stats(None)
        cpu = self.runtime.cpu_percent()
        mem_used, mem_total = self.runtime.memory()
        rows = [
            {
                "ts": r["ts"],
                "ms_asr": int(r["ms_asr"]),
                "ms_post": int(r["ms_post"]),
                "ms_inject": int(r["ms_inject"]),
                "e2e_ms": int(r["e2e_ms"]),
            }
            for r in self.store.history_page(days, 0, 12)[0]
            if int(r["e2e_ms"]) > 0
        ]
        model_dir = Path(self.settings.model_dir or "") if self.settings.model_dir else paths.models_dir()
        return {
            "threads": self.settings.threads,
            "asr": s["asr"],
            "by_outcome": s["by_outcome"],
            "daily": self._daily14(),
            "kpis": {
                "p50": int(s["e2e"]["p50"]),
                "p95": int(s["e2e"]["p95"]),
                "charsToday": int(today["chars"]),
                "charsAll": int(all_stats["chars"]),
                "total": int(s["total"]),
                "injected": int(s["injected"]),
                "successRate": float(s["success_rate"]),
                "failed": int(s["by_outcome"].get("Failed", 0)),
                "dropped": int(s["by_outcome"].get("Dropped", 0)),
                "timeouts": int(s["timeouts"]),
            },
            "rows": rows,
            "runtime": {"cpu": cpu, "memUsed": mem_used, "memTotal": mem_total},
            "model": {
                "name": model_dir.name,
                "quant": _model_quant(model_dir),
                "ready": _model_ready(model_dir),
            },
        }

    def _daily14(self) -> list[dict]:
        """最近 14 天逐日序列（无记录的天补 0），旧→新。"""
        from datetime import date, timedelta

        by = {d["date"]: d for d in self.store.daily(14)}
        today = date.today()
        out = []
        for i in range(13, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            out.append(by.get(d) or {"date": d, "count": 0, "chars": 0, "avg_e2e": 0})
        return out

    def history(self, period: str = "all", offset: int = 0, limit: int = 50) -> dict:
        days = {"today": 1, "week": 7, "all": None}.get(period, None)
        rows_raw, _total = self.store.history_page(days, offset, limit)
        rows = [
            {
                "id": int(r["id"]),
                "ts": r["ts"],
                "mode": r["mode"],
                "outcome": r["outcome"],
                "text": r["text"],
                "raw_text": r["raw_text"],
                "clean_text": r["clean_text"],
                "chars": int(r["chars"]),
                "speech_ms": int(r["speech_ms"]),
                "e2e_ms": int(r["e2e_ms"]),
                "ms_asr": int(r["ms_asr"]),
                "ms_post": int(r["ms_post"]),
                "ms_inject": int(r["ms_inject"]),
                "strategy": r["strategy"],
                "error": r["error"],
                "fixes": int(r["fixes"]),
            }
            for r in rows_raw
        ]
        return {"rows": rows, "has_more": len(rows) == limit}

    def history_delete(self, body: dict) -> dict:
        self.store.delete_history(int(body["id"]))
        return {"ok": True}

    def settings_save(self, body: dict) -> dict:
        self.settings = user_settings.update(self.settings, **body)
        user_settings.save(self.store, self.settings)
        return self._settings()

    def settings_reset(self) -> dict:
        self.settings = user_settings.update(user_settings.DEFAULTS)
        user_settings.save(self.store, self.settings)
        return self._settings()

    def theme(self, body: dict) -> dict:
        return self.settings_save({"theme": body.get("theme", "dark")})

    def models_use(self, body: dict) -> dict:
        path = Path(body["path"])
        if not _model_ready(path):
            raise ValueError("该目录缺少 model.onnx（或 int8）与 tokens.txt")
        self.settings = user_settings.update(self.settings, model_dir=str(path))
        user_settings.save(self.store, self.settings)
        return _discover(self.store)

    def autostart(self, body: dict) -> dict:
        try:
            if body.get("enabled"):
                autostart.enable()
            else:
                autostart.disable()
            return {"ok": True, "error": ""}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def prune(self) -> dict:
        return {"removed": self.store.prune(30)}

    def check_update(self) -> dict:
        """查 GitHub latest release 版本；网络失败显式返回错误。"""
        import json as _json
        import urllib.error
        import urllib.request

        url = f"https://api.github.com/repos/{REPO}/releases/latest"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "xifeng"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = _json.loads(r.read().decode("utf-8"))
            latest = str(data.get("tag_name", "")).lstrip("vV")
            return {"latest": latest, "current": __version__,
                    "update": version_key(latest) > version_key(__version__)}
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"error": "仓库暂无 Release（或仓库未公开）"}
            return {"error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)[:120]}

    def models_find(self, body: dict) -> dict:
        """按目录名 + 文件清单在本机候选位置匹配模型目录（浏览器拿不到绝对路径）。"""
        name = str(body.get("name") or "").strip().lower()
        wanted = set(str(f) for f in (body.get("files") or []))
        if not name or not wanted:
            return {"matches": []}
        roots = []
        for r in (paths.models_dir().parent, paths.app_dir(), Path.home()):
            try:
                if r.exists() and r not in roots:
                    roots.append(r)
            except Exception:  # noqa: BLE001
                pass
        matches: list[str] = []
        seen: set[str] = set()
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root):
                if len(Path(dirpath).parts) - len(root.parts) > 3:
                    dirnames[:] = []          # 限深 3 层
                    continue
                low = Path(dirpath).name.lower()
                if low == name and dirpath not in seen:
                    fset = set(filenames)
                    if "tokens.txt" in fset and ("model.int8.onnx" in fset or "model.onnx" in fset):
                        matches.append(dirpath)
                        seen.add(dirpath)
                if len(matches) >= 5:
                    break
            if len(matches) >= 5:
                break
        return {"matches": matches}

    def open(self, body: dict) -> dict:
        raw = body.get("path", "")
        target = self.store.path.parent if raw == "__data__" else Path(raw)
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606
        return {"ok": True}


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    api: Api
    dist: Path

    def log_message(self, fmt, *args):  # noqa: ANN001 - 静默访问日志
        pass

    def _json(self, obj, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str) -> None:
        target = (self.dist / rel.lstrip("/")).resolve()
        if not str(target).startswith(str(self.dist.resolve())):
            self._json({"error": "forbidden"}, 403)
            return
        if not target.is_file():
            target = self.dist / "index.html"  # SPA 回退
        if not target.is_file():
            self._json(
                {"error": "前端未构建：cd frontend && pnpm build（产物应在 " + str(self.dist) + "）"},
                500,
            )
            return
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        path, _, query = self.path.partition("?")
        try:
            if path == "/api/bootstrap":
                self._json(self.api.bootstrap())
            elif path == "/api/stats":
                period = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("period", "today")
                self._json(self.api.stats(period))
            elif path == "/api/history":
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                self._json(
                    self.api.history(
                        q.get("period", "all"),
                        int(q.get("offset", 0)),
                        int(q.get("limit", 50)),
                    )
                )
            elif path.startswith("/api/"):
                self._json({"error": f"未知端点 {path}"}, 404)
            else:
                self._static("/" if path == "/" else path)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)[:200]}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        routes = {
            "/api/ping": self.api.ping,
            "/api/history/delete": self.api.history_delete,
            "/api/settings/save": self.api.settings_save,
            "/api/settings/reset": self.api.settings_reset,
            "/api/theme": self.api.theme,
            "/api/models/use": self.api.models_use,
            "/api/autostart": self.api.autostart,
            "/api/prune": self.api.prune,
            "/api/check_update": self.api.check_update,
            "/api/models/find": self.api.models_find,
            "/api/open": self.api.open,
        }
        handler = routes.get(path)
        if handler is None:
            self._json({"error": f"未知端点 {path}"}, 404)
            return
        try:
            self._json(handler(body))
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)[:200]}, 500)


def _service_url(tab: str = "stats") -> str:
    page = tab if tab in PAGES else "stats"
    return f"http://127.0.0.1:{PORT}/#{page}"


def _port_busy() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.4):
            return True
    except OSError:
        return False


def _is_our_service() -> bool:
    """端口被占用时确认是不是本程序的旧实例，避免把别人的服务当窗口打开。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/bootstrap", timeout=1.5) as r:
            data = json.loads(r.read().decode("utf-8"))
        return isinstance(data, dict) and "version" in data and "settings" in data
    except Exception:  # noqa: BLE001
        return False


def open_window(url: str) -> None:
    """用 Chrome 应用模式开一个无地址栏窗口；失败时打印地址供手动打开。"""
    browser = find_browser()
    if not browser:
        print("[web] 未找到 Chrome，请手动打开 " + url, flush=True)
        return
    subprocess.Popen(  # noqa: S603
        [
            browser, f"--app={url}", "--window-size=1200,780",
            "--no-first-run", "--disable-sync",
        ],
        close_fds=True,
    )
    print("[web] 浏览器应用窗口已启动", flush=True)


def _watchdog(server, api: "Api", *, interval: float = 5.0, timeout: float = 30.0) -> None:
    """前端每 5s 一次心跳；窗口关闭后 30s 没有心跳就收工。"""
    while True:
        time.sleep(interval)
        last = api.last_ping
        if last is not None and time.monotonic() - last > timeout:
            server.shutdown()
            return


def run(db_path: str | None = None, tab: str = "stats") -> int:
    """启动本地服务并打开 Edge 应用模式窗口；窗口关闭后服务自动退出。"""
    url = _service_url(tab)

    # 托盘每次点菜单都会起一个进程：已有服务就只开窗口，绝不重复监听端口
    if _port_busy():
        if not _is_our_service():
            print(f"[web] 端口 {PORT} 已被其它程序占用，无法启动", flush=True)
            return 1
        print("[web] 已有服务在运行，直接打开窗口", flush=True)
        open_window(url)
        return 0

    store = Store(db_path)
    api = Api(store)
    dist = front_dist()
    Handler.api = api
    Handler.dist = dist

    # MUST 用单线程 HTTPServer：Store 在本线程创建，请求也必须在本线程处理
    # （sqlite3 check_same_thread）。GUI 场景没有并发压力，串行足够。
    server = __import__("http.server", fromlist=["HTTPServer"]).HTTPServer(
        ("127.0.0.1", PORT), Handler
    )
    print(f"[web] 服务 http://127.0.0.1:{PORT}（仅本机） dist={dist}", flush=True)

    threading.Thread(
        target=_watchdog, args=(server, api), daemon=True, name="web-watchdog"
    ).start()

    open_window(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[web] 退出", flush=True)
    finally:
        server.server_close()
        store.close()
    return 0
