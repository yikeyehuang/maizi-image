#!/usr/bin/env python3
"""
共济生图 · 本地桌面版

双击运行 → 自动打开浏览器 → 首次填入 API Key → 直接生图。
所有数据都留在本机（配置、生成记录、图片缓存）。

  Windows 数据目录： %APPDATA%\maizi\
  macOS / Linux  ： ~/.maizi/
  可用环境变量 MAIZI_HOME 指定数据目录（便于多份配置并存）

依赖：仅 Python 3 标准库
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def data_home() -> Path:
    """数据目录：Windows 用 %APPDATA%\\maizi，其他系统用 ~/.maizi。"""
    env = os.environ.get("MAIZI_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "maizi"
    return Path.home() / ".maizi"


def resource_path(name: str) -> Path:
    """打包成 exe 后资源解到 sys._MEIPASS；开发态就在脚本旁边。"""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


HERE = Path(__file__).resolve().parent
DATA_HOME = data_home()
CONFIG_PATHS = (DATA_HOME / "config.json",)      # 桌面版只认自己的数据目录
DEFAULT_BASE = "https://www.maizitech.xyz"
# 生成记录：JSON 清单 + 本地图片缓存，重启/刷新都不丢
HISTORY_DIR = DATA_HOME / "history"
HISTORY_FILE = DATA_HOME / "history.json"
HISTORY_MAX = 300
_history_lock = threading.Lock()
RATIOS = ["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "5:4", "4:5", "21:9", "2:1", "1:2", "9:21", "3:1", "1:3"]
# 表单上的中文标签 → 接口实际取值（"自动"= 不指定，直接省略该字段）
BG_MAP = {"不透明": "opaque", "透明": "transparent"}
FMT_MAP = {"PNG": "png", "JPEG": "jpeg", "WEBP": "webp"}
# 分段控制器：型号 id → 展示名，以及展示顺序
MODEL_LABELS = {"gpt-image-2.5": "img-2.5（标准版）", "gpt-image-2.5-sunburst-t": "img-2.5（增强版）"}
MODEL_ORDER = ["gpt-image-2.5", "gpt-image-2.5-sunburst-t"]
# 平台型号页写死的精确能力（/v1/models 的 max_outputs 偏宽松，以型号页为准）
MODEL_LIMITS = {"gpt-image-2.5": {"resolutions": ["1K"], "outputs": 1, "references": 9}}
FALLBACK_MODEL = "gpt-image-2.5-sunburst-t"

_model_cache: dict = {}


class ApiError(RuntimeError):
    """带 HTTP 状态码的接口错误，便于区分「密钥无效」与「平台不可达」。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def config() -> dict:
    cfg: dict = {}
    for path in CONFIG_PATHS:
        try:
            if path.is_file():
                cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    import os
    cfg["apiKey"] = os.environ.get("MAIZI_API_KEY") or cfg.get("apiKey", "")
    cfg["baseURL"] = (os.environ.get("MAIZI_BASE_URL") or cfg.get("baseURL") or DEFAULT_BASE).rstrip("/")
    cfg["model"] = os.environ.get("MAIZI_MODEL") or cfg.get("model") or "gpt-image-2.5"
    return cfg


LOG_FILE = DATA_HOME / "app.log"


def log(msg: str) -> None:
    """控制台可能不存在（--noconsole），所以同时写日志文件。"""
    line = time.strftime("[%H:%M:%S] ") + msg
    try:
        if sys.stdout:
            print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


server_ref: list = [None]


def save_api_key(key: str) -> None:
    """写入数据目录的 config.json（不覆盖其它字段）。"""
    path = DATA_HOME / "config.json"
    data = {}
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data["apiKey"] = key
    data.setdefault("baseURL", DEFAULT_BASE)
    data.setdefault("model", MODEL_ORDER[0])      # 默认「标准版」，不是增强版
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def save_model(model: str) -> None:
    path = CONFIG_PATHS[0]
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data["model"] = model
    data["modelChosen"] = True      # 标记：这是用户自己选的，别再回退默认值
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 生成记录 ─────────────────────────────────────────────────────────
def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def history_read() -> list:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001 - 首次运行没有文件
        return []


