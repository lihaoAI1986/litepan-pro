"""/d/{account_id}/{file_path} 302 反代入口（TgtoDrive 风格）。

调用链：Emby/Infuse 等播放器读取 STRM -> 请求本入口 ->
本入口解析路径 -> 查内存缓存 / db.path_file_cache / 驱动 list_files 拿到 file_id ->
走 resolve_download 拿直链 -> 302 跳到网盘 CDN。

触发同目录预缓存：解析完成后异步调 list_sibling_paths_for_precache 给同目录
其它文件预热直链，首播延迟几乎为零。
"""

import asyncio
import time
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from config import config_manager
from core.driver_service import (
    get_account_driver_instance,
    list_sibling_paths_for_precache,
    resolve_path_to_file,
)

try:
    from core.log_manager import get_writer, LogModule
    _strm_play_logger = get_writer(LogModule.WEB)
except Exception:
    _strm_play_logger = None


def _log(level: str, msg: str) -> None:
    if _strm_play_logger is None:
        return
    try:
        getattr(_strm_play_logger, level)(msg)
    except Exception:
        pass
from core.error_handler import raise_api_error, raise_not_found
from core.path_download_cache import (
    CachedDownload,
    get_path_download_cache,
)
from core.strm_security import (
    extract_filename_from_remote_path,
    normalize_strm_remote_path,
)


router = APIRouter()


# ---- 配置读取 ----

async def _get_path_cache_enabled() -> bool:
    from core.utils import normalize_bool
    value = await config_manager.get_async("strm_path_cache_enabled")
    return normalize_bool(value, True)


async def _get_precache_enabled() -> bool:
    from core.utils import normalize_bool
    value = await config_manager.get_async("strm_path_precache_enabled")
    return normalize_bool(value, True)


async def _get_max_age_seconds() -> int:
    """302 Location 的 Cache-Control: max-age，让播放器/CDN 缓存 302 自身。"""
    value = await config_manager.get_async("strm_path_redirect_max_age")
    try:
        return max(0, int(value or 300))
    except Exception:
        return 300


# ---- 入口 ----

