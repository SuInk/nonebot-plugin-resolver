import asyncio
import os
import time
from pathlib import Path
from typing import Dict, Optional, Set

from nonebot import logger
import yt_dlp

LAST_YTDLP_ERROR: dict = {}


def _existing_path(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    path = Path(value).expanduser()
    return str(path) if path.is_file() else None


def _youtube_cookiefile() -> Optional[str]:
    for candidate in (
        os.getenv("YOUTUBE_COOKIES_FILE"),
        os.getenv("YT_DLP_COOKIES_FILE"),
        "ytb_cookies.txt",
        str(Path.cwd() / "ytb_cookies.txt"),
    ):
        if path := _existing_path(candidate):
            return path
    return None


def _youtube_js_runtimes() -> Dict[str, Dict[str, Optional[str]]]:
    deno_path = _existing_path(os.getenv("YOUTUBE_JS_RUNTIME"))
    if deno_path:
        return { "deno": { "path": deno_path } }
    return { "deno": {} }


def _build_ydl_opts(
    *,
    is_oversea: bool,
    my_proxy: Optional[str],
    video_type: str,
    skip_download: bool,
    output_dir: Optional[str] = None,
) -> dict:
    ydl_opts = {
        "quiet": False,
        "no_warnings": False,
        "ignoreerrors": False,
        "noplaylist": True,
        "skip_download": skip_download,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if not is_oversea and my_proxy:
        ydl_opts["proxy"] = my_proxy

    if video_type == "youtube":
        if cookiefile := _youtube_cookiefile():
            ydl_opts["cookiefile"] = cookiefile
        ydl_opts["js_runtimes"] = _youtube_js_runtimes()
        ydl_opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "web"],
            }
        }

    if not skip_download:
        target_dir = Path(output_dir or os.getcwd())
        target_dir.mkdir(parents=True, exist_ok=True)
        ydl_opts.update(
            {
                "outtmpl": str(target_dir / "%(title).180B-%(id)s.%(ext)s"),
                "merge_output_format": "mp4",
                "format": (
                    "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
                    "bv*[height<=1080]+ba/"
                    "b[height<=1080][ext=mp4]/"
                    "b[height<=1080]/b"
                ),
            }
        )
    return ydl_opts


def _friendly_error(exc: Exception, video_type: str) -> str:
    raw = str(exc)
    if video_type == "youtube":
        if "provided YouTube account cookies are no longer valid" in raw:
            return "YouTube cookies 已失效，需要重新导出 cookies。"
        if "Sign in to confirm" in raw or "not a bot" in raw:
            if _youtube_cookiefile():
                return "YouTube 要求登录验证，当前 cookies 不可用或已失效。"
            return "YouTube 要求登录验证，但当前没有配置可用的 cookies 文件。"
    return f"{video_type} 解析失败：{type(exc).__name__}: {raw[:240]}"


def _remember_error(url: str, message: str) -> None:
    LAST_YTDLP_ERROR.clear()
    LAST_YTDLP_ERROR.update({
        "url": url,
        "message": message,
        "at": time.time(),
    })


def _downloaded_file_from_info(ydl, info: Optional[dict], output_dir: str, before: Set[Path]) -> Optional[str]:
    if not info:
        return None
    for item in info.get("requested_downloads") or ():
        for key in ("filepath", "_filename", "filename"):
            value = item.get(key)
            if value and Path(value).is_file():
                return str(Path(value))

    candidates: list = []
    try:
        prepared = Path(ydl.prepare_filename(info))
        candidates.extend([prepared, prepared.with_suffix(".mp4")])
    except Exception:
        pass

    target_dir = Path(output_dir)
    try:
        candidates.extend(
            sorted(
                (path for path in target_dir.glob("*") if path.is_file() and path not in before),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        )
    except Exception:
        pass

    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return str(path)
    return None

async def get_video_title(url: str, is_oversea: bool, my_proxy=None, video_type='youtube') -> str:
    ydl_opts = _build_ydl_opts(
        is_oversea=is_oversea,
        my_proxy=my_proxy,
        video_type=video_type,
        skip_download=True,
    )
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await asyncio.to_thread(ydl.extract_info, url, download=False)
        if not info_dict:
            return "-"
        return info_dict.get("title") or "-"
    except Exception as exc:
        message = _friendly_error(exc, video_type)
        _remember_error(url, message)
        logger.error(message)
        return "解析失败"

async def download_ytb_video(url, is_oversea, path, my_proxy=None, video_type='youtube'):
    output_dir = str(Path(path))
    before = { item for item in Path(output_dir).glob("*") if item.is_file() }
    ydl_opts = _build_ydl_opts(
        is_oversea=is_oversea,
        my_proxy=my_proxy,
        video_type=video_type,
        skip_download=False,
        output_dir=output_dir,
    )
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info_dict = await asyncio.to_thread(ydl.extract_info, url, download=True)
            downloaded = _downloaded_file_from_info(ydl, info_dict, output_dir, before)
        if downloaded:
            return downloaded
        _remember_error(url, f"{video_type} 下载结束但没有找到输出文件。")
        logger.error(f"{video_type} 下载结束但没有找到输出文件")
        return None
    except Exception as exc:
        message = _friendly_error(exc, video_type)
        _remember_error(url, message)
        logger.error(message)
        return None
