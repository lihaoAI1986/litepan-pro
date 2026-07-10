"""驱动获取与下载公共服务。"""

import json
import asyncio
from time import monotonic
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.strm_security import (
    extract_filename_from_remote_path,
    normalize_strm_remote_path,
)

from core.base import FileItem
from core.error_handler import raise_api_error, raise_not_found
from database.db import db

from .account_utils import ensure_account_active, filter_runtime_config, get_account_or_404
from .operation_wrapper import ensure_driver_auth_request_allowed
from .registry import driver_registry


_download_cache: Dict[str, Dict[str, Any]] = {}
_download_locks: Dict[str, asyncio.Lock] = {}

def _get_download_lock(key: str) -> asyncio.Lock:
    if key not in _download_locks:
        _download_locks[key] = asyncio.Lock()
    return _download_locks[key]


async def get_account_driver(account_id: int, db_session=None):
    if db_session is None:
        account = await get_account_or_404(account_id)
    else:
        cursor = await db_session.execute("SELECT * FROM cloud_accounts WHERE id = ?", (account_id,))
        row = await cursor.fetchone()
        if not row:
            raise_not_found("账号")
        account = dict(row)
        account["config"] = json.loads(account.get("config", "{}"))

    ensure_account_active(account)
    return await get_account_driver_instance(account_id, account=account, require_active=False)


async def get_account_driver_instance(
    account_id: int,
    *,
    account: Optional[Dict] = None,
    require_active: bool = True,
    existing_driver_name: Optional[str] = None,
    config_override: Optional[Dict] = None,
):
    account = account or await get_account_or_404(account_id)
    if require_active:
        ensure_account_active(account)

    try:
        return await driver_registry.get_driver_instance(
            account_id=str(account_id),
            driver_name=existing_driver_name or account["driver_type"],
            config=filter_runtime_config(config_override or account["config"]),
        )
    except ValueError as e:
        raise_api_error(str(e), "get_driver_instance", 400)


@dataclass
class ResolvedDownload:
    download_url: str
    file_name: str
    file_size: int
    file_info: Optional[FileItem] = None
    headers: Optional[Dict[str, str]] = None
    effective_mode: Optional[str] = None


async def resolve_download(
    driver,
    file_id: str,
    user_agent: str = "",
    *,
    file_info: Optional[FileItem] = None,
    force_refresh: bool = False,
) -> ResolvedDownload:
    """解析下载地址 + 文件元数据。ttl=0 也会强制给 1s 的窗口防瞬时并发击穿。

    force_refresh=True 时跳过缓存读取（含 1s 兜底窗口），强制向上游重取直链并刷新缓存，
    用于上一条直链已过期/异常需要换一条新链的补救场景。
    """
    config = getattr(driver, "config", None)
    ttl = getattr(config, "download_url_ttl", 0)

    driver_name = getattr(driver, 'name', getattr(driver.__class__, '__name__', 'unknown'))
    cache_key = f"{driver_name}:{file_id}:{user_agent}"

    now = monotonic()

    # 无锁快读：只有驱动明确声明 ttl>0 才走（force_refresh 直接跳过）
    if ttl > 0 and not force_refresh:
        cached = _download_cache.get(cache_key)
        if cached and now - cached["time"] < ttl:
            return cached["result"]

    lock = _get_download_lock(cache_key)
    async with lock:
        # 双重检查；即使驱动 ttl=0，也给 1s 的兜底窗口，避免同一时刻多个请求都去真请求上游
        now = monotonic()
        cached = _download_cache.get(cache_key)
        effective_ttl = max(ttl, 1)
        if not force_refresh and cached and now - cached["time"] < effective_ttl:
            return cached["result"]

        await ensure_driver_auth_request_allowed(driver)

        if hasattr(driver, "get_download_info"):
            download_info = await driver.get_download_info(file_id, user_agent)
            raw_headers = download_info.get("headers")
            effective_mode = (
                download_info.get("effective_mode")
                or download_info.get("download_mode")
            )
            result = ResolvedDownload(
                download_url=download_info["download_url"],
                file_name=download_info.get("file_name") or f"file_{file_id}",
                file_size=int(download_info.get("size") or 0),
                file_info=file_info,
                headers=dict(raw_headers) if raw_headers is not None else None,
                effective_mode=str(effective_mode).strip() if effective_mode else None,
            )
        else:
            actual_file_info = file_info or await driver.file_info(file_id)
            if not actual_file_info:
                raise_not_found("文件")

            result = ResolvedDownload(
                download_url=await driver.get_download_url(file_id, user_agent),
                file_name=actual_file_info.name or f"file_{file_id}",
                file_size=int(actual_file_info.size or 0),
                file_info=actual_file_info,
            )

        _download_cache[cache_key] = {"time": monotonic(), "result": result}

        # 锁表爆炸兜底：简单整体清掉，保留当前 key
        if len(_download_locks) > 1000:
            _download_locks.clear()
            _download_cache.clear()
            _download_locks[cache_key] = lock
            _download_cache[cache_key] = {"time": monotonic(), "result": result}

        return result


