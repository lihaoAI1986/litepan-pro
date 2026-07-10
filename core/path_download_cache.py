"""路径 -> 下载直链 缓存层（仿 TgtoDrive url_cache）。

设计目标：
1. 按 (account_id, normalized_remote_path) 缓存 (file_id, download_url, ts)；
2. TTL 默认 30 分钟，过期后下次访问会重取直链；
3. 同目录其它文件支持后台预缓存，便于首播接近零延迟；
4. 进程内 LRU + 全表容量保护，避免长时间运行内存爆炸。

不持久化：进程重启后清空；持久化由 database 层的 path_file_cache 表承担。
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple


@dataclass
class CachedDownload:
    file_id: str
    file_name: str
    file_size: int
    download_url: str
    headers: Optional[Dict[str, str]]
    ts: float  # monotonic time


class PathDownloadCache:
    """进程内 LRU 缓存：(account_id, remote_path) -> CachedDownload"""

    DEFAULT_TTL = 30 * 60          # 30 分钟
    DEFAULT_MAX_SIZE = 5000        # LRU 上限
    PRECACHE_DEFAULT = True
    PRECACHE_DEFAULT_BATCH = 100   # 单次预缓存最多取多少个兄弟文件

    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL,
        max_size: int = DEFAULT_MAX_SIZE,
    ):
        self._ttl = max(int(ttl_seconds or 0), 30)
        self._max_size = max(int(max_size or 1), 1)
        self._data: "OrderedDict[Tuple[int, str], CachedDownload]" = OrderedDict()
        self._locks: Dict[Tuple[int, str], asyncio.Lock] = {}
        self._precache_tasks: set = set()
        self._closed = False

    # ---- 配置 ----

    def update_ttl(self, ttl_seconds: int) -> None:
        self._ttl = max(int(ttl_seconds or 0), 30)

    def update_max_size(self, max_size: int) -> None:
        self._max_size = max(int(max_size or 1), 1)

    @property
    def ttl(self) -> int:
        return self._ttl

    @property
    def max_size(self) -> int:
        return self._max_size

    # ---- 基础读写 ----

    def _normalize_key(self, account_id: int, remote_path: str) -> Tuple[int, str]:
        from core.strm_security import normalize_strm_remote_path
        return (int(account_id), normalize_strm_remote_path(remote_path))

    def _get_lock(self, key: Tuple[int, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def get(self, account_id: int, remote_path: str) -> Optional[CachedDownload]:
        key = self._normalize_key(account_id, remote_path)
        now = time.monotonic()
        cached = self._data.get(key)
        if cached is None:
            return None
        if now - cached.ts > self._ttl:
            # 过期
            try:
                del self._data[key]
            except KeyError:
                pass
            return None
        # LRU 触碰：移到末尾
        self._data.move_to_end(key)
        return cached

    def put(
        self,
        account_id: int,
        remote_path: str,
        *,
        file_id: str,
        file_name: str,
        file_size: int,
        download_url: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> CachedDownload:
        key = self._normalize_key(account_id, remote_path)
        item = CachedDownload(
            file_id=str(file_id or ""),
            file_name=str(file_name or ""),
            file_size=int(file_size or 0),
            download_url=str(download_url or ""),
            headers=dict(headers) if headers else None,
            ts=time.monotonic(),
        )
        self._data[key] = item
        self._data.move_to_end(key)
        self._evict_if_needed()
        return item

    def invalidate(self, account_id: int, remote_path: str) -> None:
        key = self._normalize_key(account_id, remote_path)
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()
        self._locks.clear()

    def stats(self) -> Dict[str, int]:
        return {
            "size": len(self._data),
            "max_size": self._max_size,
            "ttl": self._ttl,
        }

    def _evict_if_needed(self) -> None:
        while len(self._data) > self._max_size:
            try:
                self._data.popitem(last=False)
            except KeyError:
                break

    # ---- 异步获取 / 刷新 ----

    async def get_or_fetch(
        self,
        account_id: int,
        remote_path: str,
        fetcher: Callable[[int, str], Awaitable[Optional[CachedDownload]]],
    ) -> Optional[CachedDownload]:
        """带锁的"读穿透"实现：缓存命中直接返回；未命中调 fetcher 获取并写入。"""
        cached = self.get(account_id, remote_path)
        if cached is not None:
            return cached

        key = self._normalize_key(account_id, remote_path)
        lock = self._get_lock(key)
        async with lock:
            cached = self.get(account_id, remote_path)
            if cached is not None:
                return cached
            try:
                result = await fetcher(int(account_id), remote_path)
            except Exception:
                return None
            if result is None:
                return None
            self.put(
                account_id,
                remote_path,
                file_id=result.file_id,
                file_name=result.file_name,
                file_size=result.file_size,
                download_url=result.download_url,
                headers=result.headers,
            )
            return result

    # ---- 同目录预缓存 ----

    async def precache_directory(
        self,
        account_id: int,
        directory_remote_path: str,
        siblings_provider: Callable[[int, str], Awaitable[Any]],
        max_count: int = PRECACHE_DEFAULT_BATCH,
    ) -> int:
        """后台预缓存 directory_remote_path 下其它兄弟文件的直链。"""
        if self._closed:
            return 0
        coro = self._do_precache(account_id, directory_remote_path, siblings_provider, max_count)
        task = asyncio.create_task(coro)
        self._precache_tasks.add(task)
        task.add_done_callback(self._precache_tasks.discard)
        return 0

    async def _do_precache(
        self,
        account_id: int,
        directory_remote_path: str,
        siblings_provider: Callable[[int, str], Awaitable[Any]],
        max_count: int,
    ) -> int:
        try:
            siblings = await siblings_provider(int(account_id), directory_remote_path)
        except Exception:
            return 0
        if not siblings:
            return 0
        count = 0
        for item in siblings[: max(0, int(max_count or 0))]:
            if self._closed:
                break
            try:
                sibling_path = item.get("remote_path") if isinstance(item, dict) else None
                if not sibling_path:
                    continue
                cached = self.get(account_id, sibling_path)
                if cached is not None:
                    continue
                fetcher = item.get("fetcher") if isinstance(item, dict) else None
                if not callable(fetcher):
                    continue
                result = await fetcher(int(account_id), sibling_path)
                if result is None:
                    continue
                self.put(
                    account_id,
                    sibling_path,
                    file_id=result.file_id,
                    file_name=result.file_name,
                    file_size=result.file_size,
                    download_url=result.download_url,
                    headers=result.headers,
                )
                count += 1
            except Exception:
                continue
        return count

    async def close(self) -> None:
        self._closed = True
        for task in list(self._precache_tasks):
            task.cancel()
        if self._precache_tasks:
            await asyncio.gather(*self._precache_tasks, return_exceptions=True)


# ---- 全局单例 ----

import os  # noqa: E402

_DEFAULT_TTL = int(os.getenv("LITEPAN_PATH_CACHE_TTL", str(PathDownloadCache.DEFAULT_TTL)))
_DEFAULT_MAX_SIZE = int(os.getenv("LITEPAN_PATH_CACHE_MAX_SIZE", str(PathDownloadCache.DEFAULT_MAX_SIZE)))


path_download_cache = PathDownloadCache(
    ttl_seconds=_DEFAULT_TTL,
    max_size=_DEFAULT_MAX_SIZE,
)


def get_path_download_cache() -> PathDownloadCache:
    return path_download_cache
