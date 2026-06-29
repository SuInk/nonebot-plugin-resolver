import asyncio
import os.path
import subprocess
from functools import wraps
from typing import cast, Iterable, Union
from urllib.parse import parse_qs, urlparse

from bilibili_api import video, Credential, live, article
from bilibili_api.favorite_list import get_video_favorite_list_content
from bilibili_api.opus import Opus
from bilibili_api.video import VideoDownloadURLDataDetecter
from nonebot import on_regex, get_driver, on_command
from nonebot.adapters.onebot.v11 import Message, Event, Bot, MessageSegment, GROUP_ADMIN, GROUP_OWNER
from nonebot.adapters.onebot.v11.event import GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import FinishedException
from nonebot.matcher import current_bot, current_event
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .config import Config
# noinspection PyUnresolvedReferences
from .constants import COMMON_HEADER, URL_TYPE_CODE_DICT, DOUYIN_VIDEO, GENERAL_REQ_LINK, XHS_REQ_LINK, DY_TOUTIAO_INFO, \
    BILIBILI_HEADER, NETEASE_API_CN, NETEASE_TEMP_API, VIDEO_MAX_MB, \
    WEIBO_SINGLE_INFO, KUGOU_TEMP_API
from .core.acfun import parse_url, download_m3u8_videos, parse_m3u8, merge_ac_file_to_mp4
from .core.bili23 import download_b_file, merge_file_to_mp4, extra_bili_info
from .core.common import *
from .core.tiktok import generate_x_bogus_url, dou_transfer_other
from .core.weibo import mid2id
from .core.ytdlp import get_video_title, download_ytb_video

__plugin_meta__ = PluginMetadata(
    name="链接分享解析器",
    description="NoneBot2链接分享解析器插件。解析视频、图片链接/小程序插件，tiktok、bilibili、twitter等实时发送！",
    usage="分享链接即可体验到效果",
    type="application",
    homepage="https://github.com/zhiyu1998/nonebot-plugin-resolver",
    config=Config,
    supported_adapters={ "~onebot.v11" }
)

# 配置加载
global_config = Config.parse_obj(get_driver().config.dict())
# 全局名称
GLOBAL_NICKNAME: str = str(getattr(global_config, "r_global_nickname", ""))
# 🪜地址
resolver_proxy: str = getattr(global_config, "resolver_proxy", "http://127.0.0.1:7890")
# 是否是海外服务器
IS_OVERSEA: bool = bool(getattr(global_config, "is_oversea", False))
# 视频发送最大时长（默认15分钟），单位：秒
VIDEO_DURATION_MAXIMUM: int = int(getattr(global_config, "video_duration_maximum", 900))
# 全局解析内容控制
GLOBAL_RESOLVE_CONTROLLER: list = split_and_strip(str(getattr(global_config, "global_resolve_controller", "[]")), ",")
# 哔哩哔哩的 SESSDATA
BILI_SESSDATA: str = str(getattr(global_config, "bili_sessdata", ""))
# 构建哔哩哔哩的Credential
credential = Credential(sessdata=BILI_SESSDATA)

bili23 = on_regex(
    r"(bilibili.com|b23.tv|bili2233.cn|^BV[0-9a-zA-Z]{10}$)", priority=1
)
douyin = on_regex(
    r"(v.douyin.com)", priority=1
)
tik = on_regex(
    r"(www.tiktok.com|vt.tiktok.com|vm.tiktok.com)", priority=1
)
acfun = on_regex(r"(acfun.cn)")
twit = on_regex(
    r"(x.com)", priority=1
)
xhs = on_regex(
    r"(xhslink.com|xiaohongshu.com)", priority=1
)
y2b = on_regex(
    r"(youtube.com|youtu.be)", priority=1
)
ncm = on_regex(
    r"(music.163.com|163cn.tv)"
)
weibo = on_regex(
    r"(weibo.com|m.weibo.cn)"
)
kg = on_regex(
    r"(kugou.com)"
)

enable_resolve = on_command('开启解析', rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER | SUPERUSER)
disable_resolve = on_command('关闭解析', rule=to_me(), permission=GROUP_ADMIN | GROUP_OWNER | SUPERUSER)
check_resolve = on_command('查看关闭解析', permission=SUPERUSER)
RESOLVER_FORWARD_MATCHERS = (bili23, douyin, tik, acfun, twit, xhs, y2b, ncm, weibo, kg)

# 内存中关闭解析的名单，第一次先进行初始化
resolve_shutdown_list_in_memory: list = load_or_initialize_list()


def resolve_handler(func):
    """
    解析控制装饰器
    :param func:
    :return:
    """

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # 假设 `event` 是通过被装饰函数的参数传入的
        event = kwargs.get('event') or args[1]  # 根据位置参数或者关键字参数获取 event
        send_id = get_id_both(event)

        if send_id not in resolve_shutdown_list_in_memory:
            return await func(*args, **kwargs)
        else:
            logger.info(f"发送者/群 {send_id} 已关闭解析，不再执行")
            return None

    return wrapper


@enable_resolve.handle()
async def enable(bot: Bot, event: Event):
    """
    开启解析
    :param bot:
    :param event:
    :return:
    """
    send_id = get_id_both(event)
    if send_id in resolve_shutdown_list_in_memory:
        resolve_shutdown_list_in_memory.remove(send_id)
        save_sub_user(resolve_shutdown_list_in_memory)
        logger.info(resolve_shutdown_list_in_memory)
        await enable_resolve.finish('解析已开启')
    else:
        await enable_resolve.finish('解析已开启，无需重复开启')


@disable_resolve.handle()
async def disable(bot: Bot, event: Event):
    """
    关闭解析
    :param bot:
    :param event:
    :return:
    """
    send_id = get_id_both(event)
    if send_id not in resolve_shutdown_list_in_memory:
        resolve_shutdown_list_in_memory.append(send_id)
        save_sub_user(resolve_shutdown_list_in_memory)
        logger.info(resolve_shutdown_list_in_memory)
        await disable_resolve.finish('解析已关闭')
    else:
        await disable_resolve.finish('解析已关闭，无需重复关闭')


@check_resolve.handle()
async def check_disable(bot: Bot, event: Event):
    """
    查看关闭解析
    :param bot:
    :param event:
    :return:
    """
    memory_disable_list = [str(item) + "--" + (await bot.get_group_info(group_id=item))['group_name'] for item in
                           resolve_shutdown_list_in_memory]
    memory_disable_list = "1. 在【内存】中的名单有：\n" + '\n'.join(memory_disable_list)
    persistence_disable_list = [str(item) + "--" + (await bot.get_group_info(group_id=item))['group_name'] for item in
                                list(load_sub_user())]
    persistence_disable_list = "2. 在【持久层】中的名单有：\n" + '\n'.join(persistence_disable_list)

    await check_resolve.send(Message("已经发送到私信了~"))
    await bot.send_private_msg(user_id=event.user_id, message=Message(
        "[nonebot-plugin-resolver 关闭名单如下：]" + "\n\n" + memory_disable_list + '\n\n' + persistence_disable_list + "\n\n" + "🌟 温馨提示：如果想关闭解析需要艾特我然后输入: 关闭解析"))


