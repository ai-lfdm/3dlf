#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hanime1.me 视频爬虫（最新上市页面，全量，从底部向上）
- 标题第一行：百度翻译（任何语言 → 简体中文），结果自动缓存
- 标签：繁体转简体
- 每次运行后保存 seen.txt，避免重复发送
"""

import os, re, sys, time, tempfile, json, logging, hashlib, random
from typing import Optional, Set, List, Tuple

import cloudscraper
import requests as req
from bs4 import BeautifulSoup
from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
import zhconv

# ---------- 配置 ----------
BASE_URL = "https://hanime1.me"
SEARCH_URL = f"{BASE_URL}/search?sort=最新上市&page=1"

CHAT_ID = os.environ["CHAT_ID"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SESSION_STRING = os.environ["SESSION_STRING"]

# 百度翻译配置
BAIDU_APP_ID = os.environ.get("BAIDU_APP_ID", "")
BAIDU_SECRET_KEY = os.environ.get("BAIDU_SECRET_KEY", "")

# 代理：从环境变量读取（可选），不再硬编码
PROXY = os.environ.get("PROXY", "")

REQUEST_DELAY = 2
SEEN_FILE = "seen.txt"
TRANSLATION_FILE = "title_translations.json"

MAX_RETRIES = 3

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# 翻译缓存（内存中维护，运行结束统一写盘，避免逐条读写 JSON）
_title_translation_cache: Optional[dict] = None
_title_translation_dirty: bool = False


# ---------- 工具函数 ----------
def _get_proxies() -> dict:
    """获取代理配置，PROXY 为空时返回空 dict"""
    if PROXY:
        return {"http": PROXY, "https": PROXY}
    return {}


def validate_env():
    """启动时校验必需的环境变量"""
    required = ["CHAT_ID", "API_ID", "API_HASH", "SESSION_STRING"]
    missing = [v for v in required if v not in os.environ]
    if missing:
        sys.exit(f"缺少必需的环境变量: {', '.join(missing)}")
    try:
        int(os.environ["API_ID"])
    except ValueError:
        sys.exit("API_ID 必须是整数")
    if not os.environ.get("CHAT_ID", "").lstrip("-").isdigit():
        sys.exit("CHAT_ID 必须是数字")


# ---------- 百度翻译（自动检测语言，目标语言简体中文）----------
def baidu_translate(text: str) -> str:
    """调用百度翻译API，将任意文本翻译为简体中文，失败返回原文"""
    if not BAIDU_APP_ID or not BAIDU_SECRET_KEY:
        logger.warning("百度翻译API未配置，跳过翻译")
        return text
    if len(text) > 2000:
        text = text[:2000]
    salt = random.randint(32768, 65536)
    sign = hashlib.md5(f"{BAIDU_APP_ID}{text}{salt}{BAIDU_SECRET_KEY}".encode()).hexdigest()
    url = "https://fanyi-api.baidu.com/api/trans/vip/translate"
    params = {
        "q": text,
        "from": "auto",
        "to": "zh",
        "appid": BAIDU_APP_ID,
        "salt": salt,
        "sign": sign,
    }
    try:
        resp = req.get(url, params=params, timeout=10)
        data = resp.json()
        if "trans_result" in data:
            translated = data["trans_result"][0]["dst"]
            logger.info(f"百度翻译成功: {text[:30]}... -> {translated[:30]}...")
            return translated
        else:
            logger.warning(f"百度翻译返回错误: {data}")
            return text
    except Exception as e:
        logger.error(f"百度翻译请求失败: {e}")
        return text


# ---------- 持久化函数 ----------
def load_seen_ids() -> Set[str]:
    seen = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                vid = line.strip()
                if vid:
                    seen.add(vid)
    return seen


def save_seen_ids(seen: Set[str]):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        for vid in sorted(seen):
            f.write(vid + "\n")


# ---------- 翻译缓存（内存维护，批量写盘）----------
def _ensure_trans_cache() -> dict:
    global _title_translation_cache
    if _title_translation_cache is None:
        _title_translation_cache = _load_translation_cache()
    return _title_translation_cache


def _load_translation_cache() -> dict:
    if not os.path.exists(TRANSLATION_FILE):
        return {}
    try:
        with open(TRANSLATION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"加载翻译缓存失败: {e}，将重建")
        backup = TRANSLATION_FILE + ".bak"
        if os.path.exists(TRANSLATION_FILE):
            os.rename(TRANSLATION_FILE, backup)
        return {}


def flush_translation_cache():
    """将内存中的翻译缓存写入磁盘，main() 结束时统一调用"""
    global _title_translation_dirty
    if _title_translation_cache and _title_translation_dirty:
        try:
            with open(TRANSLATION_FILE, "w", encoding="utf-8") as f:
                json.dump(_title_translation_cache, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存翻译缓存，共 {len(_title_translation_cache)} 条")
            _title_translation_dirty = False
        except Exception as e:
            logger.warning(f"保存翻译缓存失败: {e}")


def get_chinese_title(raw_title: str) -> str:
    """
    获取中文标题：优先本地缓存，未命中则调用百度翻译。
    """
    cleaned = re.sub(r'\[.*?\]', '', raw_title).strip()
    if not cleaned:
        cleaned = raw_title

    cache = _ensure_trans_cache()
    if cleaned in cache:
        return cache[cleaned]

    # 缓存未命中 -> 调用翻译API
    translated = baidu_translate(cleaned)
    if translated and translated != cleaned:
        cache[cleaned] = translated
        global _title_translation_dirty
        _title_translation_dirty = True
        return translated
    else:
        # 翻译失败时的回退：繁体转简体
        fallback = zhconv.convert(cleaned, 'zh-cn')
        logger.warning(f"翻译失败，使用繁转简回退: {fallback}")
        return fallback


def extract_video_id(url: str) -> str:
    m = re.search(r'v=(\d+)', url)
    return m.group(1) if m else ""


def get_soup(url: str, retries=MAX_RETRIES) -> BeautifulSoup:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            scraper = cloudscraper.create_scraper(
                browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False},
                delay=15,
            )
            resp = scraper.get(url, timeout=40, proxies=_get_proxies())
            resp.raise_for_status()
            return BeautifulSoup(resp.text, 'html.parser')
        except Exception as e:
            logger.warning(f"请求失败 (尝试 {attempt}/{retries}): {url}, 错误: {e}")
            last_exc = e
            time.sleep(5)
    raise last_exc


def parse_search_page(soup: BeautifulSoup) -> List[dict]:
    """从已解析的搜索页 soup 中提取视频卡片列表"""
    cards = soup.select('a[href*="/watch?v="]')
    videos = []
    for card in cards:
        href = card.get('href')
        if not href:
            continue
        if href.startswith('/watch?v='):
            full_url = BASE_URL + href
        elif href.startswith('https://hanime1.me/watch?v='):
            full_url = href
        else:
            continue
        img = card.find('img')
        cover = img.get('src') if img else ""
        if cover and cover.startswith('//'):
            cover = 'https:' + cover
        videos.append({'video_url': full_url, 'cover_url': cover})
    return videos


def clean_title(text: str) -> str:
    text = re.sub(r'\[.+?\]', '', text)
    text = text.replace('～', '').replace('~', '')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ---------- 标签提取（繁体转简体）----------
def extract_tags_from_soup(soup: BeautifulSoup, max_tags: int = 5) -> List[str]:
    tags = []
    meta_keywords = soup.find('meta', attrs={'name': 'keywords'})
    if meta_keywords and meta_keywords.get('content'):
        content = meta_keywords['content']
        raw_tags = re.split(r'[,，]+', content)
        for t in raw_tags:
            t = t.strip()
            if t:
                clean = re.sub(r'[^\w一-鿿]', '', t)
                if clean:
                    simplified = zhconv.convert(clean, 'zh-cn')
                    tags.append(simplified)
        if tags:
            return tags[:max_tags]
    tag_selectors = ['a.tag', 'a[href*="/search?tag="]', '.video-tags a', '.tags a']
    for selector in tag_selectors:
        elements = soup.select(selector)
        if elements:
            for el in elements[:max_tags]:
                tag_text = el.get_text(strip=True)
                if tag_text:
                    clean = re.sub(r'[^\w一-鿿]', '', tag_text)
                    if clean:
                        simplified = zhconv.convert(clean, 'zh-cn')
                        tags.append(simplified)
            if tags:
                break
    return tags[:max_tags]


def extract_date_from_soup(soup: BeautifulSoup) -> str:
    desc_div = soup.find('div', class_='video-description-panel-hover') or soup.find('div', class_='video-description-panel')
    if desc_div:
        text = desc_div.get_text()
        m = re.search(r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})', text)
        if m:
            return m.group(1).replace('/', '-')
    for text_node in soup.find_all(string=re.compile(r'\d{4}[/-]\d{1,2}[/-]\d{1,2}')):
        m = re.search(r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})', text_node)
        if m:
            return m.group(1).replace('/', '-')
    meta_desc = soup.find('meta', attrs={'name': 'description'})
    if meta_desc and meta_desc.get('content'):
        m = re.search(r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})', meta_desc['content'])
        if m:
            return m.group(1).replace('/', '-')
    return ""


# ---------- 视频链接提取（从已解析 soup 中提取，避免重复请求）----------
def _extract_video_url_from_dl_soup(soup: BeautifulSoup) -> str:
    """从下载页 soup 中提取直链（data-url / mp4 链接）"""
    # data-url 方式
    download_links = soup.select('a.juicyads-popunder[data-url]')
    if download_links:
        data_url = download_links[0].get('data-url', '')
        if data_url:
            if data_url.startswith('//'):
                data_url = 'https:' + data_url
            return data_url
    # mp4 链接方式
    for link in soup.find_all('a', href=re.compile(r'\.mp4')):
        href = link.get('href')
        if href.startswith('//'):
            href = 'https:' + href
        return href
    return ""


def _extract_video_url_via_redirect(video_id: str) -> str:
    """兜底方式：通过不跟随重定向获取最终视频链接"""
    url = f"{BASE_URL}/download?v={video_id}"
    try:
        scraper = cloudscraper.create_scraper()
        resp = scraper.get(url, proxies=_get_proxies(), timeout=15, allow_redirects=False)
        if resp.status_code in (301, 302, 303):
            loc = resp.headers.get('Location', '')
            if loc:
                if loc.startswith('//'):
                    loc = 'https:' + loc
                return loc
    except Exception as e:
        logger.warning(f"重定向提取失败: {e}")
    return ""


def _extract_video_url_from_watch_soup(soup: BeautifulSoup) -> str:
    """从观看页 soup 中提取直链"""
    video_tag = soup.find('video', id='player')
    if video_tag:
        sources = video_tag.find_all('source')
        best_url, best_size = "", 0
        for src in sources:
            u = src.get('src', '')
            sz_str = src.get('size', '0')
            try:
                sz = int(sz_str)
            except:
                sz = 0
            if sz > best_size:
                best_size = sz
                best_url = u
        if best_url:
            if best_url.startswith('//'):
                best_url = 'https:' + best_url
            return best_url
        vid_src = video_tag.get('src', '')
        if vid_src:
            if vid_src.startswith('//'):
                vid_src = 'https:' + vid_src
            return vid_src
    return ""


# ---------- 解析元数据 ----------
def parse_video_page_and_download(video_id: str, video_url: str) -> Tuple[str, str, str, str, List[str]]:
    """
    获取视频元数据 + 直链。
    下载页和观看页的 soup 结果会被复用，避免对同一个 URL 发起多次请求。
    """
    download_url = f"{BASE_URL}/download?v={video_id}"
    raw_title = ""
    poster_url = ""
    date_str = ""
    tags_list = []
    best_url = ""

    # 1. 从下载页获取元数据和视频直链
    try:
        soup_dl = get_soup(download_url)
        h3 = soup_dl.find('h3')
        if h3:
            raw_title = h3.get_text(strip=True)
        img = soup_dl.find('img', class_='download-image')
        if img:
            p = img.get('src', '')
            if p and p.startswith('//'):
                p = 'https:' + p
            poster_url = p
        date_candidate = extract_date_from_soup(soup_dl)
        if date_candidate:
            date_str = date_candidate
        tags_list = extract_tags_from_soup(soup_dl)
        # 复用相同的 soup 提取视频直链（避免单独再请求一次）
        best_url = _extract_video_url_from_dl_soup(soup_dl)
        if raw_title:
            logger.info("从下载页获得元数据")
    except Exception as e:
        logger.warning(f"下载页解析失败: {e}")

    # 2. 如果信息不完整，从观看页补充
    need_watch = (not raw_title) or (not tags_list) or (not date_str) or (not best_url)
    if need_watch:
        try:
            soup_watch = get_soup(video_url)
            if not raw_title:
                h3_watch = soup_watch.find('h3', class_='video-details-wrapper')
                if h3_watch:
                    raw_title = h3_watch.get_text(strip=True)
                else:
                    title_tag = soup_watch.find('title')
                    if title_tag:
                        raw_title = title_tag.get_text(strip=True)
                        if ' - Hanime1.me' in raw_title:
                            raw_title = raw_title.split(' - Hanime1.me')[0].strip()
            if not poster_url:
                video_tag = soup_watch.find('video', id='player')
                if video_tag:
                    p = video_tag.get('poster', '')
                    if p and p.startswith('//'):
                        p = 'https:' + p
                    poster_url = p
            if not date_str:
                date_candidate = extract_date_from_soup(soup_watch)
                if date_candidate:
                    date_str = date_candidate
            if not tags_list:
                tags_list = extract_tags_from_soup(soup_watch)
            if not best_url:
                best_url = _extract_video_url_from_watch_soup(soup_watch)
            logger.info("已从观看页补充元数据")
        except Exception as e:
            logger.warning(f"观看页补充失败: {e}")

    # 3. 兜底：重定向方式
    if not best_url:
        best_url = _extract_video_url_via_redirect(video_id)

    # 4. 最终兜底：跟随重定向到 hembed.com
    if not best_url:
        try:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(download_url, proxies=_get_proxies(), timeout=30)
            final_url = resp.url
            if final_url and 'hembed.com' in final_url:
                best_url = final_url
        except Exception:
            pass

    if not best_url:
        raise ValueError("所有方式均无法获取视频直链")

    return raw_title, poster_url, date_str, best_url, tags_list


def download_file(url: str, referer: str = BASE_URL, timeout: int = 120, retries=MAX_RETRIES, suffix: str = '.mp4') -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9",
        "Referer": referer,
    }
    last_exc = None
    for i in range(1, retries + 1):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            with req.get(url, headers=headers, proxies=_get_proxies(), stream=True, timeout=timeout) as r:
                r.raise_for_status()
                for chunk in r.iter_content(8192):
                    if chunk:
                        tmp.write(chunk)
            tmp.flush()
            tmp_path = tmp.name
            tmp.close()
            return tmp_path
        except Exception as e:
            tmp_path = tmp.name
            tmp.close()
            os.unlink(tmp_path)
            logger.warning(f"下载失败 (尝试 {i}/{retries}): {e}")
            last_exc = e
            time.sleep(5)
    raise last_exc


def send_video_pyrogram(video_path: str, thumb_path: Optional[str], caption: str):
    app = Client(":memory:", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING)
    try:
        with app:
            while True:
                try:
                    app.send_video(
                        chat_id=CHAT_ID,
                        video=video_path,
                        caption=caption,
                        thumb=thumb_path if thumb_path and os.path.exists(thumb_path) else None,
                        supports_streaming=True,
                    )
                    logger.info("视频发送成功")
                    break
                except FloodWait as e:
                    logger.warning(f"限流等待 {e.value} 秒")
                    time.sleep(e.value)
                except RPCError as e:
                    if "CHAT_WRITE_FORBIDDEN" in str(e):
                        logger.error("权限不足！请检查账号是否为频道管理员，以及 SESSION_STRING 和 CHAT_ID 是否正确。")
                    raise
    except Exception as e:
        logger.error(f"发送失败: {e}")
        raise


def process_video(video_info: dict) -> Optional[str]:
    video_url = video_info['video_url']
    cover_fallback = video_info.get('cover_url', '')
    vid = extract_video_id(video_url)
    logger.info(f"处理视频 ID={vid}")

    try:
        raw_title, poster_url, date_str, best_video_url, tags_list = parse_video_page_and_download(vid, video_url)

        # 第一行：百度翻译
        line1 = get_chinese_title(raw_title)
        # 第二行：原始标题（清理后）
        line2 = clean_title(raw_title)
        # 第三行：日期
        line3 = date_str if date_str else "日期未知"
        # 第四行：标签（已繁转简）
        tag_items = [f"#{tag}" for tag in tags_list[:5] if tag]
        line4 = ' '.join(tag_items) if tag_items else ""

        caption = f"{line1}\n{line2}\n{line3}\n{line4}".strip()
        logger.info(f"生成的 caption: {caption}")

        final_cover = poster_url if poster_url else cover_fallback
        video_path = download_file(best_video_url, referer=video_url)

        thumb_path = None
        if final_cover:
            try:
                thumb_path = download_file(final_cover, referer=video_url, suffix='.jpg')
            except Exception as e:
                logger.warning(f"封面下载失败: {e}")

        try:
            send_video_pyrogram(video_path, thumb_path, caption)
        finally:
            if os.path.exists(video_path):
                os.unlink(video_path)
            if thumb_path and os.path.exists(thumb_path):
                os.unlink(thumb_path)

        logger.info(f"视频处理完成: {line1}")
        return vid
    except Exception as e:
        logger.error(f"处理失败 {vid}: {e}")
        return None


# ---------- 主函数 ----------
def main():
    # 启动时校验必需环境变量
    validate_env()

    logger.info("====== Hanime1 -> Telegram 最新上市（全量）发布 ======")
    logger.info(f"代理配置: {'已设置' if PROXY else '未设置'}")

    seen_ids = load_seen_ids()
    logger.info(f"已发送记录数: {len(seen_ids)}")

    page_url = SEARCH_URL
    logger.info(f"正在获取页面: {page_url}")
    soup = get_soup(page_url)
    all_videos = parse_search_page(soup)  # 直接传 soup，不重复解析
    logger.info(f"页面上共有 {len(all_videos)} 个视频")

    # 从底部向上收集未发送的视频
    videos_to_send = []
    for video in reversed(all_videos):
        vid = extract_video_id(video['video_url'])
        if vid and vid not in seen_ids:
            videos_to_send.append(video)
            logger.info(f"发现新视频 ID={vid}")

    if not videos_to_send:
        logger.info("没有新视频需要发送。")
        return

    logger.info(f"找到 {len(videos_to_send)} 部新视频，开始发送...")
    success_count = 0
    for v in videos_to_send:
        vid = process_video(v)
        if vid:
            seen_ids.add(vid)
            success_count += 1
            # 每成功一个立即保存，防止中断后重新发送
            save_seen_ids(seen_ids)
        time.sleep(REQUEST_DELAY)

    # 运行结束，统一写盘翻译缓存（避免每翻译一条就全量读写一次 JSON）
    flush_translation_cache()

    logger.info(f"本次成功发送 {success_count} 部视频，任务结束。")


if __name__ == "__main__":
    main()