def history_write(records: list) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(records[:HISTORY_MAX], ensure_ascii=False, indent=1), encoding="utf-8")


def history_add(record: dict) -> None:
    with _history_lock:
        records = history_read()
        records.insert(0, record)
        history_write(records)


def history_patch(rec_id: str, **fields) -> None:
    with _history_lock:
        records = history_read()
        for rec in records:
            if rec.get("id") == rec_id or rec.get("task_id") == rec_id:
                rec.update(fields)
                break
        else:
            records.insert(0, {"id": rec_id, "task_id": rec_id, **fields})
        history_write(records)


def history_finalize(task_id: str, status: str, urls: list, cost, error) -> list:
    """任务终态落库。一次生成 N 张 → 拆成 N 条记录，一条记录对应一张图。"""
    with _history_lock:
        records = history_read()
        pos = next((i for i, r in enumerate(records) if r.get("task_id") == task_id), 0)
        found = bool(records) and records[pos].get("task_id") == task_id
        base = records.pop(pos) if found else {"task_id": task_id, "created_at": now()}
        base.pop("id", None)
        made = []
        if status == "completed" and urls:
            for i, url in enumerate(urls, 1):
                rec = dict(base)
                rec.update({
                    "id": f"{task_id}#{i}", "task_id": task_id, "index": i, "total": len(urls),
                    "status": status, "cost": cost, "error": None, "finished_at": now(),
                    "images": [{"url": url, "file": None}],
                })
                made.append(rec)
        else:
            rec = dict(base)
            rec.update({"id": task_id, "status": status, "cost": cost, "error": error,
                        "finished_at": now(), "images": []})
            made = [rec]
        records[pos:pos] = made
        history_write(records)
        return [r["id"] for r in made]


def backfill_images(rec_id: str, urls: list) -> None:
    """后台把结果图存到本地：不拖慢任务查询，CDN 链接过期后记录里也还能看。"""
    history_patch(rec_id, images=cache_images(rec_id, urls))


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", text)


# 对账：把还挂在 pending 的记录拿去平台问一次（页面刷新/关掉后也能自愈）
_reconciling: set = set()


def reconcile_pending() -> None:
    for rec in history_read():
        if rec.get("status") != "pending":
            continue
        tid = rec.get("task_id")
        if not tid or tid in _reconciling:
            continue
        _reconciling.add(tid)
        try:
            task = api("/v1/tasks/" + tid, timeout=60)
            status = str(task.get("status", "")).lower()
            if status not in ("completed", "failed", "violation", "error"):
                # 还没完：把进度写回记录，前端卡片就能画进度条
                history_patch(tid, progress=task.get("progress"), display_status=task.get("display_status"))
            if status in ("completed", "failed", "violation", "error"):
                urls = (task.get("result_urls") or []) if status == "completed" else []
                ids = history_finalize(tid, status, urls, task.get("cost"), task.get("error_msg"))
                for rec_id, url in zip(ids, urls):
                    threading.Thread(target=backfill_images, args=(rec_id, [url]), daemon=True).start()
        except Exception:  # noqa: BLE001 - 对账失败不影响列表返回
            pass
        finally:
            _reconciling.discard(tid)


def cache_images(rec_id: str, urls: list) -> list:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for i, url in enumerate(urls, 1):
        suffix = Path(urllib.parse.urlparse(url).path).suffix or ".png"
        base = safe_name(rec_id).strip(" .-")
        if not base:      # 名字算不出来时用哈希兜底，避免写成 " .png" 这种
            base = hashlib.sha1(str(rec_id).encode()).hexdigest()[:16]
        name = f"{base}-{i}{suffix}"
        dest = HISTORY_DIR / name
        try:
            if not dest.is_file():
                dest.write_bytes(urllib.request.urlopen(url, timeout=180).read())
            out.append({"url": url, "file": name})
        except Exception:  # noqa: BLE001 - 存不下来就退回原始 URL
            out.append({"url": url, "file": None})
    return out


def history_view() -> list:
    """给前端的记录：本地缓存文件若已不在，就把 file 置空，让界面回退用 CDN 链接。"""
    records = history_read()
    for rec in records:
        for im in rec.get("images") or []:
            fname = im.get("file")
            if fname and not (HISTORY_DIR / fname).is_file():
                im["file"] = None
    return records