def resolve_controller(func):
    """
        将装饰器应用于函数，通过装饰器自动判断是否允许执行函数
    :param func:
    :return:
    """

    logger.debug(
        f"[nonebot-plugin-resolver][解析全局控制] 加载 {func.__name__} {'禁止' if func.__name__ in GLOBAL_RESOLVE_CONTROLLER else '允许'}")

    @wraps(func)
    async def wrapper(*args, **kwargs):
        # 判断函数名是否在允许列表中
        if func.__name__ not in GLOBAL_RESOLVE_CONTROLLER:
            logger.info(f"[nonebot-plugin-resolver][解析全局控制] {func.__name__}...")
            return await func(*args, **kwargs)
        else:
            logger.warning(f"[nonebot-plugin-resolver][解析全局控制] {func.__name__} 被禁止执行")
            return None

    return wrapper


@bili23.handle()
@resolve_handler
@resolve_controller
async def bilibili(bot: Bot, event: Event) -> None:
    """
        哔哩哔哩解析
    :param bot:
    :param event:
    :return:
    """
    # 消息
    url: str = str(event.message).strip()
    # 正则匹配
    url_reg = r"(http:|https:)\/\/(space|www|live).bilibili.com\/[A-Za-z\d._?%&+\-=\/#]*"
    b_short_rex = r"(https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z\d._?%&+\-=\/#]+)"
    # BV处理
    if re.match(r'^BV[1-9a-zA-Z]{10}$', url):
        url = 'https://www.bilibili.com/video/' + url
    # 处理短号、小程序问题
    if "b23.tv" in url or "bili2233.cn" in url or "QQ小程序" in url:
        b_short_url = re.search(b_short_rex, url.replace("\\", ""))[0]
        resp = httpx.get(b_short_url, headers=BILIBILI_HEADER, follow_redirects=True)
        url: str = str(resp.url)
    else:
        url: str = re.search(url_reg, url).group(0)
    # ===============发现解析的是动态，转移一下===============
    if ('t.bilibili.com' in url or '/opus' in url) and BILI_SESSDATA != '':
        # 去除多余的参数
        if '?' in url:
            url = url[:url.index('?')]
        dynamic_id = int(re.search(r'[^/]+(?!.*/)', url)[0])
        dynamic_info = await Opus(dynamic_id, credential).get_info()
        # 这里比较复杂，暂时不用管，使用下面这个算法即可实现哔哩哔哩动态转发
        if dynamic_info is not None:
            title = dynamic_info['item']['basic']['title']
            paragraphs = []
            for module in dynamic_info['item']['modules']:
                if 'module_content' in module:
                    paragraphs = module['module_content']['paragraphs']
                    break
            desc = paragraphs[0]['text']['nodes'][0]['word']['words']
            pics = paragraphs[1]['pic']['pics']
            await bili23.send(Message(f"{GLOBAL_NICKNAME}识别：B站动态，{title}\n{desc}"))
            send_pics = []
            for pic in pics:
                img = pic['url']
                send_pics.append(make_node_segment(bot.self_id, MessageSegment.image(img)))
            # 发送异步后的数据
            await send_forward_both(bot, event, send_pics)
        return
    # 直播间识别
    if 'live' in url:
        # https://live.bilibili.com/30528999?hotRank=0
        room_id = re.search(r'\/(\d+)$', url).group(1)
        room = live.LiveRoom(room_display_id=int(room_id))
        room_info = (await room.get_room_info())['room_info']
        title, cover, keyframe = room_info['title'], room_info['cover'], room_info['keyframe']
        await bili23.send(Message([MessageSegment.image(cover), MessageSegment.image(keyframe),
                                   MessageSegment.text(f"{GLOBAL_NICKNAME}识别：哔哩哔哩直播，{title}")]))
        return
    # 专栏识别
    if 'read' in url:
        read_id = re.search(r'read\/cv(\d+)', url).group(1)
        ar = article.Article(read_id)
        # 如果专栏为公开笔记，则转换为笔记类
        # NOTE: 笔记类的函数与专栏类的函数基本一致
        if ar.is_note():
            ar = ar.turn_to_note()
        # 加载内容
        await ar.fetch_content()
        markdown_path = f'{os.getcwd()}/article.md'
        with open(markdown_path, 'w', encoding='utf8') as f:
            f.write(ar.markdown())
        await bili23.send(Message(f"{GLOBAL_NICKNAME}识别：哔哩哔哩专栏"))
        await bili23.send(Message(MessageSegment(type="file", data={ "file": markdown_path })))
        return
    # 收藏夹识别
    if 'favlist' in url and BILI_SESSDATA != '':
        # https://space.bilibili.com/22990202/favlist?fid=2344812202
        fav_id = re.search(r'favlist\?fid=(\d+)', url).group(1)
        fav_list = (await get_video_favorite_list_content(fav_id))['medias'][:10]
        favs = []
        for fav in fav_list:
            title, cover, intro, link = fav['title'], fav['cover'], fav['intro'], fav['link']
            logger.info(title, cover, intro)
            favs.append(
                [MessageSegment.image(cover),
                 MessageSegment.text(f'🧉 标题：{title}\n📝 简介：{intro}\n🔗 链接：{link}')])
        await bili23.send(f'{GLOBAL_NICKNAME}识别：哔哩哔哩收藏夹，正在为你找出相关链接请稍等...')
        await bili23.send(make_node_segment(bot.self_id, favs))
        return
    # 获取视频信息
    video_id = re.search(r"video\/[^\?\/ ]+", url)[0].split('/')[1]
    v = video.Video(video_id, credential=credential)
    video_info = await v.get_info()
    if video_info is None:
        await bili23.send(Message(f"{GLOBAL_NICKNAME}识别：B站，出错，无法获取数据！"))
        return
    video_title, video_cover, video_desc, video_duration = video_info['title'], video_info['pic'], video_info['desc'], \
        video_info['duration']
    # 校准 分p 的情况
    page_num = 0
    if 'pages' in video_info:
        # 解析URL
        parsed_url = urlparse(url)
        # 检查是否有查询字符串
        if parsed_url.query:
            # 解析查询字符串中的参数
            query_params = parse_qs(parsed_url.query)
            # 获取指定参数的值，如果参数不存在，则返回None
            page_num = int(query_params.get('p', [1])[0]) - 1
        else:
            page_num = 0
        if 'duration' in video_info['pages'][page_num]:
            video_duration = video_info['pages'][page_num].get('duration', video_info.get('duration'))
        else:
            # 如果索引超出范围，使用 video_info['duration'] 或者其他默认值
            video_duration = video_info.get('duration', 0)
    # 删除特殊字符
    video_title = delete_boring_characters(video_title)
    # 截断下载时间比较长的视频
    online = await v.get_online()
    online_str = f'🏄‍♂️ 总共 {online["total"]} 人在观看，{online["count"]} 人在网页端观看'
    bili_meta_message = Message(MessageSegment.image(video_cover)) + Message(
        f"\n{GLOBAL_NICKNAME}识别：B站，{video_title}\n{extra_bili_info(video_info)}\n📝 简介：{video_desc}\n{online_str}")
    if video_duration <= VIDEO_DURATION_MAXIMUM:
        pass
    else:
        return await bili23.finish(
            Message(MessageSegment.image(video_cover)) + Message(
                f"\n{GLOBAL_NICKNAME}识别：B站，{video_title}\n{extra_bili_info(video_info)}\n简介：{video_desc}\n{online_str}\n---------\n⚠️ 当前视频时长 {video_duration // 60} 分钟，超过管理员设置的最长时间 {VIDEO_DURATION_MAXIMUM // 60} 分钟！"))
    # 获取下载链接
    logger.info(page_num)
    download_url_data = await v.get_download_url(page_index=page_num)
    detecter = VideoDownloadURLDataDetecter(download_url_data)
    streams = detecter.detect_best_streams()
    video_url, audio_url = streams[0].url, streams[1].url
    video_urls = bili_stream_candidate_urls(download_url_data, streams[0], "video")
    audio_urls = bili_stream_candidate_urls(download_url_data, streams[1], "audio")
    # 下载视频和音频
    path = os.getcwd() + "/" + video_id
    try:
        await download_bili_media_with_fallback(video_urls, audio_urls, path)
        await merge_file_to_mp4(f"{path}-video.m4s", f"{path}-audio.m4s", f"{path}-res.mp4")
    except Exception as e:
        logger.error(f"B站媒体下载或合并失败: {e}")
        await bili23.send(Message(f"{GLOBAL_NICKNAME}识别：B站，媒体下载失败：{e}"))
        return
    finally:
        remove_res = remove_files([f"{path}-video.m4s", f"{path}-audio.m4s"])
        logger.info(remove_res)
    if BILI_SESSDATA != '':
        try:
            ai_conclusion = await v.get_ai_conclusion(await v.get_cid(0))
            ai_summary = ai_conclusion['model_result']['summary']
            if ai_summary != '':
                bili_meta_message += Message(f"\n\nbilibili AI总结\n{ai_summary}")
        except Exception as e:
            logger.warning(f"B站 AI 总结获取失败，跳过总结: {e}")
    # 发送出去
    # await bili23.send(Message(MessageSegment.video(f"{path}-res.mp4")))
    await send_real_video_forward_both(bot, event, bili_meta_message, f"{path}-res.mp4")


