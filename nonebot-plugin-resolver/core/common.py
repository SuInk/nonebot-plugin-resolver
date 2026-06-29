import json
import os
import re
import time
from typing import List, Dict, Any
from urllib.parse import urlparse, parse_qs, unquote
from nonebot import require, logger

require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store

import aiofiles
import aiohttp
import httpx

from ..constants import COMMON_HEADER, PLUGIN_NAME, RESOLVE_SHUTDOWN_LIST_NAME


async def download_video(url, proxy: str = None, ext_headers=None) -> str:
    """
    异步下载（httpx）视频，并支持通过代理下载。
    文件名将使用时间戳生成，以确保唯一性。
    如果提供了代理地址，则会通过该代理下载视频。

    :param ext_headers:
    :param url: 要下载的视频的URL。
    :param proxy: 可选，下载视频时使用的代理服务器的URL。
    :return: 保存视频的路径。
    """
    # 使用时间戳生成文件名，确保唯一性
    path = os.path.join(os.getcwd(), f"{int(time.time())}.mp4")

    # 判断 ext_headers 是否为 None
    if ext_headers is None:
        headers = COMMON_HEADER
    else:
        # 使用 update 方法合并两个字典
        headers = COMMON_HEADER.copy()  # 先复制 COMMON_HEADER
        headers.update(ext_headers)  # 然后更新 ext_headers

    # 配置代理
    client_config = {
        'headers': headers,
        'timeout': httpx.Timeout(60, connect=5.0),
        'follow_redirects': True
    }
    if proxy:
        client_config['proxies'] = { 'https': proxy }

    # 下载文件
    try:
        async with httpx.AsyncClient(**client_config) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "").lower()
                if content_type and "video/" not in content_type and "octet-stream" not in content_type:
                    raise ValueError(f"unexpected content-type: {content_type}")
                async with aiofiles.open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        await f.write(chunk)
        return path
    except Exception as e:
        print(f"下载视频错误原因是: {e}")
        if os.path.exists(path):
            os.unlink(path)
        return None


async def probe_video_url(url: str, proxy: str = None, ext_headers=None) -> bool:
    """
    下载前检查 URL 是否能返回真实视频内容，避免把 403/HTML 当视频发送。
    """
    if ext_headers is None:
        headers = COMMON_HEADER
    else:
        headers = COMMON_HEADER.copy()
        headers.update(ext_headers)

    client_config = {
        'headers': headers,
        'timeout': httpx.Timeout(20, connect=5.0),
        'follow_redirects': True,
    }
    if proxy:
        client_config['proxies'] = { 'https': proxy }

    try:
        async with httpx.AsyncClient(**client_config) as client:
            response = await client.get(url, headers={ **headers, "Range": "bytes=0-0" })
            if response.status_code not in (200, 206):
                logger.warning(f"视频链接不可用 status={response.status_code} url={url}")
                return False
            content_type = response.headers.get("content-type", "").lower()
            if "video/" not in content_type and "octet-stream" not in content_type:
                logger.warning(f"视频链接 content-type 异常 type={content_type} url={url}")
                return False
            return True
    except Exception as e:
        logger.warning(f"视频链接探测失败: {type(e).__name__}: {e}")
        return False