@router.api_route("/d/{account_id}/{file_path:path}", methods=["GET", "HEAD"])
async def play_by_path(account_id: int, file_path: str, request: Request):
    """/d/{account_id}/{file_path} -> 302 跳到云盘 CDN 直链。"""
    remote_path = normalize_strm_remote_path("/" + file_path if not file_path.startswith("/") else file_path)
    if not remote_path or remote_path == "/":
        _log("warning", f"[/d/] bad path: account_id={account_id} file_path={file_path!r}")
        return Response(status_code=404, content="Bad path")

    filename = extract_filename_from_remote_path(remote_path)
    if not filename:
        _log("warning", f"[/d/] no filename: account_id={account_id} remote_path={remote_path!r}")
        return Response(status_code=404, content="Bad path")

    user_agent = request.headers.get("User-Agent", "") or ""
    _log("info", f"[/d/] request: account_id={account_id} remote_path={remote_path!r} ua={user_agent[:50]}")

    cache = get_path_download_cache()
    cache_enabled = await _get_path_cache_enabled()

    # 1. 命中内存缓存就直接跳
    if cache_enabled:
        cached = cache.get(account_id, remote_path)
        if cached is not None and cached.download_url:
            _log("info", f"[/d/] cache hit: {remote_path}")
            return _build_redirect(cached.download_url, await _get_max_age_seconds())

    # 2. 否则解析路径
    try:
        driver = await get_account_driver_instance(account_id)
    except Exception as e:
        _log("error", f"[/d/] get_driver failed: account_id={account_id} err={e}")
        return _render_error(e)

    async def fetcher(_acct_id: int, _remote_path: str) -> Optional[CachedDownload]:
        try:
            resolved = await resolve_path_to_file(driver, _acct_id, _remote_path, user_agent)
        except Exception as e:
            _log("error", f"[/d/] resolve_path_to_file 异常: {_remote_path!r} err={e}")
            return None
        if resolved is None:
            _log("warning", f"[/d/] resolve_path_to_file 返回 None: {_remote_path!r} (可能是路径不同或驱动拒绝)")
            return None
        if not resolved.download_url:
            _log("warning", f"[/d/] download_url 为空: file_id={resolved.file_id} file_name={resolved.file_name}")
            return None
        return CachedDownload(
            file_id=resolved.file_id,
            file_name=resolved.file_name or "",
            file_size=int(resolved.file_size or 0),
            download_url=resolved.download_url,
            headers=resolved.headers,
            ts=time.monotonic(),
        )

    if cache_enabled:
        cached = await cache.get_or_fetch(account_id, remote_path, fetcher)
        if cached is None or not cached.download_url:
            _log("warning", f"[/d/] 未找到文件: {remote_path!r} account_id={account_id}")
            return Response(status_code=404, content="Not found")
        download_url = cached.download_url
        _log("info", f"[/d/] 跳转: {remote_path!r} -> {download_url[:80]}...")
    else:
        result = await fetcher(account_id, remote_path)
        if result is None or not result.download_url:
            _log("warning", f"[/d/] 未找到文件(无缓存): {remote_path!r}")
            return Response(status_code=404, content="Not found")
        download_url = result.download_url
        _log("info", f"[/d/] 跳转(无缓存): {remote_path!r} -> {download_url[:80]}...")

    # 3. 同目录预缓存（异步后台）
    if cache_enabled and await _get_precache_enabled():
        directory_remote_path = _parent_dir(remote_path)
        if directory_remote_path and directory_remote_path != "/":
            try:
                async def _siblings_provider(_acct_id: int, _dir_path: str):
                    try:
                        return await list_sibling_paths_for_precache(driver, _acct_id, _dir_path)
                    except Exception:
                        return []
                asyncio.create_task(
                    cache.precache_directory(
                        account_id,
                        directory_remote_path,
                        siblings_provider=_siblings_provider,
                    )
                )
            except Exception:
                # 预缓存失败不影响主链路
                pass

    return _build_redirect(download_url, await _get_max_age_seconds())


def _build_redirect(location: str, max_age_seconds: int) -> Response:
    """构造 302 重定向响应，附带适当的 Cache-Control。"""
    if not location:
        return Response(status_code=404, content="Not found")
    headers = {"Location": location}
    if max_age_seconds > 0:
        headers["Cache-Control"] = f"public, max-age={max_age_seconds}"
    return Response(status_code=302, headers=headers)


def _parent_dir(remote_path: str) -> str:
    norm = normalize_strm_remote_path(remote_path)
    if not norm or norm == "/":
        return "/"
    parent = norm.rsplit("/", 1)[0]
    return parent or "/"


def _render_error(e: Exception) -> Response:
    try:
        from core.error_handler import handle_api_exception
        return handle_api_exception(e)
    except Exception:
        pass
    try:
        if hasattr(e, "error_type"):
            raise e
        import traceback
        from core.log_manager import get_writer, LogModule
        try:
            get_writer(LogModule.WEB).error(f"/d/ 播放失败: {e}\\n{traceback.format_exc()}")
        except Exception:
            print(f"/d/ 播放失败: {e}")
    except Exception:
        pass
    return Response(status_code=500, content=f"Internal error: {str(e)}")

# ---------- 诊断端点 ----------