@douyin.handle()
@resolve_handler
@resolve_controller
async def dy(bot: Bot, event: Event) -> None:
    """
        抖音解析
    :param bot:
    :param event:
    :return:
    """
    # 消息
    msg: str = str(event.message).strip()
    logger.info(msg)
    # 正则匹配
    reg = r"(http:|https:)\/\/v.douyin.com\/[A-Za-z\d._?%&+\-=#]*"
    dou_url = re.search(reg, msg, re.I)[0]
    dou_url_2 = httpx.get(dou_url).headers.get('location')

    # 实况图集临时解决方案，eg.  https://v.douyin.com/iDsVgJKL/
    if "share/slides" in dou_url_2:
        cover, author, title, images = await dou_transfer_other(dou_url)
        # 如果第一个不为None 大概率是成功
        if author is not None:
            meta_node = Message([MessageSegment.image(cover), MessageSegment.text(
                f"\n{GLOBAL_NICKNAME}识别：【抖音】\n作者：{author}\n标题：{title}"
            )])
            await send_forward_both(bot, event, [meta_node] + [MessageSegment.image(url) for url in images])
        # 截断后续操作
        return
    # logger.error(dou_url_2)
    reg2 = r".*(video|note)\/(\d+)\/(.*?)"
    # 获取到ID
    dou_id = re.search(reg2, dou_url_2, re.I)[2]
    # logger.info(dou_id)
    # 如果没有设置dy的ck就结束，因为获取不到
    douyin_ck = getattr(global_config, "douyin_ck", "")
    if douyin_ck == "":
        logger.error(global_config)
        await douyin.send(Message(f"{GLOBAL_NICKNAME}识别：抖音，无法获取到管理员设置的抖音ck！"))
        return
    # API、一些后续要用到的参数
    headers = COMMON_HEADER | {
                  'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
                  'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
                  'referer': f'https://www.douyin.com/video/{dou_id}',
                  'cookie': douyin_ck
              }
    api_url = DOUYIN_VIDEO.replace("{}", dou_id)
    api_url = generate_x_bogus_url(api_url, headers)  # 如果请求失败直接返回
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url, headers=headers, timeout=10) as response:
            detail = await response.json()
            if detail is None:
                await douyin.send(Message(f"{GLOBAL_NICKNAME}识别：抖音，解析失败！"))
                return
            # 获取信息
            detail = detail['aweme_detail']
            # 判断是图片还是视频
            url_type_code = detail['aweme_type']
            url_type = URL_TYPE_CODE_DICT.get(url_type_code, 'video')
            title_message = Message(f"{GLOBAL_NICKNAME}识别：抖音，{detail.get('desc')}")
            # 根据类型进行发送
            if url_type == 'video':
                # 识别播放地址
                player_uri = detail.get("video").get("play_addr")['uri']
                player_real_addr = DY_TOUTIAO_INFO.replace("{}", player_uri)
                cover = detail.get("video", {}).get("cover", {}).get("url_list", [None])[0]
                meta_text = f"\n{GLOBAL_NICKNAME}识别：抖音，{detail.get('desc')}"
                meta_node = Message([MessageSegment.image(cover), MessageSegment.text(meta_text)]) if cover else title_message
                await send_real_video_forward_both(bot, event, meta_node, player_real_addr)
            elif url_type == 'image':
                # 无水印图片列表/No watermark image list
                no_watermark_image_list = [title_message]
                # 有水印图片列表/With watermark image list
                watermark_image_list = []
                # 遍历图片列表/Traverse image list
                for i in detail['images']:
                    # 无水印图片列表
                    # no_watermark_image_list.append(i['url_list'][0])
                    no_watermark_image_list.append(MessageSegment.image(i['url_list'][0]))
                    # 有水印图片列表
                    # watermark_image_list.append(i['download_url_list'][0])
                # 异步发送
                # logger.info(no_watermark_image_list)
                # imgList = await asyncio.gather([])
                await send_forward_both(bot, event, make_node_segment(bot.self_id, no_watermark_image_list))