def get_driver_download_mode(driver, default: str = "redirect") -> str:
    config = getattr(driver, "config", None)
    return getattr(config, "download_mode", default) or default


def get_effective_download_mode(
    driver,
    download: Optional[ResolvedDownload] = None,
    default: str = "redirect",
) -> str:
    """读取本次下载真正应该使用的模式。

    个别驱动会先按配置尝试 redirect，但上游不一定每次都能给匿名直链。
    此时驱动可在 get_download_info 返回 effective_mode="proxy"，公共层按
    本次结果处理，避免把需要鉴权的上游地址错误 302 给客户端。
    """
    mode = getattr(download, "effective_mode", None) if download else None
    if mode:
        return str(mode).strip() or default
    return get_driver_download_mode(driver, default)


def get_driver_proxy_part_size(driver) -> int:
    """读取驱动声明的「向上游 CDN 单片请求字节数」。

    驱动可在自己的 Config 上声明：
        proxy_part_size: int = 10 * 1024 * 1024  # 例：夸克

    返回 0 表示驱动未声明，由 range_proxy 用全局默认值（DEFAULT_PROXY_PART_SIZE）兜底。
    """
    config = getattr(driver, "config", None)
    try:
        return int(getattr(config, "proxy_part_size", 0) or 0)
    except (TypeError, ValueError):
        return 0