@router.api_route("/_debug/d/{account_id}/{file_path:path}", methods=["GET"])
async def debug_path_lookup(account_id: int, file_path: str, request: Request):
    """诊断 /d/ 路径查找过程：仅帮助调试，不跳转。

    GET /_debug/d/{account_id}/{file_path}

    返回 JSON：

    {

        "remote_path": "...",

        "filename": "...",

        "cache_db_hit": true/false,

        "cache_db_row": {...} | null,

        "cache_memory_hit": true/false,

        "driver_root": "...",

        "walk": {...} | null,    # 每层的浏览进度

        "error": null | "..."

    }
    """
    from database.db import db as _db
    import traceback as _tb

    norm = normalize_strm_remote_path("/" + file_path if not file_path.startswith("/") else file_path)
    filename = extract_filename_from_remote_path(norm) if norm and norm != "/" else ""

    result = {
        "remote_path": norm,
        "filename": filename,
        "account_id": account_id,
        "cache_db_hit": False,
        "cache_db_row": None,
        "cache_memory_hit": False,
        "driver_root": None,
        "walk": None,
        "error": None,
    }

    # 1. db.path_file_cache
    try:
        row = await _db.get_path_file_cache(int(account_id), norm)
        if row:
            result["cache_db_hit"] = True
            row_dict = dict(row)
            for k in list(row_dict.keys()):
                if k == "last_seen_at":
                    row_dict[k] = str(row_dict[k])
            result["cache_db_row"] = row_dict
    except Exception as e:
        result["error"] = f"db lookup: {e}"

    # 2. memory cache
    try:
        cache = get_path_download_cache()
        cached = cache.get(int(account_id), norm)
        if cached is not None:
            result["cache_memory_hit"] = True
    except Exception as e:
        result["error"] = (result["error"] or "") + f" | memory: {e}"

    # 3. driver root + walk
    try:
        from core.driver_service import get_account_driver_instance
        driver = await get_account_driver_instance(int(account_id))
        result["driver_root"] = _get_driver_root_folder_id_local(driver)

        # 试走前两层（避免太慢）
        walk_trace = []
        segments = [s for s in norm.split("/") if s]
        if segments:
            cur = result["driver_root"]
            for i, seg in enumerate(segments[:-1]):
                if not cur:
                    break
                try:
                    children = await driver.list_files(cur)
                    matched = None
                    sample_names = []
                    for item in children[:50]:
                        sample_names.append({
                            "name": getattr(item, "name", ""),
                            "is_dir": getattr(item, "is_dir", False),
                            "id": str(getattr(item, "id", "")),
                        })
                        if matched is None and getattr(item, "is_dir", False) and _name_match_for_walk(seg, getattr(item, "name", "")):
                            matched = {"name": getattr(item, "name", ""), "id": str(getattr(item, "id", ""))}
                    walk_trace.append({
                        "level": i,
                        "segment": seg,
                        "parent_id": cur,
                        "matched": matched,
                        "total_children": len(children),
                        "sample_names": sample_names[:10],
                    })
                    if matched:
                        cur = matched["id"]
                    else:
                        break
                except Exception as e:
                    walk_trace.append({"level": i, "segment": seg, "parent_id": cur, "error": str(e)})
                    break
            result["walk"] = walk_trace
    except Exception as e:
        result["error"] = (result["error"] or "") + f" | driver: {e}\n{_tb.format_exc()}"

    import json as _json
    return Response(
        status_code=200,
        content=_json.dumps(result, ensure_ascii=False, indent=2),
        media_type="application/json",
    )


def _get_driver_root_folder_id_local(driver):
    config = getattr(driver, "config", None)
    if config is None:
        return None
    for attr in ("root_folder_id", "default_cid", "root_cid", "root_id"):
        value = getattr(config, attr, None)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _name_match_for_walk(candidate, actual):
    """与 core.driver_service 中同名函数的复制，避免引用问题。"""
    if not candidate or not actual:
        return False

    def _variants(s):
        out = [s]
        try:
            decoded = urllib.parse.unquote(s)
            if decoded and decoded not in out:
                out.append(decoded)
        except Exception:
            pass
        cleaned = s.strip().rstrip(chr(0)).strip(chr(96)).strip(chr(34)).strip(chr(39)).strip()
        if cleaned and cleaned not in out:
            out.append(cleaned)
        return out

    for cand in _variants(candidate):
        if cand == actual:
            return True
    for cand in _variants(actual):
        if cand == candidate:
            return True
    return False