@tik.handle()
@resolve_handler
@resolve_controller
async def tiktok(event: Event) -> None:
    """
        tiktok解析
    :param event:
    :return:
    """
    # 消息
    url: str = str(event.message).strip()

    # 海外服务器判断
    proxy = None if IS_OVERSEA else resolver_proxy

    url_reg = r"(http:|https:)\/\/www.tiktok.com\/[A-Za-z\d._?%&+\-=\/#@]*"
    url_short_reg = r"(http:|https:)\/\/vt.tiktok.com\/[A-Za-z\d._?%&+\-=\/#]*"
    url_short_reg2 = r"(http:|https:)\/\/vm.tiktok.com\/[A-Za-z\d._?%&+\-=\/#]*"

    if "vt.tiktok" in url:
        temp_url = re.search(url_short_reg, url)[0]
        temp_resp = httpx.get(temp_url, follow_redirects=True, proxies=proxy)
        url = temp_resp.url
    elif "vm.tiktok" in url:
        temp_url = re.search(url_short_reg2, url)[0]
        temp_resp = httpx.get(temp_url, headers={ "User-Agent": "facebookexternalhit/1.1" }, follow_redirects=True,
                              proxies=proxy)
        url = str(temp_resp.url)
        # logger.info(url)
    else:
        url = re.search(url_reg, url)[0]
    title = await get_video_title(url, IS_OVERSEA, resolver_proxy, 'tiktok')

    await tik.send(Message(f"{GLOBAL_NICKNAME}识别：TikTok，{title}\n"))

    target_tik_video_path = await download_ytb_video(url, IS_OVERSEA, os.getcwd(), resolver_proxy, 'tiktok')

    await auto_video_send(event, target_tik_video_path)


@acfun.handle()
@resolve_handler
@resolve_controller
async def ac(event: Event) -> None:
    """
        acfun解析
    :param event:
    :return:
    """
    # 消息
    inputMsg: str = str(event.message).strip()

    # 短号处理
    if "m.acfun.cn" in inputMsg:
        inputMsg = f"https://www.acfun.cn/v/ac{re.search(r'ac=([^&?]*)', inputMsg)[1]}"

    url_m3u8s, video_name = parse_url(inputMsg)
    await acfun.send(Message(f"{GLOBAL_NICKNAME}识别：猴山，{video_name}"))
    m3u8_full_urls, ts_names, output_folder_name, output_file_name = parse_m3u8(url_m3u8s)
    # logger.info(output_folder_name, output_file_name)
    await asyncio.gather(*[download_m3u8_videos(url, i) for i, url in enumerate(m3u8_full_urls)])
    merge_ac_file_to_mp4(ts_names, output_file_name)
    # await acfun.send(Message(MessageSegment.video(f"{os.getcwd()}/{output_file_name}")))
    await auto_video_send(event, f"{os.getcwd()}/{output_file_name}")


@twit.handle()
@resolve_handler
@resolve_controller
async def twitter(bot: Bot, event: Event):
    """
        X解析
    :param bot:
    :param event:
    :return:
    """
    msg: str = str(event.message).strip()
    x_match = re.search(r"https?:\/\/x.com\/[0-9-a-zA-Z_]{1,20}\/status\/([0-9]*)", msg)
    if x_match is None:
        return
    x_url = x_match[0]

    x_url = GENERAL_REQ_LINK.replace("{}", x_url)

    # 内联一个请求
    def x_req(url):
        return httpx.get(url, headers={
            'Accept': 'ext/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,'
                      'application/signed-exchange;v=b3;q=0.7',
            'Accept-Encoding': 'gzip, deflate',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Host': '47.99.158.118',
            'Proxy-Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-User': '?1',
            **COMMON_HEADER
        })

    x_data: object = x_req(x_url).json()['data']

    if x_data is None:
        x_url = x_url + '/photo/1'
        logger.info(x_url)
        x_data = x_req(x_url).json()['data']

    x_url_res = x_data['url']
    x_meta_message = Message(f"{GLOBAL_NICKNAME}识别：小蓝鸟学习版")

    # 海外服务器判断
    proxy = None if IS_OVERSEA else resolver_proxy

    # 图片
    if x_url_res.endswith(".jpg") or x_url_res.endswith(".png"):
        await twit.send(x_meta_message)
        res = await download_img(x_url_res, '', proxy)
    else:
        # 视频
        res = await download_video(x_url_res, proxy)
    if not res:
        await twit.send(Message(f"{GLOBAL_NICKNAME}识别：小蓝鸟学习版\n媒体下载失败，可能是代理不可用、解析源失效或媒体链接被限制。"))
        return

    if res.endswith("mp4"):
        await send_real_video_forward_both(bot, event, x_meta_message, res)
        return
    else:
        aio_task_res = auto_determine_send_type(int(bot.self_id), res)
        await send_forward_both(bot, event, aio_task_res)

    # 清除垃圾
    if os.path.exists(res):
        os.unlink(res)


@xhs.handle()
@resolve_handler
@resolve_controller
async def xiaohongshu(bot: Bot, event: Event):
    """
        小红书解析
    :param event:
    :return:
    """
    msg_url = re.search(r"(http:|https:)\/\/(xhslink|(www\.)xiaohongshu).com\/[A-Za-z\d._?%&+\-=\/#@]*",
                        str(event.message).replace("&amp;", "&").strip())[0]
    # 如果没有设置xhs的ck就结束，因为获取不到
    xhs_ck = getattr(global_config, "xhs_ck", "")
    if xhs_ck == "":
        logger.error(global_config)
        await xhs.send(Message(f"{GLOBAL_NICKNAME}识别内容来自：【小红书】\n无法获取到管理员设置的小红书ck！"))
        return
    # 请求头
    headers = {
                  'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,'
                            'application/signed-exchange;v=b3;q=0.9',
                  'cookie': xhs_ck,
              } | COMMON_HEADER
    if "xhslink" in msg_url:
        msg_url = httpx.get(msg_url, headers=headers, follow_redirects=True).url
        msg_url = str(msg_url)
    xhs_id = re.search(r'/explore/(\w+)', msg_url)
    if not xhs_id:
        xhs_id = re.search(r'/discovery/item/(\w+)', msg_url)
    if not xhs_id:
        xhs_id = re.search(r'source=note&noteId=(\w+)', msg_url)
    xhs_id = xhs_id[1]
    # 解析 URL 参数
    parsed_url = urlparse(msg_url)
    params = parse_qs(parsed_url.query)
    # 提取 xsec_source 和 xsec_token
    xsec_source = params.get('xsec_source', [None])[0] or "pc_feed"
    xsec_token = params.get('xsec_token', [None])[0]

    html = httpx.get(f'{XHS_REQ_LINK}{xhs_id}?xsec_source={xsec_source}&xsec_token={xsec_token}', headers=headers).text
    # response_json = re.findall('window.__INITIAL_STATE__=(.*?)</script>', html)[0]
    try:
        response_json = re.findall('window.__INITIAL_STATE__=(.*?)</script>', html)[0]
    except IndexError:
        await xhs.send(
            Message(f"{GLOBAL_NICKNAME}识别内容来自：【小红书】\n当前ck已失效，请联系管理员重新设置的小红书ck！"))
        return
    response_json = response_json.replace("undefined", "null")
    response_json = json.loads(response_json)
    note_data = response_json['note']['noteDetailMap'][xhs_id]['note']
    type = note_data['type']
    note_title = note_data['title']
    note_desc = note_data['desc']
    aio_task = []
    if type == 'normal':
        image_list = note_data['imageList']
        # 批量下载
        async with aiohttp.ClientSession() as session:
            for index, item in enumerate(image_list):
                aio_task.append(asyncio.create_task(
                    download_img(item['urlDefault'], f'{os.getcwd()}/{str(index)}.jpg', session=session)))
            links_path = await asyncio.gather(*aio_task)
    elif type == 'video':
        # 这是一条解析有水印的视频
        logger.info(note_data['video'])

        stream = note_data['video']['media']['stream']
        video_candidates = []
        for item in stream.get('h264', []):
            if item.get('masterUrl'):
                video_candidates.append(item['masterUrl'])
            video_candidates.extend(item.get('backupUrls') or [])

        # ⚠️ 废弃，解析无水印视频video.consumer.originVideoKey
        # video_url = f"http://sns-video-bd.xhscdn.com/{note_data['video']['consumer']['originVideoKey']}"
        path = None
        xhs_video_headers = {
            'referer': 'https://www.xiaohongshu.com/',
            'origin': 'https://www.xiaohongshu.com',
        }
        for video_url in dict.fromkeys(video_candidates):
            path = await download_video(video_url, ext_headers=xhs_video_headers)
            if path:
                break
        if not path:
            await xhs.send(Message(f"{GLOBAL_NICKNAME}识别内容来自：【小红书】\n视频直链均不可用，暂时无法发送视频。"))
            return
        cover = note_data.get('imageList', [{}])[0].get('urlDefault') if note_data.get('imageList') else None
        meta_node = Message([MessageSegment.image(cover), MessageSegment.text(
            f"\n{GLOBAL_NICKNAME}识别内容来自：【小红书】\n"
            f"作者：{note_data.get('user', {}).get('nickname')}\n"
            f"标题：{note_data.get('title')}\n"
            f"内容：{note_data.get('desc')}"
        )]) if cover else Message(
            f"{GLOBAL_NICKNAME}识别内容来自：【小红书】\n"
            f"作者：{note_data.get('user', {}).get('nickname')}\n"
            f"标题：{note_data.get('title')}\n"
            f"内容：{note_data.get('desc')}"
        )
        await send_real_video_forward_both(bot, event, meta_node, path)
        return
    # 发送图片
    meta_node = Message(
        f"{GLOBAL_NICKNAME}识别内容来自：【小红书】\n"
        f"作者：{note_data.get('user', {}).get('nickname')}\n"
        f"标题：{note_data.get('title')}\n"
        f"内容：{note_data.get('desc')}"
    )
    links = [meta_node] + [MessageSegment.image(f"file://{link}") for link in links_path]
    # 发送异步后的数据
    await send_forward_both(bot, event, links)
    # 清除图片
    for temp in links_path:
        os.unlink(temp)