async def build_upstream_download_headers(
    driver,
    file_id: str,
    user_agent: str = "",
    *,
    range_header: Optional[str] = None,
    prefer_identity: bool = False,
    download: Optional[ResolvedDownload] = None,
    headers_override: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    if headers_override is not None:
        headers = dict(headers_override)
    elif download is not None and download.headers is not None:
        headers = dict(download.headers)
    else:
        headers = {}

    has_explicit_headers = headers_override is not None or (
        download is not None and download.headers is not None
    )
    if not headers and not has_explicit_headers and hasattr(driver, "get_download_headers"):
        await ensure_driver_auth_request_allowed(driver)
        headers = await driver.get_download_headers(file_id, user_agent)

    headers = dict(headers or {})
    if user_agent and "User-Agent" not in headers:
        headers["User-Agent"] = user_agent

    headers.setdefault("Accept", "*/*")
    headers.setdefault("Connection", "keep-alive")

    if prefer_identity:
        headers.setdefault("Accept-Encoding", "identity")

    if range_header:
        headers["Range"] = range_header

    return headers


# ---- v3 path-based 解析 ----

@dataclass
class PathResolution:
    file_id: str
    file_name: str
    file_size: int
    pick_code: Optional[str] = None
    parent_id: Optional[str] = None
    download_url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None


async def resolve_path_to_file(
    driver,
    account_id: int,
    remote_path: str,
    user_agent: str = "",
) -> Optional[PathResolution]:
    """把 /Movies/碟中谍4.mkv 这种远端路径解析成 PathResolution。

    流程：
    1. 先查 db.path_file_cache（STRM 同步时持久化下来的）；
    2. 命中则直接 file_id + pick_code，再走 resolve_download 拿直链；
    3. 未命中则按路径逐级 list_files 定位（仅限当前驱动 root_folder_id 之下）；
       找到后回填到 db.path_file_cache。

    注意：仅支持当前驱动；其它驱动通过 fallback_resolve_path 返回 None 让上层降级。
    """
    from database.db import db

    norm = normalize_strm_remote_path(remote_path)
    if not norm or norm == "/":
        return None
    filename = extract_filename_from_remote_path(norm)
    if not filename:
        return None

    # 1. 持久化缓存
    cached = await db.get_path_file_cache(account_id, norm)
    if cached and cached.get("file_id"):
        pick_code = cached.get("pick_code") or ""
        file_id = str(cached.get("file_id"))
        file_name = str(cached.get("file_name") or filename)
        file_size = int(cached.get("file_size") or 0)
        parent_id = cached.get("parent_id")
        return await _build_path_resolution_from_pick(
            driver,
            account_id=account_id,
            file_id=file_id,
            pick_code=pick_code,
            file_name=file_name,
            file_size=file_size,
            parent_id=parent_id,
            user_agent=user_agent,
            cache_refresh_path=norm,
        )

    # 2. 未命中 → 驱动层按目录逐级查找
    resolved = await _walk_path_in_driver(
        driver,
        account_id=account_id,
        remote_path=norm,
        user_agent=user_agent,
    )
    if resolved is not None:
        try:
            await db.upsert_path_file_cache(
                account_id=account_id,
                remote_path=norm,
                file_id=resolved.file_id,
                pick_code=resolved.pick_code,
                file_name=resolved.file_name,
                file_size=resolved.file_size,
                parent_id=resolved.parent_id,
            )
        except Exception:
            pass
    return resolved


async def _build_path_resolution_from_pick(
    driver,
    *,
    account_id: int,
    file_id: str,
    pick_code: str,
    file_name: str,
    file_size: int,
    parent_id,
    user_agent: str,
    cache_refresh_path: str = "",
) -> Optional[PathResolution]:
    """用已知 pick_code / file_id 直接拿直链。"""
    if not file_id:
        return None
    try:
        download = await resolve_download(driver, file_id, user_agent)
    except Exception:
        return None
    if not download or not download.download_url:
        return None
    return PathResolution(
        file_id=str(file_id),
        file_name=file_name or download.file_name or "",
        file_size=int(file_size or download.file_size or 0),
        pick_code=pick_code or None,
        parent_id=str(parent_id) if parent_id else None,
        download_url=download.download_url,
        headers=download.headers,
    )


async def _walk_path_in_driver(
    driver,
    *,
    account_id: int,
    remote_path: str,
    user_agent: str,
) -> Optional[PathResolution]:
    """按路径逐级下钻：从 driver.config.root_folder_id 开始，逐层 list_files 匹配目录名，
    最终找到目标文件名对应的 file_id。

    为避免误匹配：
    - 文件名（叶子节点）必须 ext 一致且 size>0；
    - 目录路径各段必须在 list_files 里唯一匹配（同名目录有多个会取第一个并记 warn）；
    - 单层 list 最多取 300 条；如目标目录不在前 300 条则记 warn 返回 None。
    """
    segments = [seg for seg in remote_path.split("/") if seg]
    if not segments:
        return None

    root_id = _get_driver_root_folder_id(driver)
    if root_id is None:
        return None

    current_parent_id = str(root_id)
    parent_path_so_far = ""

    # 逐级下钻目录
    for index, segment in enumerate(segments[:-1]):
        target_name = segment
        parent_path_so_far = (parent_path_so_far + "/" + target_name) if parent_path_so_far else "/" + target_name
        try:
            children = await driver.list_files(current_parent_id)
        except Exception:
            return None
        match = None
        for item in children:
            if getattr(item, "is_dir", False) and item.name == target_name:
                match = item
                break
        if match is None:
            return None
        current_parent_id = str(match.id)
        parent_path_so_far = normalize_strm_remote_path(parent_path_so_far)

    # 最后一段：找文件
    target_filename = segments[-1]
    try:
        children = await driver.list_files(current_parent_id)
    except Exception:
        return None
    file_match = None
    for item in children:
        if getattr(item, "is_dir", False):
            continue
        if item.name != target_filename:
            continue
        if file_match is None or int(getattr(item, "size", 0) or 0) > int(getattr(file_match, "size", 0) or 0):
            file_match = item

    if file_match is None:
        return None

    file_id = str(file_match.id)
    pick_code = ""
    try:
        extra = getattr(file_match, "extra", None) or {}
        pick_code = str(extra.get("pick_code") or "") if isinstance(extra, dict) else ""
    except Exception:
        pick_code = ""

    return await _build_path_resolution_from_pick(
        driver,
        account_id=account_id,
        file_id=file_id,
        pick_code=pick_code,
        file_name=file_match.name,
        file_size=int(getattr(file_match, "size", 0) or 0),
        parent_id=current_parent_id,
        user_agent=user_agent,
        cache_refresh_path=remote_path,
    )


def _get_driver_root_folder_id(driver) -> Optional[str]:
    config = getattr(driver, "config", None)
    if config is None:
        return None
    for attr in ("root_folder_id", "default_cid", "root_cid", "root_id"):
        value = getattr(config, attr, None)
        if value is not None and str(value).strip():
            return str(value)
    return None


async def list_sibling_paths_for_precache(
    driver,
    account_id: int,
    directory_remote_path: str,
) -> List[Dict[str, Any]]:
    """返回 directory_remote_path 下所有文件，供 PathDownloadCache.precache_directory 使用。

    每项形如：
        {
            "remote_path": "/Movies/碟中谍4.mkv",
            "fetcher":     async callable(account_id, remote_path) -> CachedDownload or None
        }
    """
    norm = normalize_strm_remote_path(directory_remote_path)
    if not norm or norm == "/":
        return []

    segments = [seg for seg in norm.split("/") if seg]
    root_id = _get_driver_root_folder_id(driver)
    if root_id is None:
        return []

    current_parent_id = str(root_id)
    for segment in segments:
        try:
            children = await driver.list_files(current_parent_id)
        except Exception:
            return []
        match = None
        for item in children:
            if getattr(item, "is_dir", False) and item.name == segment:
                match = item
                break
        if match is None:
            return []
        current_parent_id = str(match.id)

    try:
        children = await driver.list_files(current_parent_id)
    except Exception:
        return []

    sibling_remote_prefix = norm
    siblings: List[Dict[str, Any]] = []
    for item in children:
        if getattr(item, "is_dir", False):
            continue
        remote = sibling_remote_prefix + "/" + item.name
        siblings.append({
            "remote_path": normalize_strm_remote_path(remote),
            "fetcher": _make_sibling_fetcher(driver, account_id, item),
        })
    return siblings


def _make_sibling_fetcher(driver, account_id: int, file_item) -> Callable[[int, str], Awaitable[Optional[Any]]]:
    file_id = str(getattr(file_item, "id", "") or "")
    pick_code = ""
    extra = getattr(file_item, "extra", None) or {}
    if isinstance(extra, dict):
        pick_code = str(extra.get("pick_code") or "")
    file_name = getattr(file_item, "name", "") or ""
    file_size = int(getattr(file_item, "size", 0) or 0)

    async def _fetcher(_account_id: int, _remote_path: str):
        try:
            download = await resolve_download(driver, file_id, "")
        except Exception:
            return None
        if not download or not download.download_url:
            return None
        # 同步写一份 db 持久化，避免下次再走 list_files
        try:
            from database.db import db
            await db.upsert_path_file_cache(
                account_id=account_id,
                remote_path=_remote_path,
                file_id=file_id,
                pick_code=pick_code,
                file_name=file_name,
                file_size=file_size,
                parent_id=str(getattr(file_item, "extra", {}).get("parent_id", "") or ""),
            )
        except Exception:
            pass
        from core.path_download_cache import CachedDownload
        return CachedDownload(
            file_id=file_id,
            file_name=file_name or download.file_name,
            file_size=file_size or int(download.file_size or 0),
            download_url=download.download_url,
            headers=download.headers,
            ts=__import__("time").monotonic(),
        )

    return _fetcher