def reveal_in_folder(target: Path) -> None:
    """在系统文件管理器里选中该文件（Windows / macOS / Linux 各自实现）。"""
    try:
        if os.name == "nt":
            subprocess.run(["explorer", "/select,", str(target)], check=False)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", str(target)], check=False)
        else:
            subprocess.Popen(["xdg-open", str(target.parent)])
    except Exception:  # noqa: BLE001 - 打不开文件夹不该让请求失败
        pass


def local_image(rec: dict):
    """取这条记录的本地图片路径（找不到返回 None）。"""
    for im in rec.get("images") or []:
        if im.get("file"):
            target = (HISTORY_DIR / im["file"]).resolve()
            if HISTORY_DIR.resolve() in target.parents and target.is_file():
                return target
    return None


# ── 参考图上传缓存（按内容哈希）────────────────────────────────────────
UPLOAD_CACHE_FILE = DATA_HOME / "uploads.json"


def upload_cache_get(digest: str):
    try:
        return json.loads(UPLOAD_CACHE_FILE.read_text(encoding="utf-8")).get(digest)
    except Exception:  # noqa: BLE001
        return None


def upload_cache_set(digest: str, url: str) -> None:
    try:
        data = json.loads(UPLOAD_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data[digest] = url
    UPLOAD_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ── 上传进度：边往平台发边记账，前端轮询这个表 ───────────────────────
upload_progress: dict = {}
_progress_lock = threading.Lock()


class ProgressReader:
    """把 body 分块喂给 http.client，同时记录已发字节数。"""

    def __init__(self, data: bytes, state: dict):
        self._data = data
        self._pos = 0
        self._state = state
        self._len = len(data)

    def __len__(self):
        return self._len

    def read(self, size=-1):
        if size is None or size < 0:
            size = self._len - self._pos
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        with _progress_lock:
            self._state["sent"] = self._pos
            self._state["percent"] = round(self._pos * 100 / self._len) if self._len else 100
            self._state["phase"] = "waiting" if self._pos >= self._len else "uploading"
        return chunk


def progress_set(uid: str, **fields) -> None:
    with _progress_lock:
        state = upload_progress.setdefault(uid, {})
        state.update(fields)
        if len(upload_progress) > 60:                      # 只留最近 60 条
            for key in list(upload_progress)[:-60]:
                upload_progress.pop(key, None)


def platform_upload(cfg: dict, raw: bytes, ctype: str, state: dict) -> dict:
    """直接走 http.client，才能观察发送进度（urllib 会把整个过程包死）。"""
    parts = urllib.parse.urlsplit(cfg["baseURL"])
    conn_cls = HTTPSConnection if parts.scheme == "https" else HTTPConnection
    conn = conn_cls(parts.netloc, timeout=120)
    try:
        conn.request("POST", (parts.path.rstrip("/") or "") + "/v1/files/upload", body=ProgressReader(raw, state),
                     headers={"Authorization": "Bearer " + cfg["apiKey"], "Content-Type": ctype,
                              "Content-Length": str(len(raw))})
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise RuntimeError(f"平台拒绝上传 HTTP {resp.status}：{payload[:180]}")
        return json.loads(payload)
    finally:
        conn.close()


def api(path: str, payload=None, timeout: float = 300, raw: bool = False, key: str | None = None):
    cfg = config()
    if key is not None:
        cfg = dict(cfg, apiKey=key)
    url = cfg["baseURL"] + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    req.add_header("Authorization", "Bearer " + cfg["apiKey"])
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return body if raw else json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:  # noqa: BLE001
            detail = body
        if isinstance(detail, list):
            detail = "; ".join(str(d.get("msg", d)) for d in detail)
        hint = {401: "API Key 无效", 402: "余额不足", 403: "该 Key 未授权此模型", 422: "参数不合法", 429: "频率超限"}.get(exc.code, "")
        raise ApiError(exc.code, f"HTTP {exc.code} {hint}: {detail}")


def effective_model(cfg: dict | None = None) -> str:
    """当前生效的默认模型。

    只有用户**显式切换过**（配置里带 modelChosen: true）才用配置里的值；
    否则一律返回标准版 —— 这样老版本配置里残留的「增强版」会自动纠正。
    """
    cfg = cfg or config()
    if cfg.get("modelChosen") is True and cfg.get("model"):
        return str(cfg["model"])
    return MODEL_ORDER[0]


def model_spec(model_id: str, record: dict) -> dict:
    """把 /v1/models 的记录 + 型号页的精确限制，整理成前端要的规格。"""
    limits = MODEL_LIMITS.get(model_id, {})
    raw = record.get("resolutions")
    if isinstance(raw, dict):
        resolutions = sorted({r for v in raw.values() for r in (v if isinstance(v, list) else [v])})
    else:
        resolutions = list(raw or ["1K"])
    pricing = record.get("pricing")
    if not isinstance(pricing, dict):
        pricing = {resolutions[0]: pricing or 0}
    return {
        "id": model_id,
        "label": MODEL_LABELS.get(model_id, model_id),
        "pricing": pricing,
        "resolutions": limits.get("resolutions") or resolutions,
        "aspect_ratios": record.get("aspect_ratios") or RATIOS,
        "references": limits.get("references") or record.get("reference_image_limit") or 9,
        "outputs": limits.get("outputs") or record.get("max_outputs") or 1,
    }


_MODEL_CACHE_FILE = DATA_HOME / "model_cache.json"


def _load_model_cache_file():
    """上次成功拿到的规格：冷启动也能秒开表单，不用等平台。"""
    try:
        data = json.loads(_MODEL_CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data.get("models") else None
    except Exception:  # noqa: BLE001
        return None


def _save_model_cache_file(value: dict) -> None:
    try:
        _MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODEL_CACHE_FILE.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ── 授权状态（桌面版：首次填 Key，之后每次启动校验）──────────────
# status: checking 校验中 / ok 正常 / invalid 密钥无效（挡住界面）/ unreachable 平台不可达
AUTH_CACHE_FILE = DATA_HOME / "auth_cache.json"


def _key_hash(key: str) -> str:
    return hashlib.sha1((key or "").encode()).hexdigest()


def _trusted_key() -> bool:
    """这个密钥以前校验通过过吗？用于启动时先放行、后台再校验。"""
    key = config().get("apiKey") or ""
    if not key:
        return False
    try:
        data = json.loads(AUTH_CACHE_FILE.read_text(encoding="utf-8"))
        return data.get("keyHash") == _key_hash(key)
    except Exception:  # noqa: BLE001
        return False


def _mark_trusted(key: str) -> None:
    try:
        DATA_HOME.mkdir(parents=True, exist_ok=True)
        AUTH_CACHE_FILE.write_text(json.dumps({"keyHash": _key_hash(key), "at": time.time()}), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


auth_state: dict = {"status": "checking", "error": "", "checked_at": 0, "models": []}
_auth_lock = threading.Lock()


def _set_auth(**fields) -> None:
    with _auth_lock:
        auth_state.update(fields)


def get_auth() -> dict:
    with _auth_lock:
        state = dict(auth_state)
    state["has_key"] = bool(config().get("apiKey"))
    state["trusted"] = _trusted_key()      # 以前校验通过过 → 启动时可以先放行
    return state


def verify_key(key: str, timeout: float = 12) -> tuple:
    """校验密钥。返回 (status, error)。401/403 = 明确无效；超时/网络错 = 不可达。"""
    try:
        api("/v1/api-key/limits", timeout=timeout, key=key)
        return "ok", ""
    except ApiError as exc:
        if exc.code in (401, 403):
            return "invalid", "API 密钥无效，请检查后重新配置"
        if exc.code == 402:
            return "ok", ""          # 余额不足也算密钥有效，能进界面
        return "unreachable", f"平台返回 HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - 超时/断网
        return "unreachable", f"无法连接平台（{type(exc).__name__}）"


def _refresh_sync_safe() -> None:
    try:
        _refresh_sync()
    except Exception:  # noqa: BLE001
        pass


def check_auth_async() -> None:
    """后台校验，校验完把状态写回，前端轮询 /api/state 就能看到。"""
    key = config().get("apiKey") or ""
    if not key:
        _set_auth(status="invalid", error="尚未配置 API 密钥", checked_at=time.time())
        return
    _set_auth(status="checking", error="")
    status, err = verify_key(key)
    if status == "ok":
        _mark_trusted(key)
        try:
            _refresh_sync()          # 顺便把模型规格刷新+落盘
        except Exception:  # noqa: BLE001
            pass
    _set_auth(status=status, error=err, checked_at=time.time())


def _update_cached_default(model_id: str) -> None:
    """用户切了模型：把内存/磁盘缓存里的 default 一起改掉，避免下次读到的还是旧默认值。"""
    _model_cache.clear()
    try:
        data = json.loads(_MODEL_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if any(m.get("id") == model_id for m in data.get("models", [])):
        data["default"] = model_id
        try:
            _MODEL_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def catalog() -> dict:
    """授权模型 + 规格。绝不阻塞：先给内存/磁盘里的旧值，后台再刷新。"""
    import time
    now = time.time()
    cached = _model_cache.get("value")
    if cached is None:
        disk = _load_model_cache_file()
        if disk:
            _model_cache.update(value=disk, at=0)   # 立刻可用，但按过期处理
            cached = disk
    if cached is not None and now - _model_cache.get("at", 0) < 60:
        return cached
    if cached is not None:                          # 过期：先返回旧的，后台刷新
        if not _model_cache.get("refreshing"):
            _model_cache["refreshing"] = True

            def refresh():
                try:
                    _refresh_sync()
                finally:
                    _model_cache["refreshing"] = False

            threading.Thread(target=refresh, daemon=True).start()
        return cached
    return _refresh_sync()                          # 头一次、又没磁盘缓存：只能等


def _refresh_sync() -> dict:
    """真正去平台拉一次（可能慢），成功就落盘。"""
    fallback_models = [
        model_spec("gpt-image-2.5", {"pricing": {"1K": 0.0081},
                                     "aspect_ratios": RATIOS, "resolutions": ["1K"],
                                     "reference_image_limit": 9, "max_outputs": 1}),
        model_spec(FALLBACK_MODEL, {"pricing": {"1K": 0.0196, "2K": 0.0229, "4K": 0.0263},
                                    "aspect_ratios": ["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "5:4", "4:5", "21:9"],
                                    "resolutions": ["1K", "2K", "4K"], "reference_image_limit": 16, "max_outputs": 10}),
    ]
    try:
        limits = api("/v1/api-key/limits", timeout=12)
        allowed = limits.get("available_models") or []
        records = {m["id"]: m for m in api("/v1/models", timeout=12).get("data", [])}
    except Exception as exc:  # noqa: BLE001 - 断网/超时也要能用表单
        value = {"models": fallback_models, "default": effective_model() if any(m["id"] == effective_model() for m in fallback_models) else fallback_models[0]["id"],
                 "warning": f"接口暂不可达（{exc}），显示的是上次已知配置"}
        _model_cache.update(at=time.time() - 30, value=value)  # 半分钟后重试
        return value
    order = [m for m in MODEL_ORDER if m in allowed] + [m for m in allowed if m not in MODEL_ORDER]
    specs = [model_spec(mid, records.get(mid, {})) for mid in order] or fallback_models
    configured = effective_model()
    value = {"models": specs, "default": configured if any(s["id"] == configured for s in specs) else specs[0]["id"]}
    _model_cache.update(at=time.time(), value=value)
    _save_model_cache_file(value)      # 落盘：下次冷启动秒开
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "maizi-form/1.0"

    def log_message(self, fmt, *args):  # 安静一点：只记非 API 请求
        try:
            if "/api/" not in (args[0] if args else ""):
                log("HTTP " + fmt % args)
        except Exception:  # noqa: BLE001
            pass

    # ── helpers ──────────────────────────────────────────────
    def send_json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    # ── routes ───────────────────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                html = resource_path("index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                # 改样式后刷新就能看到，不要走浏览器缓存
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.end_headers()
                self.wfile.write(html)
            elif path == "/api/state":
                data = catalog()
                data["auth"] = get_auth()
                self.send_json(data)
            elif path == "/api/task":
                tid = query.get("id", [""])[0]
                task = api("/v1/tasks/" + tid, timeout=60)
                status = str(task.get("status", "")).lower()
                if status in ("completed", "failed", "violation", "error"):
                    rec = next((r for r in history_read() if r.get("task_id") == tid), None)
                    if rec is None or rec.get("status") == "pending":
                        urls = (task.get("result_urls") or []) if status == "completed" else []
                        ids = history_finalize(tid, status, urls, task.get("cost"), task.get("error_msg"))
                        for rec_id, url in zip(ids, urls):
                            threading.Thread(target=backfill_images, args=(rec_id, [url]), daemon=True).start()
                self.send_json(task)
            elif path == "/api/history":
                # 列表立刻返回；有 pending 就后台去平台对账，下次刷新就能看到结果
                threading.Thread(target=reconcile_pending, daemon=True).start()
                self.send_json({"records": history_view()})
            elif path == "/api/upload-progress":
                with _progress_lock:
                    state = dict(upload_progress.get(query.get("id", [""])[0], {"phase": "unknown"}))
                self.send_json(state)
            elif path == "/api/image":
                fname = query.get("f", [""])[0]
                url = query.get("u", [""])[0]
                if fname:
                    target = (HISTORY_DIR / fname).resolve()
                    if HISTORY_DIR.resolve() not in target.parents or not target.is_file():
                        raise RuntimeError("本地图片不存在")
                    blob = target.read_bytes()
                    ctype = mimetypes.guess_type(fname)[0] or "image/png"
                else:
                    if not url.startswith(("http://", "https://")):
                        raise RuntimeError("非法图片地址")
                    blob = urllib.request.urlopen(url, timeout=120).read()
                    ctype = mimetypes.guess_type(url)[0] or "image/png"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                if query.get("dl"):
                    name = fname or Path(urllib.parse.urlparse(url).path).name
                    self.send_header("Content-Disposition", 'attachment; filename="' + name + '"')
                self.end_headers()
                self.wfile.write(blob)
            elif path == "/api/health":
                self.send_json({"ok": True, "app": "maizi-desktop", "data_home": str(DATA_HOME),
                                "config": [str(p) for p in CONFIG_PATHS]})
            else:
                self.send_json({"error": "not found"}, 404)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/generate":
                payload = json.loads(self.body() or b"{}")
                if not str(payload.get("prompt", "")).strip():
                    raise RuntimeError("提示词不能为空")
                # 模型由分段控制器指定；只接受当前 Key 授权过的，其余落到配置默认值
                allowed = {s["id"] for s in catalog()["models"]}
                chosen = str(payload.get("model") or "")
                payload["model"] = chosen if chosen in allowed else effective_model()
                if not payload.get("images"):
                    payload.pop("images", None)
                # 中文标签 → 接口取值；"自动" 表示不指定，直接不发这个字段
                bg = BG_MAP.get(str(payload.get("background", "")))
                if bg:
                    payload["background"] = bg
                else:
                    payload.pop("background", None)
                fmt = FMT_MAP.get(str(payload.get("output_format", "")))
                if fmt:
                    payload["output_format"] = fmt
                else:
                    payload.pop("output_format", None)
                result = api("/v1/images/generations", payload)
                task_id = ((result.get("data") or [{}])[0]).get("task_id")
                if task_id:  # 先记一条「生成中」，完成后由 /api/task 补全并缓存图片
                    history_add({
                        "id": task_id, "task_id": task_id, "status": "pending", "prompt": payload.get("prompt"),
                        "model": payload.get("model"), "size": payload.get("size"),
                        "resolution": payload.get("resolution"), "background": payload.get("background"),
                        "output_format": payload.get("output_format"), "n": payload.get("n"),
                        "refs": len(payload.get("images") or []),
                        "cost": (result.get("usage") or {}).get("total_cost"),
                        "images": [], "created_at": now(),
                    })
                self.send_json(result)
            elif path == "/api/reveal":
                rec_id = str(json.loads(self.body() or b"{}").get("id", ""))
                rec = next((r for r in history_read() if r.get("id") == rec_id or r.get("task_id") == rec_id), None)
                target = local_image(rec) if rec else None
                if target is None:
                    raise RuntimeError("本地还没有这张图（可能仍在下载，或当初没存下来）")
                reveal_in_folder(target)
                self.send_json({"ok": True, "path": str(target)})
            elif path == "/api/settings":
                body = json.loads(self.body() or b"{}")
                new_key = str(body.get("apiKey") or "").strip()
                if not new_key:
                    raise RuntimeError("请填写 API 密钥")
                status, err = verify_key(new_key)
                if status == "invalid":
                    self.send_json({"ok": False, "error": err})
                    return
                # 不可达时也保存（用户可能只是网络抖），但明确告诉前端状态
                save_api_key(new_key)
                _model_cache.clear()
                if status == "ok":
                    _mark_trusted(new_key)
                    _set_auth(status="ok", error="", checked_at=time.time())
                    threading.Thread(target=lambda: (_refresh_sync_safe()), daemon=True).start()
                else:
                    _set_auth(status="unreachable", error=err, checked_at=time.time())
                self.send_json({"ok": True, "status": status, "error": err})
            elif path == "/api/verify":
                threading.Thread(target=check_auth_async, daemon=True).start()
                self.send_json({"ok": True})
            elif path == "/api/quit":
                self.send_json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.3), server_ref[0] and server_ref[0].shutdown()), daemon=True).start()
            elif path == "/api/model":
                chosen = str(json.loads(self.body() or b"{}").get("model", ""))
                save_model(chosen)
                _update_cached_default(chosen)     # 缓存里的 default 一起改，下次读到就是新值
                self.send_json({"ok": True})
            elif path == "/api/upload":
                cfg = config()
                raw = self.body()
                digest = self.headers.get("X-Content-SHA256") or hashlib.sha256(raw).hexdigest()
                uid = self.headers.get("X-Upload-Id") or digest[:16]
                hit = upload_cache_get(digest)      # 同一张图不再重复上传
                if hit:
                    progress_set(uid, phase="done", percent=100, sent=len(raw), total=len(raw), cached=True)
                    self.send_json({"url": hit, "cached": True})
                    return
                progress_set(uid, phase="connecting", percent=0, sent=0, total=len(raw), cached=False, error=None)
                try:
                    out = platform_upload(cfg, raw, self.headers.get("Content-Type", ""), upload_progress[uid])
                except Exception as exc:  # noqa: BLE001 - 超时/网络错误给个人话
                    progress_set(uid, phase="error", error=str(exc))
                    raise RuntimeError(f"{exc}（平台上传接口偶发拥堵，稍后重试即可）") from None
                progress_set(uid, phase="done", percent=100)
                upload_cache_set(digest, out["url"])
                self.send_json({"url": out.get("url"), "cached": False})
            else:
                self.send_json({"error": "not found"}, 404)
        except RuntimeError as exc:
            self.send_json({"error": str(exc)}, 400)
        except urllib.error.HTTPError as exc:
            self.send_json({"error": f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}"}, 400)
        except Exception as exc:  # noqa: BLE001
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def pick_port(preferred: int) -> int:
    """优先用 8765，被占了就让系统分配一个空闲端口。"""
    for candidate in (preferred, 0):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", candidate))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    return preferred


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
            return b'"maizi-desktop"' in resp.read()
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    args = [a for a in sys.argv[1:]]
    open_browser = "--no-browser" not in args
    args = [a for a in args if not a.startswith("--")]
    preferred = int(args[0]) if args else 8765

    if already_running(preferred):            # 双击第二次：直接把页面打开
        log("已有实例在运行，打开页面")
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{preferred}")
        return 0

    port = pick_port(preferred)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        log(f"端口 {port} 起不来：{exc}")
        return 1
    server_ref[0] = srv

    url = f"http://127.0.0.1:{port}"
    log(f"数据目录 {DATA_HOME}")
    log(f"共济生图已启动：{url}")
    threading.Thread(target=check_auth_async, daemon=True).start()      # 启动即校验密钥

    if open_browser:
        threading.Thread(target=lambda: (time.sleep(1.0), webbrowser.open(url)), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