@y2b.handle()
@resolve_handler
@resolve_controller
async def youtube(bot: Bot, event: Event):
    msg_url = re.search(
        r"(?:https?:\/\/)?(www\.)?youtube\.com\/[A-Za-z\d._?%&+\-=\/#]*|(?:https?:\/\/)?youtu\.be\/[A-Za-z\d._?%&+\-=\/#]*",
        str(event.message).strip())[0]

    # 海外服务器判断
    proxy = None if IS_OVERSEA else resolver_proxy

    title = await get_video_title(msg_url, IS_OVERSEA, proxy)

    await y2b.send(Message(f"{GLOBAL_NICKNAME}识别：油管，{title}\n"))

    target_ytb_video_path = await download_ytb_video(msg_url, IS_OVERSEA, os.getcwd(), proxy)

    await auto_video_send(event, target_ytb_video_path)


@ncm.handle()
@resolve_handler
@resolve_controller
async def netease(bot: Bot, event: Event):
    message = str(event.message)
    # 识别短链接
    if "163cn.tv" in message:
        message = re.search(r"(http:|https:)\/\/163cn\.tv\/([a-zA-Z0-9]+)", message).group(0)
        message = str(httpx.head(message, follow_redirects=True).url)

    ncm_id = re.search(r"id=(\d+)", message).group(1)
    if ncm_id is None:
        await ncm.finish(Message(f"❌ {GLOBAL_NICKNAME}识别：网易云，获取链接失败"))
    # 拼接获取信息的链接
    # ncm_detail_url = f'{NETEASE_API_CN}/song/detail?ids={ncm_id}'
    # ncm_detail_resp = httpx.get(ncm_detail_url, headers=COMMON_HEADER)
    # # 获取歌曲名
    # ncm_song = ncm_detail_resp.json()['songs'][0]
    # ncm_title = f'{ncm_song["name"]}-{ncm_song["ar"][0]["name"]}'.replace(r'[\/\?<>\\:\*\|".… ]', "")

    # 对接临时接口
    ncm_vip_data = httpx.get(f"{NETEASE_TEMP_API.replace('{}', ncm_id)}", headers=COMMON_HEADER).json()
    ncm_url = ncm_vip_data['music_url']
    ncm_cover = ncm_vip_data['cover']
    ncm_singer = ncm_vip_data['singer']
    ncm_title = ncm_vip_data['title']
    await ncm.send(Message(
        [MessageSegment.image(ncm_cover), MessageSegment.text(f'{GLOBAL_NICKNAME}识别：网易云音乐，{ncm_title}-{ncm_singer}')]))
    # 下载音频文件后会返回一个下载路径
    ncm_music_path = await download_audio(ncm_url)
    # 发送语音
    await ncm.send(Message(MessageSegment.record(ncm_music_path)))
    # 发送群文件
    await upload_both(bot, event, ncm_music_path, f'{ncm_title}-{ncm_singer}.{ncm_music_path.split(".")[-1]}')
    if os.path.exists(ncm_music_path):
        os.unlink(ncm_music_path)


