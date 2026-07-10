"""STRM 播放链接签名与路径编码工具。"""

import base64
import hashlib
import hmac
import re
import urllib.parse

from config import Settings


def build_strm_play_path(account_id: int, file_id: str) -> str:
    encoded_file_id = urllib.parse.quote(str(file_id).lstrip("/"), safe="/")
    return f"/api/strm/play/{int(account_id)}/{encoded_file_id}"


def encode_strm_file_key(file_id: str) -> str:
    raw = str(file_id or "").encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_strm_file_key(file_key: str) -> str:
    text = str(file_key or "").strip()
    if not text:
        return ""
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + padding).encode("ascii")).decode("utf-8")


def build_strm_v2_base_path(account_id: int, file_id: str, token: str) -> str:
    file_key = encode_strm_file_key(file_id)
    token_segment = urllib.parse.quote(str(token or "").strip(), safe="")
    return f"/api/strm/v2/play/{int(account_id)}/{file_key}/t/{token_segment}"


def build_strm_v2_play_path(account_id: int, file_id: str, token: str, signature_enabled: bool = False) -> str:
    base_path = build_strm_v2_base_path(account_id, file_id, token)
    if signature_enabled:
        return f"{base_path}/s/{sign_strm_path(base_path)}"
    return base_path


def sign_strm_path(path: str) -> str:
    secret = str(Settings.SECRET_KEY or "litepan-strm").encode("utf-8")
    digest = hmac.new(secret, str(path).encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def verify_strm_signature(path: str, signature: str) -> bool:
    expected = sign_strm_path(path)
    return hmac.compare_digest(expected, str(signature or "").strip())


# ---- v3 path-based play path (TgtoDrive-style: /d/{account_id}/{file_path}) ----

def normalize_strm_remote_path(path: str) -> str:
    """统一 STRM 里使用的远端路径：始终以 / 开头，不带尾斜杠。

    兼容空字符串与多余前缀斜杠；中段连续斜杠合并。
    """
    text = str(path or "").replace("\\", "/").strip()
    while "//" in text:
        text = text.replace("//", "/")
    if not text.startswith("/"):
        text = "/" + text
    return text.rstrip("/") or "/"


def extract_filename_from_remote_path(remote_path: str) -> str:
    """从远端路径里截取最后一段作为文件名（带扩展名）。"""
    norm = normalize_strm_remote_path(remote_path)
    if norm in ("", "/"):
        return ""
    return norm.rsplit("/", 1)[-1]


def build_strm_v3_base_path(account_id: int, remote_path: str) -> str:
    """构造 /d/{account_id}/{remote_path} 形式的基础播放路径。

    remote_path 形如：/Movies/碟中谍4.mkv
    account_id 形如：1
    """
    norm = normalize_strm_remote_path(remote_path)
    encoded = urllib.parse.quote(norm, safe="/")
    return f"/d/{int(account_id)}{encoded}"


def build_strm_v3_play_url(base_url: str, account_id: int, remote_path: str) -> str:
    """返回完整的 v3 STRM 播放 URL：{base}/d/{account_id}{remote_path}"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = ""
    return f"{base}{build_strm_v3_base_path(account_id, remote_path)}"


def parse_strm_v3_play_path(path: str):
    """从 /d/{account_id}/{remote_path} 解析出 account_id 和 remote_path。

    返回 dict 或 None（解析失败）。
    """
    text = str(path or "").strip()
    if not text:
        return None
    qs = ""
    if "?" in text:
        text, qs = text.split("?", 1)
    if "#" in text:
        text = text.split("#", 1)[0]
    m = re.match(r"^/d/(\d+)(/.*)$", text)
    if not m:
        return None
    raw_remote = urllib.parse.unquote(m.group(2) or "")
    return {
        "account_id": int(m.group(1)),
        "remote_path": normalize_strm_remote_path(raw_remote),
        "query": qs,
    }