async def download_img(url: str, path: str = '', proxy: str = None, session=None, headers=None) -> str:
    """
    异步下载（aiohttp）网络图片，并支持通过代理下载。
    如果未指定path，则图片将保存在当前工作目录并以图片的文件名命名。
    如果提供了代理地址，则会通过该代理下载图片。

    :param url: 要下载的图片的URL。
    :param path: 图片保存的路径。如果为空，则保存在当前目录。
    :param proxy: 可选，下载图片时使用的代理服务器的URL。
    :return: 保存图片的路径。
    """
    if path == '':
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        filename = unquote(filename)
        filename = re.sub(r'[<>:"/\\|?*]', "_", filename)

        original_ext = os.path.splitext(filename)[1].lower()
        ext = original_ext
        if ext not in { ".jpg", ".jpeg", ".png", ".webp", ".gif" }:
            query_params = parse_qs(parsed_url.query)
            fmt = (query_params.get("format") or query_params.get("fmt") or [None])[0]
            if fmt:
                fmt = fmt.lower()
                if fmt in { "jpg", "jpeg", "png", "webp", "gif" }:
                    ext = f".{fmt}"
        if not filename:
            filename = f"{int(time.time())}{ext or '.jpg'}"
        elif not original_ext:
            filename = f"{filename}{ext or '.jpg'}"

        path = os.path.join(os.getcwd(), filename)
    # 单个文件下载
    if session is None:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, proxy=proxy, headers=headers) as response:
                if response.status == 200:
                    data = await response.read()
                    with open(path, 'wb') as f:
                        f.write(data)
    # 多个文件异步下载
    else:
        async with session.get(url, proxy=proxy, headers=headers) as response:
            if response.status == 200:
                data = await response.read()
                with open(path, 'wb') as f:
                    f.write(data)
    return path


async def download_audio(url):
    # 从URL中提取文件名
    parsed_url = urlparse(url)
    file_name = parsed_url.path.split('/')[-1]
    # 去除可能存在的请求参数
    file_name = file_name.split('?')[0]

    path = os.path.join(os.getcwd(), file_name)

    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        response.raise_for_status()  # 检查请求是否成功

        async with aiofiles.open(path, 'wb') as file:
            await file.write(response.content)
    return path


def delete_boring_characters(sentence):
    """
        去除标题的特殊字符
    :param sentence:
    :return:
    """
    return re.sub(r'[0-9’!"∀〃#$%&\'()*+,-./:;<=>?@，。?★、…【】《》？“”‘’！[\\]^_`{|}~～\s]+', "", sentence)


def remove_files(file_paths: List[str]) -> Dict[str, str]:
    """
    根据路径删除文件

    Parameters:
    *file_paths (str): 要删除的一个或多个文件路径

    Returns:
    dict: 一个以文件路径为键、删除状态为值的字典
    """
    results = { }

    for file_path in file_paths:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                results[file_path] = 'remove'
            except Exception as e:
                results[file_path] = f'error: {e}'
        else:
            results[file_path] = 'don\'t exist'

    return results


def get_file_size_mb(file_path):
    """
    判断当前文件的大小是多少MB
    :param file_path:
    :return:
    """
    # 获取文件大小（以字节为单位）
    file_size_bytes = os.path.getsize(file_path)

    # 将字节转换为 MB 并取整
    file_size_mb = int(file_size_bytes / (1024 * 1024))

    return file_size_mb


def load_or_initialize_list() -> List[Any]:
    data_file = store.get_data_file(PLUGIN_NAME, RESOLVE_SHUTDOWN_LIST_NAME)
    # 判断是否存在
    if not data_file.exists():
        data_file.write_text(json.dumps([]))
    return list(json.loads(data_file.read_text()))


def save_sub_user(sub_group):
    """
    使用pickle将对象保存到文件
    :return: None
    """
    data_file = store.get_data_file(PLUGIN_NAME, RESOLVE_SHUTDOWN_LIST_NAME)
    data_file.write_text(json.dumps(sub_group))


def load_sub_user():
    """
    从文件中加载对象
    :return: 订阅用户列表
    """
    data_file = store.get_data_file(PLUGIN_NAME, RESOLVE_SHUTDOWN_LIST_NAME)
    # 判断是否存在
    if not data_file.exists():
        data_file.write_text(json.dumps([]))
    return json.loads(data_file.read_text())


def split_and_strip(text, sep=None) -> List[str]:
    # 先去除两边的空格，然后按指定分隔符分割
    split_text = text.strip().split(sep)
    # 去除每个子字符串两边的空格
    return [sub_text.strip() for sub_text in split_text]