@kg.handle()
@resolve_handler
@resolve_controller
async def kugou(bot: Bot, event: Event):
    message = str(event.message)
    # logger.info(message)
    reg1 = r"https?://.*?kugou\.com.*?(?=\s|$|\n)"
    reg2 = r'jumpUrl":\s*"(https?:\\/\\/[^"]+)"'
    reg3 = r'jumpUrl":\s*"(https?://[^"]+)"'
    # 处理卡片问题
    if 'com.tencent.structmsg' in message:
        match = re.search(reg2, message)
        if match:
            get_url = match.group(1)
        else:
            match = re.search(reg3, message)
            if match:
                get_url = match.group(1)
            else:
                await kg.send(Message(f"{GLOBAL_NICKNAME}\n来源：【酷狗音乐】\n获取链接失败"))
                get_url = None
                return
        if get_url:
            url = json.loads('"' + get_url + '"')
    else:
        match = re.search(reg1, message)
        url = match.group()

        # 使用 httpx 获取 URL 的标题
    response = httpx.get(url, follow_redirects=True)
    if response.status_code == 200:
        title = response.text
        get_name = r"<title>(.*?)_高音质在线试听"
        name = re.search(get_name, title)
        if name:
            kugou_title = name.group(1)  # 只输出歌曲名和歌手名的部分
            kugou_vip_data = httpx.get(f"{KUGOU_TEMP_API.replace('{}', kugou_title)}", headers=COMMON_HEADER).json()
            # logger.info(kugou_vip_data)
            kugou_url = kugou_vip_data.get('music_url')
            kugou_cover = kugou_vip_data.get('cover')
            kugou_name = kugou_vip_data.get('title')
            kugou_singer = kugou_vip_data.get('singer')
            await kg.send(Message(
                [MessageSegment.image(kugou_cover),
                 MessageSegment.text(f'{GLOBAL_NICKNAME}\n来源：【酷狗音乐】\n歌曲：{kugou_name}-{kugou_singer}')]))
            # 下载音频文件后会返回一个下载路径
            kugou_music_path = await download_audio(kugou_url)
            # 发送语音
            await kg.send(Message(MessageSegment.record(kugou_music_path)))
            # 发送群文件
            await upload_both(bot, event, kugou_music_path,
                              f'{kugou_name}-{kugou_singer}.{kugou_music_path.split(".")[-1]}')
            if os.path.exists(kugou_music_path):
                os.unlink(kugou_music_path)
        else:
            await kg.send(Message(f"{GLOBAL_NICKNAME}\n来源：【酷狗音乐】\n不支持当前外链，请重新分享再试"))
    else:
        await kg.send(Message(f"{GLOBAL_NICKNAME}\n来源：【酷狗音乐】\n获取链接失败"))


@weibo.handle()
@resolve_handler
@resolve_controller
async def wb(bot: Bot, event: Event):
    message = str(event.message)
    weibo_id = None
    reg = r'(jumpUrl|qqdocurl)": ?"(.*?)"'

    # 处理卡片问题
    if 'com.tencent.structmsg' or 'com.tencent.miniapp' in message:
        match = re.search(reg, message)
        print(match)
        if match:
            get_url = match.group(2)
            print(get_url)
            if get_url:
                message = json.loads('"' + get_url + '"')
    else:
        message = message
    # logger.info(message)
    # 判断是否包含 "m.weibo.cn"
    if "m.weibo.cn" in message:
        # https://m.weibo.cn/detail/4976424138313924
        match = re.search(r'(?<=detail/)[A-Za-z\d]+', message) or re.search(r'(?<=m.weibo.cn/)[A-Za-z\d]+/[A-Za-z\d]+',
                                                                            message)
        weibo_id = match.group(0) if match else None

    # 判断是否包含 "weibo.com/tv/show" 且包含 "mid="
    elif "weibo.com/tv/show" in message and "mid=" in message:
        # https://weibo.com/tv/show/1034:5007449447661594?mid=5007452630158934
        match = re.search(r'(?<=mid=)[A-Za-z\d]+', message)
        if match:
            weibo_id = mid2id(match.group(0))

    # 判断是否包含 "weibo.com"
    elif "weibo.com" in message:
        # https://weibo.com/1707895270/5006106478773472
        match = re.search(r'(?<=weibo.com/)[A-Za-z\d]+/[A-Za-z\d]+', message)
        weibo_id = match.group(0) if match else None

    # 无法获取到id则返回失败信息
    if not weibo_id:
        await weibo.finish(Message("解析失败：无法获取到wb的id"))
    # 最终获取到的 id
    weibo_id = weibo_id.split("/")[1] if "/" in weibo_id else weibo_id
    logger.info(weibo_id)
    # 请求数据
    resp = httpx.get(WEIBO_SINGLE_INFO.replace('{}', weibo_id), headers={
                                                                            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                                                                            "cookie": "_T_WM=40835919903; WEIBOCN_FROM=1110006030; MLOGIN=0; XSRF-TOKEN=4399c8",
                                                                            "Referer": f"https://m.weibo.cn/detail/{id}",
                                                                        } | COMMON_HEADER).json()
    weibo_data = resp['data']
    logger.info(weibo_data)
    text, status_title, source, region_name, pics, page_info = (weibo_data.get(key, None) for key in
                                                                ['text', 'status_title', 'source', 'region_name',
                                                                 'pics', 'page_info'])
    # 发送消息
    await weibo.send(
        Message(
            f"{GLOBAL_NICKNAME}识别：微博，{re.sub(r'<[^>]+>', '', text)}\n{status_title}\n{source}\t{region_name if region_name else ''}"))
    if pics:
        pics = map(lambda x: x['url'], pics)
        download_img_funcs = [asyncio.create_task(download_img(item, '', headers={
                                                                                     "Referer": "http://blog.sina.com.cn/"
                                                                                 } | COMMON_HEADER)) for item in pics]
        links_path = await asyncio.gather(*download_img_funcs)
        # 发送图片
        links = make_node_segment(bot.self_id,
                                  [MessageSegment.image(f"file://{link}") for link in links_path])
        # 发送异步后的数据
        await send_forward_both(bot, event, links)
        # 清除图片
        for temp in links_path:
            os.unlink(temp)
    if page_info:
        video_url = page_info.get('urls', '').get('mp4_720p_mp4', '') or page_info.get('urls', '').get('mp4_hd_mp4', '')
        if video_url:
            path = await download_video(video_url, ext_headers={
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
                "referer": "https://weibo.com/"
            })
            await auto_video_send(event, path)


def auto_determine_send_type(user_id: int, task: str):
    """
        判断是视频还是图片然后发送最后删除，函数在 twitter 这类可以图、视频混合发送的媒体十分有用
    :param user_id:
    :param task:
    :return:
    """
    if task.endswith("jpg") or task.endswith("png"):
        return MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                          content=Message(MessageSegment.image(task)))
    elif task.endswith("mp4"):
        return MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                          content=Message(MessageSegment.video(task)))


def make_node_segment(user_id, segments: Union[MessageSegment, List]) -> Union[
    MessageSegment, Iterable[MessageSegment]]:
    """
        将消息封装成 Segment 的 Node 类型，可以传入单个也可以传入多个，返回一个封装好的转发类型
    :param user_id: 可以通过event获取
    :param segments: 一般为 MessageSegment.image / MessageSegment.video / MessageSegment.text
    :return:
    """
    if isinstance(segments, list):
        return [MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                           content=Message(segment)) for segment in segments]
    return MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                      content=Message(segments))


def _coerce_node_content(segment) -> Message:
    if isinstance(segment, Message):
        return segment
    if isinstance(segment, MessageSegment):
        return Message(segment)
    return Message(str(segment))


def _coerce_forward_nodes(user_id, segments: Union[Message, MessageSegment, List, str]) -> Union[
    MessageSegment, Iterable[MessageSegment]]:
    if isinstance(segments, Message):
        node_segments = [segment for segment in segments if segment.type == "node"]
        if node_segments and len(node_segments) == len(segments):
            return node_segments
        return MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                          content=segments)

    if isinstance(segments, MessageSegment):
        if segments.type == "node":
            return segments
        return MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                          content=Message(segments))

    if isinstance(segments, list):
        if all(isinstance(segment, MessageSegment) and segment.type == "node" for segment in segments):
            return segments
        return [MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                           content=_coerce_node_content(segment)) for segment in segments]

    return MessageSegment.node_custom(user_id=user_id, nickname=GLOBAL_NICKNAME,
                                      content=Message(str(segments)))


