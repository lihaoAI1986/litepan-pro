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
        return Response(status_code=404, content="Bad path")

    filename = extract_filename_from_remote_path(remote_path)
    if not filename:
        return Response(status_code=404, content="Bad path")

    user_agent = request.headers.get("User-Agent", "") or ""

    cache = get_path_download_cache()
    cache_enabled = await _get_path_cache_enabled()

    # 1. 命中内存缓存就直接跳
    if cache_enabled:
        cached = cache.get(account_id, remote_path)
        if cached is not None and cached.download_url:
            return _build_redirect(cached.download_url, await _get_max_age_seconds())

    # 2. 否则解析路径
    try:
        driver = await get_account_driver_instance(account_id)
    except Exception as e:
        return _render_error(e)

    async def fetcher(_acct_id: int, _remote_path: str) -> Optional[CachedDownload]:
        try:
            resolved = await resolve_path_to_file(driver, _acct_id, _remote_path, user_agent)
        except Exception:
            return None
        if resolved is None or not resolved.download_url:
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
            return Response(status_code=404, content="Not found")
        download_url = cached.download_url
    else:
        result = await fetcher(account_id, remote_path)
        if result is None or not result.download_url:
            return Response(status_code=404, content="Not found")
        download_url = result.download_url

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