async def send_forward_both(bot: Bot, event: Event, segments: Union[MessageSegment, List]) -> None:
    """
        自动判断message是 List 还是单个，然后发送{转发}，允许发送群和个人
    :param bot:
    :param event:
    :param segments:
    :return:
    """
    segments = _coerce_forward_nodes(bot.self_id, segments)
    if isinstance(event, GroupMessageEvent):
        await bot.send_group_forward_msg(group_id=event.group_id,
                                         messages=segments)
    else:
        await bot.send_private_forward_msg(user_id=event.user_id,
                                           messages=segments)


async def send_both(bot: Bot, event: Event, segments: Union[Message, MessageSegment, List, str]) -> None:
    """
        自动判断message是 List 还是单个，统一发送{合并转发}，允许发送群和个人
    :param bot:
    :param event:
    :param segments:
    :return:
    """
    await send_forward_both(bot, event, segments)


async def send_direct_both(bot: Bot, event: Event, segments: Union[Message, MessageSegment, str]) -> None:
    message = segments if isinstance(segments, Message) else Message(segments)
    if isinstance(event, GroupMessageEvent):
        await bot.send_group_msg(group_id=event.group_id, message=message)
    else:
        await bot.send_private_msg(user_id=event.user_id, message=message)


async def _cleanup_media_file_later(data_path: str | None, delay_seconds: int) -> None:
    await asyncio.sleep(delay_seconds)
    if isinstance(data_path, str) and os.path.exists(data_path):
        os.unlink(data_path)
    if isinstance(data_path, str) and os.path.exists(data_path + '.jpg'):
        os.unlink(data_path + '.jpg')


def cleanup_media_file(data_path: str | None, delay_seconds: int = 0) -> None:
    if delay_seconds <= 0:
        if isinstance(data_path, str) and os.path.exists(data_path):
            os.unlink(data_path)
        if isinstance(data_path, str) and os.path.exists(data_path + '.jpg'):
            os.unlink(data_path + '.jpg')
        return
    asyncio.create_task(_cleanup_media_file_later(data_path, delay_seconds))


def get_video_duration_seconds(data_path: str | None) -> float | None:
    if not data_path or not os.path.exists(data_path):
        return None
    try:
        result = subprocess.run(
            [
                '/usr/bin/ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration',
                '-of',
                'default=noprint_wrappers=1:nokey=1',
                data_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            logger.warning(f"视频时长获取失败: {result.stderr[-500:]}")
            return None
        duration_text = result.stdout.strip()
        if not duration_text or duration_text == "N/A":
            return None
        return float(duration_text)
    except Exception as e:
        logger.warning(f"视频时长获取异常: {e}")
        return None


def video_duration_limit_message(data_path: str | None) -> str | None:
    duration = get_video_duration_seconds(data_path)
    if duration is None:
        logger.warning(f"无法获取视频时长，将继续发送: {data_path}")
        return None
    if duration <= VIDEO_DURATION_MAXIMUM:
        return None
    duration_minutes = int(duration // 60)
    duration_seconds = int(duration % 60)
    limit_minutes = VIDEO_DURATION_MAXIMUM // 60
    logger.warning(
        f"当前视频时长 {duration:.1f}s 超过最长 {VIDEO_DURATION_MAXIMUM}s，跳过视频发送: {data_path}"
    )
    return f"当前视频时长 {duration_minutes} 分 {duration_seconds} 秒，超过最长 {limit_minutes} 分钟，暂不发送视频。"


def ensure_video_thumbnail(data_path: str) -> bool:
    thumb_path = data_path + '.jpg'
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        return True
    try:
        result = subprocess.run(
            [
                '/usr/bin/ffmpeg',
                '-y',
                '-ss',
                '00:00:00.500',
                '-i',
                data_path,
                '-frames:v',
                '1',
                '-q:v',
                '2',
                thumb_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            logger.warning(f"视频封面生成失败: {result.stderr[-500:]}")
            return False
        return os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0
    except Exception as e:
        logger.warning(f"视频封面生成异常: {e}")
        return False


async def build_video_forward_node(data_path: str) -> tuple[MessageSegment | None, str | None]:
    """
    下载远端视频并构造合并转发节点，调用方负责发送后清理临时文件。
    """
    if data_path is not None and data_path.startswith("http"):
        data_path = await download_video(data_path)

    if not data_path:
        logger.warning("视频路径为空，跳过视频节点")
        return None, data_path

    duration_limit_message = video_duration_limit_message(data_path)
    if duration_limit_message is not None:
        return MessageSegment.text(duration_limit_message), data_path

    file_size_in_mb = get_file_size_mb(data_path)
    if file_size_in_mb > VIDEO_MAX_MB:
        logger.warning(f"当前解析文件 {file_size_in_mb} MB 大于 {VIDEO_MAX_MB} MB，跳过合并转发视频节点")
        return MessageSegment.text(
            f"当前解析文件 {file_size_in_mb} MB 大于 {VIDEO_MAX_MB} MB，暂不塞进合并转发。"
        ), data_path

    if ensure_video_thumbnail(data_path):
        logger.info(f"已生成视频封面: {data_path}.jpg")
    else:
        logger.warning(f"未能生成视频封面，将继续发送视频: {data_path}")

    return MessageSegment.video(f'file://{data_path}'), data_path


async def send_video_direct_both(bot: Bot, event: Event, data_path: str) -> None:
    video_node, downloaded_path = await build_video_forward_node(data_path)
    if video_node is None:
        logger.warning("视频下载失败，已仅发送标题和封面")
        cleanup_media_file(downloaded_path)
        return
    try:
        try:
            staging = await bot.send_private_msg(user_id=int(bot.self_id), message=Message(video_node))
            message_id = staging.get("message_id") if isinstance(staging, dict) else None
            if not message_id:
                raise RuntimeError(f"发给自己的视频没有返回 message_id: {staging}")
            if isinstance(event, GroupMessageEvent):
                await bot.call_api("forward_group_single_msg", group_id=event.group_id, message_id=message_id)
            else:
                await bot.call_api("forward_friend_single_msg", user_id=event.user_id, message_id=message_id)
            logger.info(f"已通过自发视频消息转发: message_id={message_id}")
        except Exception as e:
            logger.warning(f"自发视频再转发失败，降级为普通视频消息: {e}")
            await send_direct_both(bot, event, video_node)
    finally:
        cleanup_media_file(downloaded_path)


def _get_message_id(result) -> int | None:
    if isinstance(result, dict):
        return result.get("message_id")
    return None


def _make_message_id_node(message_id: int) -> dict:
    return {"type": "node", "data": {"id": str(message_id)}}


async def _send_real_forward_nodes(bot: Bot, event: Event, message_ids: list[int]) -> None:
    messages = [_make_message_id_node(message_id) for message_id in message_ids]
    if isinstance(event, GroupMessageEvent):
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=messages)
    else:
        await bot.call_api("send_private_forward_msg", user_id=event.user_id, messages=messages)


async def send_real_video_forward_both(bot: Bot, event: Event, meta_message: Message | MessageSegment | str | None,
                                       data_path: str) -> None:
    """
    先把元信息和视频发给机器人自己，再用真实 message_id 合并转发到目标会话。
    这绕开 NapCat custom forward video node 的“视频已过期”问题。
    """
    video_node, downloaded_path = await build_video_forward_node(data_path)
    if video_node is None:
        logger.warning("视频下载失败，已仅发送标题和封面")
        if meta_message is not None:
            await send_forward_both(bot, event, meta_message)
        cleanup_media_file(downloaded_path)
        return

    try:
        message_ids = []
        if meta_message is not None:
            try:
                meta_result = await bot.send_private_msg(user_id=int(bot.self_id), message=Message(meta_message))
                meta_message_id = _get_message_id(meta_result)
                if meta_message_id:
                    message_ids.append(meta_message_id)
                else:
                    logger.warning(f"发给自己的元信息没有返回 message_id: {meta_result}")
            except Exception as e:
                logger.warning(f"发给自己的元信息失败，将仅转发视频: {e}")

        video_result = await bot.send_private_msg(user_id=int(bot.self_id), message=Message(video_node))
        video_message_id = _get_message_id(video_result)
        if not video_message_id:
            raise RuntimeError(f"发给自己的视频没有返回 message_id: {video_result}")
        message_ids.append(video_message_id)

        await _send_real_forward_nodes(bot, event, message_ids)
        logger.info(f"已通过自发真实消息合并转发: message_ids={message_ids}")
    except Exception as e:
        logger.warning(f"自发真实消息合并转发失败，不再降级单发视频: {e}")
        error_message = Message(f"{GLOBAL_NICKNAME}视频合并转发失败：{e}")
        try:
            await send_forward_both(bot, event, [meta_message, error_message] if meta_message is not None else error_message)
        except Exception as forward_error:
            logger.warning(f"合并转发错误信息失败，降级为纯文本提示: {forward_error}")
            await send_direct_both(bot, event, error_message)
    finally:
        cleanup_media_file(downloaded_path)


async def _resolver_forward_send(cls, message=None, **kwargs) -> None:
    bot: Bot = cast(Bot, current_bot.get())
    event: Event = cast(Event, current_event.get())
    if message is None:
        message = Message("")
    await send_forward_both(bot, event, message)


async def _resolver_forward_finish(cls, message=None, **kwargs) -> None:
    if message is not None:
        await _resolver_forward_send(cls, message, **kwargs)
    raise FinishedException


def _install_resolver_forward_sender() -> None:
    for matcher in RESOLVER_FORWARD_MATCHERS:
        matcher.send = classmethod(_resolver_forward_send)
        matcher.finish = classmethod(_resolver_forward_finish)


def unique_urls(urls) -> list[str]:
    result = []
    seen = set()
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def bili_dash_item_urls(item: dict) -> list[str]:
    urls = [item.get("baseUrl"), item.get("base_url")]
    backup_urls = item.get("backupUrl") or item.get("backup_url") or []
    if isinstance(backup_urls, str):
        urls.append(backup_urls)
    else:
        urls.extend(backup_urls)
    return unique_urls(urls)


def bili_stream_candidate_urls(download_url_data: dict, stream, media_type: str) -> list[str]:
    primary_url = getattr(stream, "url", None)
    candidates = [primary_url]
    dash = download_url_data.get("dash") or {}
    for item in dash.get(media_type) or []:
        item_urls = bili_dash_item_urls(item)
        if primary_url in item_urls:
            candidates.extend(item_urls)
            break
    return unique_urls(candidates)


async def download_bili_media_with_fallback(video_urls: list[str], audio_urls: list[str], path: str) -> None:
    video_path = f"{path}-video.m4s"
    audio_path = f"{path}-audio.m4s"
    errors = []
    for video_url in video_urls:
        remove_files([video_path, audio_path])
        video_host = urlparse(video_url).netloc
        try:
            logger.info(f"B站视频流下载候选: {video_host}")
            await download_b_file(video_url, video_path, logger.info)
        except Exception as e:
            errors.append(f"video {video_host}: {type(e).__name__}")
            logger.warning(f"B站视频流下载失败: host={video_host}, error={e}")
            continue
        for audio_url in audio_urls:
            remove_files([audio_path])
            audio_host = urlparse(audio_url).netloc
            try:
                logger.info(f"B站音频流下载候选: {audio_host}")
                await download_b_file(audio_url, audio_path, logger.info)
                return
            except Exception as e:
                errors.append(f"audio {audio_host}: {type(e).__name__}")
                logger.warning(f"B站音频流下载失败: host={audio_host}, error={e}")
        remove_files([video_path, audio_path])
    raise RuntimeError("已尝试 B站主 URL/backupUrl 均失败: " + "; ".join(errors[-6:]))


async def upload_both(bot: Bot, event: Event, file_path: str, name: str) -> None:
    """
        上传文件，不限于群和个人
    :param bot:
    :param event:
    :param file_path:
    :param name:
    :return:
    """
    if isinstance(event, GroupMessageEvent):
        # 上传群文件
        await bot.upload_group_file(group_id=event.group_id, file=file_path, name=name)
    elif isinstance(event, PrivateMessageEvent):
        # 上传私聊文件
        await bot.upload_private_file(user_id=event.user_id, file=file_path, name=name)


def get_id_both(event: Event):
    if isinstance(event, GroupMessageEvent):
        return event.group_id
    elif isinstance(event, PrivateMessageEvent):
        return event.user_id


async def auto_video_send(event: Event, data_path: str):
    """
    自动判断视频类型并进行发送，支持群发和私发
    :param event:
    :param data_path:
    :return:
    """
    try:
        bot: Bot = cast(Bot, current_bot.get())

        # 如果data以"http"开头，先下载视频
        if data_path is not None and data_path.startswith("http"):
            data_path = await download_video(data_path)

        if not data_path:
            logger.warning("视频路径为空，跳过发送")
            return

        duration_limit_message = video_duration_limit_message(data_path)
        if duration_limit_message is not None:
            await send_both(bot, event, Message(duration_limit_message))
            return

        # 检测文件大小
        file_size_in_mb = get_file_size_mb(data_path)
        # 如果视频大于 100 MB 自动转换为群文件
        if file_size_in_mb > VIDEO_MAX_MB:
            await send_both(bot, event, Message(
                f"当前解析文件 {file_size_in_mb} MB 大于 {VIDEO_MAX_MB} MB，尝试改用文件方式发送，请稍等..."))
            await upload_both(bot, event, data_path, data_path.split('/')[-1])
            return
        # 根据事件类型发送不同的消息
        await send_both(bot, event, MessageSegment.video(f'file://{data_path}'))
    except Exception as e:
        logger.error(f"解析发送出现错误，具体为\n{e}")
    finally:
        cleanup_media_file(data_path)


_install_resolver_forward_sender()
