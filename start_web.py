#!/usr/bin/env python3
"""
BiliDown Web UI 启动脚本 (集成多用户系统、独立配置、任务隔离、手机端下载与在线播放)
1. 启动 BBDown Api Server (端口 58682)
2. 启动 Web UI 静态与 API 服务 (端口 58683)
3. 提供基于 SQLite (db.py) 的多用户认证、独立任务隔离、独立保存目录、RFC 7233 文件流式播放与下载
"""

import os
import sys
import time
import signal
import subprocess
import urllib.request
import urllib.parse
import json
import re
import http.cookiejar
import mimetypes
import threading
import tempfile
import shutil
import socket
import concurrent.futures
from http.server import SimpleHTTPRequestHandler, HTTPServer

import db

# 全局活跃任务映射：用于严格按发起客户端/用户隔离正在下载的任务队列，彻底防止跨设备混淆
# key: aid (str), value: { "user_id": int, "task_id": int, "dfn_tag": str, "quality_label": str, "work_dir": str, "start_time": float }
ACTIVE_RUNNING_TASKS = {}
ACTIVE_TASKS_LOCK = threading.Lock()

SERVER_PORT = 58682
WEB_PORT = 58683
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(CURRENT_DIR, "web")
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

# 访客/客户端临时缓存根目录 (严格独立，专门用于手机与浏览器直下任务的临时音视频合成，每日自动清理，绝不污染用户主目录)
TEMP_CACHE_DIR = os.environ.get("BBDOWN_CACHE_DIR", os.path.join(tempfile.gettempdir(), "bbdown_cache"))
CLIENT_CACHE_DIR = os.path.join(TEMP_CACHE_DIR, "clients")
os.makedirs(CLIENT_CACHE_DIR, exist_ok=True)

DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# 寻找 BBDown.data 存放路径
BBDOWN_DATA_PATHS = [
    os.path.join(os.path.expanduser("~/.local/bin"), "BBDown.data"),
    os.path.join(CURRENT_DIR, "BBDown.data")
]

def get_saved_cookie():
    for p in BBDOWN_DATA_PATHS:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    c = f.read().strip()
                    if "SESSDATA=" in c:
                        return c
            except Exception:
                pass
    return ""

def save_cookie_to_files(cookie_str):
    for p in BBDOWN_DATA_PATHS:
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(cookie_str)
        except Exception as e:
            print(f"[!] 写入 Cookie 到 {p} 失败: {e}", flush=True)

def clear_cookie_files():
    for p in BBDOWN_DATA_PATHS:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def extract_client_id_from_request(headers, query_params=None):
    cid = headers.get("X-Client-Id", "").strip()
    if cid:
        return cid
    if query_params and "client_id" in query_params:
        tokens = query_params["client_id"]
        if tokens and tokens[0].strip():
            return tokens[0].strip()
    cookie_header = headers.get("Cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("bilidown_client_id="):
                return part.split("=", 1)[1].strip()
    return ""

def extract_bili_cookie_from_request(headers, query_params=None):
    bili_c = headers.get("X-Bili-Cookie", "").strip()
    if bili_c:
        return urllib.parse.unquote(bili_c)
    if query_params and "bili_cookie" in query_params:
        tokens = query_params["bili_cookie"]
        if tokens and tokens[0].strip():
            return urllib.parse.unquote(tokens[0].strip())
    cookie_header = headers.get("Cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("bbdown_bili_cookie="):
                return urllib.parse.unquote(part.split("=", 1)[1].strip())
    return ""

def extract_token_from_request(headers, query_params=None):
    # 1. Authorization: Bearer <token>
    auth_header = headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    # 2. Cookie: bilidown_session=<token>
    cookie_header = headers.get("Cookie", "")
    if cookie_header:
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("bilidown_session="):
                return part.split("=", 1)[1].strip()

    # 3. Query param: ?token=<token> (支持流媒体播放与下载直链)
    if query_params and "token" in query_params:
        tokens = query_params["token"]
        if tokens and tokens[0].strip():
            return tokens[0].strip()
    return ""

def get_user_work_dir(user):
    if not user:
        return DEFAULT_DOWNLOAD_DIR
    custom_dir = user.get("custom_save_dir")
    if custom_dir:
        os.makedirs(custom_dir, exist_ok=True)
        return custom_dir
    user_dir = os.path.join(DEFAULT_DOWNLOAD_DIR, "users", user["username"])
    os.makedirs(user_dir, exist_ok=True)
    return user_dir

def is_path_safe_for_user(user, target_path):
    if not user or not target_path:
        return False
    try:
        target = os.path.realpath(target_path)
        if not os.path.exists(target):
            return False
        # admin 权限：允许访问默认下载目录根目录及其下任何用户目录，以及临时缓存目录
        if user.get("username") == "admin":
            allowed_roots = [os.path.realpath(DEFAULT_DOWNLOAD_DIR), os.path.realpath(TEMP_CACHE_DIR)]
            if user.get("custom_save_dir"):
                allowed_roots.append(os.path.realpath(user["custom_save_dir"]))
            for root in allowed_roots:
                if os.path.commonpath([root, target]) == root:
                    return True
            return False

        # 普通用户与访客：严格限定在其个人保存/缓存目录内，彻底防止跨目录与路径遍历穿越攻击
        user_root = os.path.realpath(get_user_work_dir(user))
        return os.path.commonpath([user_root, target]) == user_root
    except Exception:
        return False

def clean_temp_cache(max_age_seconds=86400):
    """
    仅针对访客客户端的临时下载缓存目录执行自动清理：
    1. 严格限定在 CLIENT_CACHE_DIR (/tmp/bbdown_cache/clients) 目录内
    2. 删除修改时间超过 max_age_seconds (默认 24 小时) 的音视频及分片缓存文件
    3. 清理已空的 client 访客子文件夹
    4. 同步将数据库中对应已过期的临时客户端任务标记为已清理，避免数据残留
    绝对不会触碰任何用户的正式保存目录 (DEFAULT_DOWNLOAD_DIR / users)
    """
    try:
        clients_cache_dir = os.path.realpath(CLIENT_CACHE_DIR)
        if not os.path.exists(clients_cache_dir):
            return {"cleaned_files": 0, "freed_bytes": 0}

        real_default = os.path.realpath(DEFAULT_DOWNLOAD_DIR)
        real_home = os.path.realpath(os.path.expanduser("~"))
        real_temp_cache = os.path.realpath(TEMP_CACHE_DIR)

        if clients_cache_dir in ("/", real_home, real_default) or not clients_cache_dir.startswith(real_temp_cache):
            print(f"[!] 缓存清理安全拦截：路径异常 {clients_cache_dir}", flush=True)
            return {"cleaned_files": 0, "freed_bytes": 0}

        now = time.time()
        cleaned_files = 0
        freed_bytes = 0

        for root, dirs, files in os.walk(clients_cache_dir, topdown=False):
            real_root = os.path.realpath(root)
            if os.path.commonpath([clients_cache_dir, real_root]) != clients_cache_dir:
                continue
            for f in files:
                file_path = os.path.join(root, f)
                try:
                    stat = os.stat(file_path)
                    if now - stat.st_mtime > max_age_seconds:
                        size = stat.st_size
                        os.remove(file_path)
                        cleaned_files += 1
                        freed_bytes += size
                except Exception as e:
                    print(f"[!] 清理缓存文件失败 {file_path}: {e}", flush=True)

            if real_root != clients_cache_dir:
                try:
                    if not os.listdir(real_root):
                        os.rmdir(real_root)
                except Exception:
                    pass

        try:
            db.clean_expired_client_tasks(now - max_age_seconds, clients_cache_dir)
        except Exception as e:
            print(f"[!] 清理数据库过期访客任务异常: {e}", flush=True)

        if cleaned_files > 0:
            mb = round(freed_bytes / (1024 * 1024), 2)
            print(f"[🧹 缓存自动清理] 成功清理 {cleaned_files} 个临时文件，释放磁盘空间 {mb} MB", flush=True)
        return {"cleaned_files": cleaned_files, "freed_bytes": freed_bytes}
    except Exception as e:
        print(f"[!] 临时缓存清理过程异常: {e}", flush=True)
        return {"cleaned_files": 0, "freed_bytes": 0}

def cache_cleaner_daemon():
    """
    后台定时守护线程：每日自动清理访客临时下载缓存 (每 1 小时检查一次超过 24 小时的缓存)
    """
    time.sleep(3)
    clean_temp_cache(max_age_seconds=24 * 3600)
    while True:
        time.sleep(3600)
        clean_temp_cache(max_age_seconds=24 * 3600)

def migrate_existing_clients_to_temp(old_download_dir, new_client_cache_dir):
    """
    将旧版存放在 ~/Downloads/clients 的数据一次性无损迁移至 /tmp/bbdown_cache/clients，
    并彻底删除旧的 ~/Downloads/clients 目录，保持用户下载文件夹整洁
    """
    old_clients_dir = os.path.join(old_download_dir, "clients")
    if os.path.isdir(old_clients_dir):
        os.makedirs(new_client_cache_dir, exist_ok=True)
        for item in os.listdir(old_clients_dir):
            src = os.path.join(old_clients_dir, item)
            dst = os.path.join(new_client_cache_dir, item)
            try:
                if os.path.isdir(src):
                    if os.path.exists(dst):
                        for f in os.listdir(src):
                            fsrc = os.path.join(src, f)
                            fdst = os.path.join(dst, f)
                            if not os.path.exists(fdst):
                                shutil.move(fsrc, fdst)
                        shutil.rmtree(src, ignore_errors=True)
                    else:
                        shutil.move(src, dst)
            except Exception as e:
                print(f"[!] 迁移客户端目录 {src} 异常: {e}", flush=True)
        try:
            if not os.listdir(old_clients_dir):
                os.rmdir(old_clients_dir)
            else:
                shutil.rmtree(old_clients_dir, ignore_errors=True)
            print(f"[✓] 已将原下载目录下的 clients 成功迁移至临时目录 {new_client_cache_dir}，旧目录已彻底清除", flush=True)
        except Exception as e:
            print(f"[!] 清除旧 clients 目录异常: {e}", flush=True)

        try:
            db.migrate_client_paths_in_db(old_clients_dir, new_client_cache_dir)
        except Exception as e:
            print(f"[!] 数据库迁移旧路径异常: {e}", flush=True)

QUALITY_CONFIG = {
    127: {"dfn_tag": "8K 超高清", "dfn_priority": "8K 超高清", "fallback_kbps": 11000},
    126: {"dfn_tag": "杜比视界", "dfn_priority": "杜比视界", "fallback_kbps": 8000},
    125: {"dfn_tag": "HDR 真彩", "dfn_priority": "HDR 真彩", "fallback_kbps": 8000},
    120: {"dfn_tag": "4K 超清", "dfn_priority": "4K 超清, 4K 超高清", "fallback_kbps": 7500},
    116: {"dfn_tag": "1080P 高帧率", "dfn_priority": "1080P 高帧率, 1080P 60帧, 1080P 高码率", "fallback_kbps": 1800},
    112: {"dfn_tag": "1080P 高码率", "dfn_priority": "1080P 高码率, 1080P 高帧率", "fallback_kbps": 1500},
    80:  {"dfn_tag": "1080P 高清", "dfn_priority": "1080P 高清", "fallback_kbps": 1000},
    74:  {"dfn_tag": "720P 高帧率", "dfn_priority": "720P 高帧率", "fallback_kbps": 800},
    64:  {"dfn_tag": "720P 高清", "dfn_priority": "720P 高清, 720P 准高清", "fallback_kbps": 550},
    32:  {"dfn_tag": "480P 清晰", "dfn_priority": "480P 清晰, 480P 标清", "fallback_kbps": 220},
    16:  {"dfn_tag": "360P 流畅", "dfn_priority": "360P 流畅", "fallback_kbps": 130},
}

def fetch_qualities(bvid, cid, duration, cookie="", is_bili_login=False):
    try:
        play_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=127&fnval=4048&fourk=1"
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.bilibili.com"
        }
        # 如果未登录 B 站，使用基础无凭据或基础 Cookie 请求免登录支持的流
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(play_url, headers=headers)
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", {})

        max_audio_bw = 0
        for a in data.get("dash", {}).get("audio", []):
            max_audio_bw = max(max_audio_bw, a.get("bandwidth", 0))

        video_bw = {}
        for v in data.get("dash", {}).get("video", []):
            qn = v.get("id")
            if qn not in video_bw:
                video_bw[qn] = v.get("bandwidth", 0)

        qualities = []
        for sf in data.get("support_formats", []):
            qn = sf.get("quality")
            desc = sf.get("new_description") or sf.get("display_desc") or ""
            reason = sf.get("can_watch_qn_reason", 0)

            cfg = QUALITY_CONFIG.get(qn, {
                "dfn_tag": desc,
                "dfn_priority": desc,
                "fallback_kbps": 1000
            })

            badge = ""
            badge_type = "free"
            if reason == 3:
                badge = "大会员"
                badge_type = "vip"
            elif qn >= 80:
                badge = "登录即享"
                badge_type = "login"

            bw = video_bw.get(qn, 0)
            if bw and duration:
                size_mb = round((bw + max_audio_bw) * duration / 8 / (1024 * 1024), 1)
            elif duration:
                size_mb = round((cfg["fallback_kbps"] * 1000 + max_audio_bw) * duration / 8 / (1024 * 1024), 1)
            else:
                size_mb = 0

            qualities.append({
                "qn": qn,
                "desc": desc,
                "dfn": cfg["dfn_priority"],
                "dfn_tag": cfg["dfn_tag"],
                "badge": badge,
                "badge_type": badge_type,
                "size": f"{size_mb} MB" if size_mb else "未知",
                "is_audio": False
            })

        # 添加音频流选项
        audio_mb = round(max_audio_bw * duration / 8 / (1024 * 1024), 1) if (max_audio_bw and duration) else round(192 * 1000 * duration / 8 / (1024 * 1024), 1)
        qualities.append({
            "qn": 0,
            "desc": "仅下载音频 (M4A)",
            "dfn": "",
            "dfn_tag": "仅音频",
            "badge": "纯音频",
            "badge_type": "audio",
            "size": f"{audio_mb} MB" if audio_mb else "高音质",
            "is_audio": True
        })

        # 用户核心要求: "没登录默认走tv，并且仅显示没登录可下载的画质，登陆了就可以下载高清"
        if not is_bili_login:
            qualities = [q for q in qualities if q["qn"] <= 80 or q.get("is_audio")]

        return qualities
    except Exception as e:
        print(f"[!] 获取画质列表失败: {e}", flush=True)
        fallback = [
            {"qn": 120, "desc": "4K / 1080P 原画", "dfn": "4K 超清, 4K 超高清, 1080P 高码率, 1080P 高清", "dfn_tag": "原画", "badge": "", "badge_type": "free", "size": "自动", "is_audio": False},
            {"qn": 80, "desc": "1080P 高清", "dfn": "1080P 高清", "dfn_tag": "1080P 高清", "badge": "", "badge_type": "free", "size": "约 50 MB", "is_audio": False},
            {"qn": 64, "desc": "720P 准高清", "dfn": "720P 高清, 720P 准高清", "dfn_tag": "720P 高清", "badge": "", "badge_type": "free", "size": "约 30 MB", "is_audio": False},
            {"qn": 32, "desc": "480P 标清", "dfn": "480P 清晰, 480P 标清", "dfn_tag": "480P 清晰", "badge": "", "badge_type": "free", "size": "约 18 MB", "is_audio": False},
            {"qn": 0, "desc": "仅下载音频 (M4A)", "dfn": "", "dfn_tag": "仅音频", "badge": "纯音频", "badge_type": "audio", "size": "约 8 MB", "is_audio": True}
        ]
        if not is_bili_login:
            fallback = [q for q in fallback if q["qn"] <= 80 or q.get("is_audio")]
        return fallback

def get_proxy_for_url(url):
    """
    智能代理探测：针对海外媒体与被墙域名 (Twitter / X)，自动按优先级探测代理通道
    1. 优先读取系统环境变量 (HTTP_PROXY / HTTPS_PROXY)
    2. 自动探测本地及局域网 Clash 常用端口 (127.0.0.1:7890, 172.168.200.192:7890 等)
    3. 普通国内站点 (如 B 站) 自动直连，避免无谓中转
    """
    need_proxy = any(domain in url for domain in ["twimg.com", "twitter.com", "x.com", "t.co", "fxtwitter.com", "vxtwitter.com"])
    if not need_proxy:
        return None

    # 1. 优先使用系统环境变量
    env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if env_proxy:
        return env_proxy

    # 2. 自动探测本地及局域网代理地址
    candidates = [
        ("127.0.0.1", 7890),
        ("172.168.200.192", 7890),
        ("127.0.0.1", 10809),
        ("127.0.0.1", 1080)
    ]
    for host, port in candidates:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex((host, port)) == 0:
                    return f"http://{host}:{port}"
        except Exception:
            pass

    return None

def get_url_opener(url=""):
    proxy = get_proxy_for_url(url)
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()

def is_twitter_url(url):
    if not url:
        return False
    u = url.lower()
    return any(k in u for k in ["x.com", "twitter.com", "t.co", "fxtwitter.com", "vxtwitter.com"])

def parse_twitter_url(raw_url):
    """
    解析 X (Twitter) 推文视频，支持多清晰度 (包括 2.7K / 4K 原画) 提取与真实体积预估
    """
    final_url = raw_url.strip()
    if "t.co/" in final_url:
        try:
            opener = get_url_opener(final_url)
            req = urllib.request.Request(final_url, headers={"User-Agent": DEFAULT_UA})
            with opener.open(req, timeout=5) as resp:
                final_url = resp.geturl()
        except Exception:
            pass

    m = re.search(r"(?:twitter|x)\.com/(?:[^/]+/status|i/status)/(\d+)", final_url)
    if not m:
        m = re.search(r"/status/(\d+)", final_url)
    if not m:
        raise ValueError("未识别到有效的 X (Twitter) 推文 ID")
    status_id = m.group(1)

    # 1. 优先调用 fxtwitter API 获取完整多清晰度与码率流
    opener = get_url_opener("https://api.fxtwitter.com")
    api_url = f"https://api.fxtwitter.com/status/{status_id}"
    req = urllib.request.Request(api_url, headers={"User-Agent": DEFAULT_UA})
    tweet_data = None
    try:
        with opener.open(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            tweet_data = data.get("tweet")
    except Exception as fx_err:
        print(f"[!] 请求 fxtwitter API 失败: {fx_err}，尝试切换备用源...", flush=True)

    # 2. 备用源: vxtwitter API
    if not tweet_data:
        try:
            vx_opener = get_url_opener("https://api.vxtwitter.com")
            vx_req = urllib.request.Request(f"https://api.vxtwitter.com/status/{status_id}", headers={"User-Agent": DEFAULT_UA})
            with vx_opener.open(vx_req, timeout=8) as resp:
                vx_json = json.loads(resp.read().decode("utf-8"))
                media_ext = vx_json.get("media_extended", [])
                first_media = media_ext[0] if media_ext else {}
                tweet_data = {
                    "id": status_id,
                    "text": vx_json.get("text", ""),
                    "author": {
                        "name": vx_json.get("user_name", "X User"),
                        "screen_name": vx_json.get("user_screen_name", "")
                    },
                    "media": {
                        "videos": [
                            {
                                "duration": (first_media.get("duration_millis", 0) / 1000.0),
                                "thumbnail_url": first_media.get("thumbnail_url", ""),
                                "width": first_media.get("size", {}).get("width", 0),
                                "height": first_media.get("size", {}).get("height", 0),
                                "variants": [
                                    {
                                        "url": first_media.get("url", ""),
                                        "content_type": "video/mp4",
                                        "bitrate": 0
                                    }
                                ]
                            }
                        ]
                    }
                }
        except Exception as vx_err:
            print(f"[!] 请求 vxtwitter 备用 API 失败: {vx_err}", flush=True)

    if not tweet_data:
        raise ValueError(f"无法获取推文 (ID: {status_id}) 的媒体信息，请检查网络连接或链接有效性")

    media = tweet_data.get("media", {})
    videos = media.get("videos") or [m for m in media.get("all", []) if m.get("type") == "video" or "mp4" in m.get("format", "")]
    if not videos:
        raise ValueError("该推文未包含可下载的视频媒体")

    # 检查是否指定了子视频索引 (如 /video/2)
    m_v = re.search(r"/video/(\d+)", final_url)
    target_video = videos[0]
    if m_v:
        idx = int(m_v.group(1)) - 1
        if 0 <= idx < len(videos):
            target_video = videos[idx]

    duration = float(target_video.get("duration", 0))
    thumbnail_url = target_video.get("thumbnail_url", "")

    # 提取所有 MP4 清晰度流
    raw_variants = target_video.get("variants", []) or target_video.get("formats", [])
    mp4_variants = []
    seen_urls = set()
    for v in raw_variants:
        v_url = v.get("url", "")
        if not v_url or v_url in seen_urls:
            continue
        content_type = v.get("content_type", "")
        if "mp4" not in content_type and ".mp4" not in v_url and "video/mp4" not in str(v):
            continue
        seen_urls.add(v_url)

        bitrate = v.get("bitrate", 0)
        m_res = re.search(r'/(\d+)x(\d+)/', v_url)
        if m_res:
            w, h = int(m_res.group(1)), int(m_res.group(2))
        else:
            w = target_video.get("width", 0)
            h = target_video.get("height", 0)

        mp4_variants.append({
            "url": v_url,
            "bitrate": bitrate,
            "w": w,
            "h": h
        })

    if not mp4_variants and target_video.get("url"):
        mp4_variants.append({
            "url": target_video["url"],
            "bitrate": 0,
            "w": target_video.get("width", 0),
            "h": target_video.get("height", 0)
        })

    # 按分辨率及码率降序排序
    mp4_variants.sort(key=lambda x: (x["w"] * x["h"], x["bitrate"]), reverse=True)

    # 并发 HEAD 请求探测真实文件体积
    def get_variant_size(v_item):
        u = v_item["url"]
        try:
            op = get_url_opener(u)
            r = urllib.request.Request(u, headers={"User-Agent": DEFAULT_UA}, method="HEAD")
            with op.open(r, timeout=3.0) as resp:
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    return int(cl)
        except Exception:
            pass
        if v_item["bitrate"] and duration:
            return int(v_item["bitrate"] * duration / 8)
        return None

    exact_sizes = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(mp4_variants) or 1)) as ex:
        future_to_url = {ex.submit(get_variant_size, v): v["url"] for v in mp4_variants}
        for fut in concurrent.futures.as_completed(future_to_url):
            u = future_to_url[fut]
            try:
                exact_sizes[u] = fut.result()
            except Exception:
                exact_sizes[u] = None

    qualities = []
    for idx, v in enumerate(mp4_variants):
        w, h = v["w"], v["h"]
        if w >= 3840 or h >= 2160:
            desc = f"4K 超清 ({w}x{h})"
            dfn_tag = "4K"
            qn = 120
        elif w >= 2560 or h >= 1440:
            desc = f"2.7K 原画 ({w}x{h})"
            dfn_tag = "2.7K"
            qn = 116
        elif w >= 1920 or h >= 1080:
            desc = f"1080P 全高清 ({w}x{h})"
            dfn_tag = "1080P"
            qn = 80
        elif w >= 1280 or h >= 720:
            desc = f"720P 高清 ({w}x{h})"
            dfn_tag = "720P"
            qn = 64
        elif w >= 640 or h >= 360:
            desc = f"360P 标清 ({w}x{h})"
            dfn_tag = "360P"
            qn = 32
        elif w > 0 and h > 0:
            desc = f"流畅画质 ({w}x{h})"
            dfn_tag = "流畅"
            qn = 16
        else:
            desc = f"原画视频"
            dfn_tag = "原画"
            qn = 80 - idx * 10

        b_size = exact_sizes.get(v["url"])
        if b_size:
            size_str = f"{b_size / (1024 * 1024):.1f} MB"
        else:
            size_str = "原画质"

        qualities.append({
            "qn": qn,
            "desc": desc,
            "dfn": desc,
            "dfn_tag": dfn_tag,
            "badge": "最高原画" if idx == 0 else "",
            "badge_type": "free",
            "size": size_str,
            "is_audio": False,
            "direct_url": v["url"]
        })

    # 音频流选项
    qualities.append({
        "qn": 0,
        "desc": "仅下载音频 (M4A)",
        "dfn": "",
        "dfn_tag": "仅音频",
        "badge": "纯音频",
        "badge_type": "audio",
        "size": "提取原声",
        "is_audio": True,
        "direct_url": mp4_variants[0]["url"] if mp4_variants else ""
    })

    author_obj = tweet_data.get("author", {})
    author_name = author_obj.get("name") or author_obj.get("screen_name") or "X 用户"
    screen_name = author_obj.get("screen_name", "")
    owner_str = f"{author_name} (@{screen_name})" if screen_name else author_name

    raw_text = tweet_data.get("text", "")
    clean_text = re.sub(r'https?://t\.co/\S+', '', raw_text)
    clean_text = re.sub(r'https?://\S+', '', clean_text)
    clean_text = re.sub(r'\s+', ' ', clean_text).strip()
    if not clean_text:
        clean_text = f"X_Post_{status_id}"
    title = clean_text[:100].strip()

    return {
        "platform": "twitter",
        "title": title,
        "raw_text": raw_text,
        "pic": thumbnail_url,
        "bvid": f"X_{status_id}",
        "aid": f"X_{status_id}",
        "cid": 0,
        "duration": int(duration),
        "owner": owner_str,
        "qualities": qualities,
        "is_bili_login": False
    }

def download_direct_file(direct_url, target_file, aid, user_id, task_id, title, dfn_tag, audio_only=False):
    """
    后台线程执行 X (Twitter) 直链原画下载或原声音频提取
    1. 动态自适应探测网络代理 (避免 twimg CDN 阻断)
    2. 分块写入与精确进度、速率计算 (实时同步至 ACTIVE_RUNNING_TASKS)
    3. 安全写入 (.part 临时文件，下载完成后原子重命名)
    4. 支持音视频分离提取 (若选择纯音频，自动调用 ffmpeg 转码为 m4a)
    """
    part_file = target_file + ".part"
    temp_mp4 = part_file if not audio_only else (target_file + ".temp.mp4")
    opener = get_url_opener(direct_url)
    req = urllib.request.Request(direct_url, headers={
        "User-Agent": DEFAULT_UA,
        "Referer": "https://x.com/"
    })

    start_time = time.time()
    last_calc_time = start_time
    last_calc_bytes = 0
    downloaded_bytes = 0
    total_bytes = 0

    try:
        os.makedirs(os.path.dirname(target_file), exist_ok=True)
        with opener.open(req, timeout=30) as resp:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                total_bytes = int(cl)

            chunk_size = 128 * 1024
            with open(temp_mp4, "wb") as f:
                while True:
                    with ACTIVE_TASKS_LOCK:
                        act = ACTIVE_RUNNING_TASKS.get(str(aid))
                        if not act or act.get("cancelled"):
                            print(f"[*] 任务 {aid} 已被用户取消下载", flush=True)
                            if os.path.exists(temp_mp4):
                                try:
                                    os.remove(temp_mp4)
                                except Exception:
                                    pass
                            return

                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded_bytes += len(chunk)

                    now = time.time()
                    elapsed = now - last_calc_time
                    if elapsed >= 0.4:
                        speed = int((downloaded_bytes - last_calc_bytes) / elapsed) if elapsed > 0 else 0
                        last_calc_time = now
                        last_calc_bytes = downloaded_bytes
                        progress = (downloaded_bytes / total_bytes) if total_bytes > 0 else 0.5

                        with ACTIVE_TASKS_LOCK:
                            aid_str = str(aid)
                            if aid_str in ACTIVE_RUNNING_TASKS:
                                ACTIVE_RUNNING_TASKS[aid_str]["progress"] = progress
                                ACTIVE_RUNNING_TASKS[aid_str]["speed"] = speed
                                ACTIVE_RUNNING_TASKS[aid_str]["downloaded_bytes"] = downloaded_bytes
                                ACTIVE_RUNNING_TASKS[aid_str]["total_bytes"] = total_bytes

        if audio_only:
            ffmpeg_cmd = ["ffmpeg", "-y", "-i", temp_mp4, "-vn", "-c:a", "copy", target_file]
            subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            if os.path.exists(temp_mp4):
                try:
                    os.remove(temp_mp4)
                except Exception:
                    pass
        else:
            if os.path.exists(target_file):
                try:
                    os.remove(target_file)
                except Exception:
                    pass
            shutil.move(temp_mp4, target_file)

        print(f"[✓] X 直链任务下载完成: {target_file} ({downloaded_bytes / (1024 * 1024):.2f} MB)", flush=True)

    except Exception as e:
        print(f"[!] X 直链下载异常: {e}", flush=True)
        for p in [part_file, target_file + ".temp.mp4"]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
    finally:
        with ACTIVE_TASKS_LOCK:
            aid_str = str(aid)
            if aid_str in ACTIVE_RUNNING_TASKS:
                del ACTIVE_RUNNING_TASKS[aid_str]

def parse_bili_url(raw_url, cookie="", is_bili_login=False):
    final_url = raw_url
    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        try:
            req = urllib.request.Request(raw_url, headers={"User-Agent": DEFAULT_UA})
            with urllib.request.urlopen(req, timeout=5) as resp:
                final_url = resp.geturl()
        except Exception:
            final_url = raw_url

    match = re.search(r"BV[0-9A-Za-z]{10}", final_url)
    aid_match = re.search(r"av(\d+)", final_url, re.IGNORECASE)

    if match:
        param = f"bvid={match.group(0)}"
    elif aid_match:
        param = f"aid={aid_match.group(1)}"
    else:
        raise ValueError("未识别到有效的 BV 号或 AV 号")

    api_url = f"https://api.bilibili.com/x/web-interface/view?{param}"
    base_cookie = cookie or get_saved_cookie()
    headers = {"User-Agent": DEFAULT_UA}
    if base_cookie:
        headers["Cookie"] = base_cookie

    api_req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(api_req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise ValueError(data.get("message", "B站接口错误"))
        v = data["data"]
        cid = v.get("cid") or (v.get("pages") and v["pages"][0].get("cid")) or 0
        duration = v.get("duration", 0)
        qualities = fetch_qualities(v["bvid"], cid, duration, cookie=cookie, is_bili_login=is_bili_login)
        return {
            "title": v["title"],
            "pic": v["pic"].replace("http://", "https://"),
            "bvid": v["bvid"],
            "aid": v["aid"],
            "cid": cid,
            "duration": duration,
            "owner": v["owner"]["name"],
            "qualities": qualities,
            "is_bili_login": is_bili_login
        }

def get_user_info(cookie_str=None):
    if cookie_str is None:
        cookie_str = get_saved_cookie()
    if not cookie_str or "SESSDATA=" not in cookie_str:
        return {"is_login": False}

    api_url = "https://api.bilibili.com/x/web-interface/nav"
    headers = {
        "User-Agent": DEFAULT_UA,
        "Cookie": cookie_str
    }
    req = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") == 0 and data["data"]["isLogin"]:
                u = data["data"]
                vip_label = u.get("vip_label", {}).get("text", "")
                if not vip_label and u.get("vipType", 0) > 0:
                    vip_label = "大会员"
                return {
                    "is_login": True,
                    "uname": u["uname"],
                    "face": u["face"].replace("http://", "https://"),
                    "vip_type": u.get("vipType", 0),
                    "vip_label": vip_label,
                    "level": u.get("level_info", {}).get("current_level", 0),
                    "mid": u.get("mid", 0)
                }
    except Exception as e:
        print(f"[!] 获取用户信息异常: {e}", flush=True)

    return {"is_login": False}

def get_bbdown_valid_title(title):
    if not title:
        return ""
    clean = re.sub(r'[\"<>|:\*?\\/\x00-\x1f]', '_', title)
    return clean.strip().rstrip('.').strip()

def normalize_title(t):
    if not t:
        return ""
    clean = get_bbdown_valid_title(t)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.lower()

def is_same_quality(q1, q2):
    if not q1 and not q2:
        return True
    if not q1 or not q2:
        return False
    q1 = q1.lower().replace(" ", "")
    q2 = q2.lower().replace(" ", "")
    if q1 == q2:
        return True
    aliases = [
        {"8k超高清", "8k"},
        {"4k超清", "4k超高清", "4k"},
        {"1080p高码率", "1080p高帧率", "1080p60帧", "1080p60"},
        {"1080p高清", "1080p"},
        {"720p高清", "720p准高清", "720p"},
        {"480p清晰", "480p标清", "480p"},
        {"360p流畅", "360p"},
        {"仅音频", "audio"}
    ]
    for s in aliases:
        if q1 in s and q2 in s:
            return True
    return False

FILE_NAME_PATTERN = re.compile(r"^(.*?)(?:\s*\[([^\[\]]+)\])?(?:\s*\((\d+)\))?\.(mp4|m4a|mkv)$", re.IGNORECASE)

def find_specific_task_file(title, dfn_tag, expected_ext, save_dir):
    if not os.path.exists(save_dir) or not title:
        return None

    clean_title = get_bbdown_valid_title(title)
    if not clean_title:
        return None

    copy_num = ""
    base_tag = dfn_tag or ""
    m_copy = re.search(r'\s*\((\d+)\)$', dfn_tag)
    if m_copy:
        copy_num = m_copy.group(1)
        base_tag = dfn_tag[:m_copy.start()].strip()

    exts = [expected_ext]
    for ext in [".mp4", ".m4a", ".mkv"]:
        if ext not in exts:
            exts.append(ext)

    # 1. 优先尝试全名精确直接读取
    candidate_names = []
    if base_tag:
        for ext in exts:
            if copy_num:
                candidate_names.append(f"{clean_title} [{base_tag}] ({copy_num}){ext}")
                candidate_names.append(f"{clean_title} [{base_tag}]({copy_num}){ext}")
            else:
                candidate_names.append(f"{clean_title} [{base_tag}]{ext}")
    if not copy_num:
        for ext in exts:
            candidate_names.append(f"{clean_title}{ext}")

    for cname in candidate_names:
        cpath = os.path.join(save_dir, cname)
        if os.path.isfile(cpath):
            size_mb = os.path.getsize(cpath) / (1024 * 1024)
            return {"name": cname, "path": cpath, "size": f"{size_mb:.1f} MB"}

    # 2. 严谨结构化文件名解析比对：标题必须完整一致，画质和副本号必须匹配，彻底杜绝系列剧集/相似标题误判
    target_norm_title = normalize_title(clean_title)
    try:
        for fname in os.listdir(save_dir):
            if not fname.endswith((".mp4", ".m4a", ".mkv")):
                continue
            m = FILE_NAME_PATTERN.match(fname)
            if not m:
                continue
            f_title, f_dfn, f_copy, _ = m.groups()
            # 标题必须完整一致（绝不使用截断前缀，防止同系列不同集数误判冲突）
            if normalize_title(f_title) != target_norm_title:
                continue
            # 副本序号必须一致
            if (f_copy or "") != copy_num:
                continue
            # 画质清晰度必须匹配
            if base_tag and not is_same_quality(base_tag, f_dfn):
                continue

            fpath = os.path.join(save_dir, fname)
            if os.path.isfile(fpath):
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                return {"name": fname, "path": fpath, "size": f"{size_mb:.1f} MB"}
    except Exception as e:
        print(f"[!] 查找任务文件异常: {e}", flush=True)

    return None

class WebUIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def log_message(self, format, *args):
        return

    def send_json(self, status_code, data, extra_headers=None):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def get_current_user(self, query=None):
        token = extract_token_from_request(self.headers, query)
        if token:
            user = db.get_user_by_token(token)
            if user:
                return user

        client_id = extract_client_id_from_request(self.headers, query)
        if not client_id:
            client_id = f"ip_{self.client_address[0]}"

        user, _ = db.get_or_create_client_user(client_id, DEFAULT_DOWNLOAD_DIR, CLIENT_CACHE_DIR)
        return user

    def send_file_range(self, file_path, is_stream=False):
        """
        支持 RFC 7233 HTTP Range 分段请求，完美适配 iOS Safari、Android Chrome、浏览器视频在线预览与文件直接下载
        """
        if not os.path.isfile(file_path):
            self.send_response(404)
            self.end_headers()
            return

        file_size = os.path.getsize(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".mp4":
            content_type = "video/mp4"
        elif ext == ".m4a":
            content_type = "audio/mp4"
        elif ext == ".mkv":
            content_type = "video/x-matroska"
        else:
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"

        range_header = self.headers.get("Range")
        start = 0
        end = file_size - 1
        is_partial = False

        if range_header:
            match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if match:
                s_str, e_str = match.groups()
                if s_str:
                    start = int(s_str)
                    if e_str:
                        end = int(e_str)
                elif e_str:
                    # 尾部范围，如 bytes=-500 表示最后 500 字节
                    suffix = int(e_str)
                    start = max(0, file_size - suffix)

                if start >= file_size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return

                end = min(end, file_size - 1)
                is_partial = True

        length = end - start + 1

        if is_partial:
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        else:
            self.send_response(200)

        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")

        fname = os.path.basename(file_path)
        encoded_fname = urllib.parse.quote(fname)
        if is_stream:
            self.send_header("Content-Disposition", f'inline; filename="{encoded_fname}"; filename*=UTF-8\'\'{encoded_fname}')
        else:
            self.send_header("Content-Disposition", f'attachment; filename="{encoded_fname}"; filename*=UTF-8\'\'{encoded_fname}')

        self.end_headers()

        if getattr(self, "command", "GET") == "HEAD":
            return

        try:
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = length
                chunk_size = 64 * 1024  # 64 KB 块缓冲
                while remaining > 0:
                    read_len = min(chunk_size, remaining)
                    data = f.read(read_len)
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print(f"[!] 传输文件异常: {e}", flush=True)

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/"):
            return self.do_GET()
        return super().do_HEAD()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # 0. 封面图片代理 (解决 B 站防盗链及 X/Twitter 缩略图代理)
        if path == "/api/image-proxy":
            img_url = query.get("url", [""])[0].strip()
            if not img_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                opener = get_url_opener(img_url)
                headers = {"User-Agent": DEFAULT_UA}
                if "twimg.com" in img_url or "twitter.com" in img_url or "x.com" in img_url:
                    headers["Referer"] = "https://x.com/"
                else:
                    headers["Referer"] = "https://www.bilibili.com/"

                img_req = urllib.request.Request(img_url, headers=headers)
                with opener.open(img_req, timeout=8) as img_resp:
                    content_type = img_resp.headers.get("Content-Type", "image/jpeg")
                    img_data = img_resp.read()
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(img_data)
            except Exception:
                self.send_response(500)
                self.end_headers()
            return

        # 1. 认证状态检查接口
        if path == "/api/auth/me":
            user = self.get_current_user(query)
            if not user:
                return self.send_json(200, {"code": 0, "is_authenticated": False, "user": None})
            bili_cookie = extract_bili_cookie_from_request(self.headers, query) or user.get("bili_cookie", "")
            bili_status = get_user_info(bili_cookie) if bili_cookie else {"is_login": False}
            work_dir = get_user_work_dir(user)
            return self.send_json(200, {
                "code": 0,
                "is_authenticated": True,
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "save_dir": work_dir,
                    "created_at": user["created_at"],
                    "bili": bili_status
                }
            })

        # 2. 手机端与远程浏览器文件直接下载 & 在线流式播放
        if path in ("/api/file/download", "/api/file/stream"):
            user = self.get_current_user(query)
            file_path = ""
            task_id_str = query.get("id", [""])[0].strip()
            if task_id_str.isdigit():
                task_id = int(task_id_str)
                task = db.get_user_task_by_id(user["id"], task_id) if user else None
                if not task:
                    task = db.get_task_by_id(task_id)
                if task:
                    work_dir = task.get("work_dir") or (get_user_work_dir(user) if user else DEFAULT_DOWNLOAD_DIR)
                    match_info = find_specific_task_file(task["title"], task["dfn_tag"], task["expected_ext"], work_dir)
                    if match_info:
                        file_path = match_info["path"]
                    elif os.path.isdir(work_dir):
                        clean_title = get_bbdown_valid_title(task["title"])
                        for fname in os.listdir(work_dir):
                            if fname.endswith((".mp4", ".m4a", ".mkv")):
                                if clean_title in fname or task["title"] in fname:
                                    file_path = os.path.join(work_dir, fname)
                                    break

            if not file_path:
                req_path = query.get("path", [""])[0].strip()
                if req_path:
                    real_req = os.path.realpath(req_path)
                    allowed_roots = [os.path.realpath(DEFAULT_DOWNLOAD_DIR), os.path.realpath(TEMP_CACHE_DIR)]
                    if any(os.path.commonpath([r, real_req]) == r for r in allowed_roots) and os.path.isfile(real_req):
                        file_path = real_req

            if not file_path or not os.path.isfile(file_path):
                return self.send_json(404, {"code": 404, "message": "文件未找到或已被移出保存目录"})

            is_stream = (path == "/api/file/stream")
            return self.send_file_range(file_path, is_stream=is_stream)

        # 3. 视频解析接口 (自动识别 B 站与 X / Twitter 平台)
        if path == "/api/parse":
            url = query.get("url", [""])[0].strip()
            if not url:
                return self.send_json(400, {"code": -1, "message": "缺少 url 参数"})

            # 判断是否为 X (Twitter) 链接
            if is_twitter_url(url):
                try:
                    info = parse_twitter_url(url)
                    return self.send_json(200, {"code": 0, "data": info})
                except Exception as e:
                    return self.send_json(500, {"code": -1, "message": f"X (Twitter) 解析失败: {e}"})

            user = self.get_current_user(query)
            cookie = extract_bili_cookie_from_request(self.headers, query) or (user.get("bili_cookie", "") if user else "")

            is_bili_login = bool(cookie and "SESSDATA=" in cookie)
            try:
                info = parse_bili_url(url, cookie=cookie, is_bili_login=is_bili_login)
                return self.send_json(200, {"code": 0, "data": info})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 4. 系统信息接口
        if path == "/api/info":
            user = self.get_current_user(query)
            user_dir = get_user_work_dir(user) if user else DEFAULT_DOWNLOAD_DIR
            public_url = ""
            pub_file = os.path.join(CURRENT_DIR, "public_url.txt")
            if os.path.exists(pub_file):
                try:
                    with open(pub_file, "r", encoding="utf-8") as pf:
                        public_url = pf.read().strip()
                except Exception:
                    pass
            return self.send_json(200, {
                "default_download_dir": user_dir,
                "server_port": SERVER_PORT,
                "public_url": public_url
            })

        # 4.1 访客临时缓存统计与手动清理接口 (仅对 clients 缓存有效)
        if path == "/api/cache/stats":
            clients_cache_dir = os.path.realpath(CLIENT_CACHE_DIR)
            total_size = 0
            file_count = 0
            if os.path.exists(clients_cache_dir):
                for root, _, files in os.walk(clients_cache_dir):
                    for f in files:
                        try:
                            fp = os.path.join(root, f)
                            total_size += os.path.getsize(fp)
                            file_count += 1
                        except Exception:
                            pass
            return self.send_json(200, {
                "code": 0,
                "cache_dir": CLIENT_CACHE_DIR,
                "file_count": file_count,
                "total_size_bytes": total_size,
                "total_size_mb": round(total_size / (1024 * 1024), 2)
            })

        if path == "/api/cache/clean":
            force = query.get("force", ["0"])[0] in ("1", "true")
            max_age = 0 if force else 24 * 3600
            res = clean_temp_cache(max_age_seconds=max_age)
            return self.send_json(200, {
                "code": 0,
                "message": f"缓存清理完成，已清理 {res['cleaned_files']} 个临时文件，释放 {round(res['freed_bytes'] / (1024 * 1024), 2)} MB 空间",
                "result": res
            })

        # 5. 申请 B 站登录二维码
        if path == "/api/login/qrcode":
            gen_url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header"
            try:
                req = urllib.request.Request(gen_url, headers={"User-Agent": DEFAULT_UA})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return self.send_json(200, data)
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"生成二维码失败: {e}"})

        # 6. 轮询二维码扫码状态 (稳定提取所有关键 Cookie 凭据)
        if path == "/api/login/poll":
            qrcode_key = query.get("key", [""])[0].strip()
            if not qrcode_key:
                return self.send_json(400, {"code": -1, "message": "缺少 key 参数"})

            try:
                cj = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                poll_url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}&source=main-fe-header"
                req = urllib.request.Request(poll_url, headers={"User-Agent": DEFAULT_UA})
                resp = opener.open(req, timeout=6)
                body = json.loads(resp.read().decode("utf-8"))

                poll_data = body.get("data", {})
                poll_code = poll_data.get("code")

                # code 0 表示扫码并确认登录成功
                if poll_code == 0:
                    cookie_map = {}
                    for c in cj:
                        cookie_map[c.name] = c.value
                    if poll_data.get("url") and "?" in poll_data["url"]:
                        qs = poll_data["url"].split("?", 1)[1]
                        for item in qs.split("&"):
                            if "=" in item:
                                k, v = item.split("=", 1)
                                cookie_map[k] = v

                    important_keys = ["SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid", "buvid3", "b_nut"]
                    cookie_parts = [f"{k}={cookie_map[k]}" for k in important_keys if k in cookie_map]
                    if not cookie_parts:
                        cookie_parts = [f"{k}={v}" for k, v in cookie_map.items()]
                    cookie_str = "; ".join(cookie_parts)

                    user_info = get_user_info(cookie_str)
                    user = self.get_current_user(query)

                    if user:
                        db.update_user_cookie(user["id"], cookie_str)
                        if user.get("username") == "admin":
                            save_cookie_to_files(cookie_str)

                    token = db.create_session(user["id"]) if user else ""
                    cookie_header = {"Set-Cookie": f"bilidown_session={token}; Path=/; Max-Age=2592000; SameSite=Lax"} if token else None

                    return self.send_json(200, {
                        "code": 0,
                        "message": "登录成功",
                        "token": token,
                        "cookie": cookie_str,
                        "user": user_info,
                        "system_user": {
                            "id": user["id"],
                            "username": user["username"],
                            "save_dir": get_user_work_dir(user)
                        } if user else None
                    }, extra_headers=cookie_header)
                elif poll_code == 86090:
                    return self.send_json(200, {"code": 86090, "message": "扫码成功，请在手机上点击确认"})
                elif poll_code == 86038:
                    return self.send_json(200, {"code": 86038, "message": "二维码已过期"})
                else:
                    return self.send_json(200, {"code": 86101, "message": "等待扫码"})

            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"轮询状态失败: {e}"})

        # 7. 获取当前用户 B 站登录态
        if path == "/api/user/status" or path == "/api/bili/status":
            user = self.get_current_user(query)
            cookie = extract_bili_cookie_from_request(self.headers, query) or (user.get("bili_cookie", "") if user else "")
            info = get_user_info(cookie) if cookie else {"is_login": False}
            info["cookie"] = cookie
            info["system_user"] = {
                "id": user["id"],
                "username": user["username"],
                "save_dir": get_user_work_dir(user)
            } if user else None
            return self.send_json(200, info)

        # 8. 打开本地保存目录 (仅限服务器本机环境)
        if path == "/api/open-folder":
            if self.client_address[0] not in ("127.0.0.1", "::1", "localhost"):
                return self.send_json(403, {"code": 403, "message": "移动端与远程访问模式不支持直接打开服务器目录，请在任务列表中点击【存至手机】直接保存"})
            user = self.get_current_user(query)
            user_work_dir = get_user_work_dir(user) if user else DEFAULT_DOWNLOAD_DIR
            target_dir = query.get("dir", [""])[0].strip() or user_work_dir
            if not os.path.exists(target_dir):
                target_dir = user_work_dir
            try:
                subprocess.Popen(["xdg-open", target_dir])
                return self.send_json(200, {"code": 0, "message": "已在系统文件管理器中打开"})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 9. 任务列表接口 (完全按用户隔离，跨重启持久化，支持移动端直链)
        if path == "/api/tasks":
            user = self.get_current_user(query)
            if not user:
                client_id = extract_client_id_from_request(self.headers, query)
                user, _ = db.get_or_create_client_user(client_id, DEFAULT_DOWNLOAD_DIR, CLIENT_CACHE_DIR)

            user_work_dir = get_user_work_dir(user)
            user_db_tasks = db.get_user_tasks(user["id"])

            bbdown_tasks = {"Running": [], "Finished": []}
            try:
                tasks_req = urllib.request.Request(f"http://127.0.0.1:{SERVER_PORT}/get-tasks/")
                with urllib.request.urlopen(tasks_req, timeout=3) as resp:
                    bbdown_tasks = json.loads(resp.read().decode("utf-8"))
            except Exception:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/remove-finished/", timeout=2)
                except:
                    pass

            # 筛选属于该用户的正在下载任务 (严格通过发起用户与任务映射隔离)
            user_running = []
            bb_running = bbdown_tasks.get("Running", [])
            bb_finished = bbdown_tasks.get("Finished", [])

            with ACTIVE_TASKS_LOCK:
                # 自动清理已进入 Finished 状态的活跃映射 (保留直链下载)
                for f in bb_finished:
                    f_aid = str(f.get("Aid") or "")
                    if f_aid in ACTIVE_RUNNING_TASKS and not ACTIVE_RUNNING_TASKS[f_aid].get("is_direct"):
                        del ACTIVE_RUNNING_TASKS[f_aid]

                for r in bb_running:
                    r_aid = str(r.get("Aid") or "")
                    active_info = ACTIVE_RUNNING_TASKS.get(r_aid)
                    # 严格隔离：只有当该运行中任务由当前用户发起时，才归入该用户的运行列表
                    if active_info and active_info["user_id"] == user["id"]:
                        r_copy = dict(r)
                        r_copy["QualityLabel"] = active_info["quality_label"]
                        r_copy["DfnTag"] = active_info["dfn_tag"]
                        r_copy["TaskId"] = active_info["task_id"]
                        user_running.append(r_copy)

                # 包含直链正在下载的任务 (如 X / Twitter)
                for aid_key, active_info in ACTIVE_RUNNING_TASKS.items():
                    if active_info.get("is_direct") and active_info["user_id"] == user["id"]:
                        user_running.append({
                            "Id": active_info["task_id"],
                            "TaskId": active_info["task_id"],
                            "Aid": aid_key,
                            "Title": active_info.get("title", ""),
                            "Url": active_info.get("url", ""),
                            "Progress": active_info.get("progress", 0.0),
                            "DownloadSpeed": active_info.get("speed", 0),
                            "TotalDownloadedBytes": active_info.get("downloaded_bytes", 0),
                            "QualityLabel": active_info["quality_label"],
                            "DfnTag": active_info["dfn_tag"],
                            "WorkDir": active_info["work_dir"],
                            "Platform": active_info.get("platform", "bilibili")
                        })

            # 筛选已完成任务并精准关联磁盘文件
            user_finished = []
            bb_finished_map = {}
            for f in bb_finished:
                f_aid = str(f.get("Aid") or "")
                if f_aid:
                    bb_finished_map[f_aid] = f

            for mt in reversed(user_db_tasks):
                # 如果该任务当前正在当前用户的运行队列中，则不重复出现在已完成列表中
                if any((r.get("TaskId") and r["TaskId"] == mt["id"]) or (r.get("Aid") and str(r["Aid"]) == mt["aid"]) for r in user_running):
                    continue

                work_dir = mt.get("work_dir") or user_work_dir
                match_info = find_specific_task_file(mt["title"], mt["dfn_tag"], mt["expected_ext"], work_dir)

                bb_info = bb_finished_map.get(mt["aid"])
                total_bytes = bb_info.get("TotalDownloadedBytes", 0) if bb_info else 0

                item = {
                    "Id": mt["id"],
                    "Aid": mt["aid"],
                    "Url": mt["url"],
                    "Title": mt["title"],
                    "QualityLabel": mt["quality_label"],
                    "DfnTag": mt["dfn_tag"],
                    "WorkDir": work_dir,
                    "TotalDownloadedBytes": total_bytes,
                    "AddTime": mt["add_time"],
                    "DownloadUrl": f"/api/file/download?id={mt['id']}",
                    "StreamUrl": f"/api/file/stream?id={mt['id']}"
                }

                if match_info:
                    item["ActualFileName"] = match_info["name"]
                    item["ActualSize"] = match_info["size"]
                    item["ActualFilePath"] = match_info["path"]
                    item["FileExists"] = True
                    item["IsSuccessful"] = True
                    item["IsFailed"] = False
                    item["StatusText"] = "已完成"
                else:
                    clean_title = get_bbdown_valid_title(mt["title"])
                    dfn = mt["dfn_tag"]
                    ext = mt["expected_ext"]
                    item["ActualFileName"] = f"{clean_title} [{dfn}]{ext}" if dfn else f"{clean_title}{ext}"
                    item["FileExists"] = False
                    if total_bytes > 0:
                        item["ActualSize"] = f"{total_bytes / (1024 * 1024):.1f} MB"
                        item["IsSuccessful"] = True
                        item["IsFailed"] = False
                        item["StatusText"] = "文件已移出保存目录"
                    else:
                        item["ActualSize"] = "0 B (未生成)"
                        item["IsSuccessful"] = False
                        item["IsFailed"] = True
                        item["StatusText"] = "下载失败 (未能获取到音视频流)"

                user_finished.append(item)

            return self.send_json(200, {"Running": user_running, "Finished": user_finished})

        # 10. 检查保存目录中是否已存在同名/同画质文件 (支持返回直链即时拉起下载)
        if path == "/api/check-file":
            user = self.get_current_user(query)
            title = query.get("title", [""])[0].strip()
            dfn_tag = query.get("dfnTag", [""])[0].strip()
            expected_ext = query.get("expectedExt", [".mp4"])[0].strip()
            work_dir = query.get("workDir", [""])[0].strip()
            if not work_dir or not user or not is_path_safe_for_user(user, work_dir):
                work_dir = get_user_work_dir(user)

            match_info = find_specific_task_file(title, dfn_tag, expected_ext, work_dir)
            if match_info:
                user_db_tasks = db.get_user_tasks(user["id"]) if user else []
                matched_task = next((t for t in reversed(user_db_tasks) if t["title"] == title and (not dfn_tag or t["dfn_tag"] == dfn_tag)), None)
                task_id = matched_task["id"] if matched_task else None
                dl_url = f"/api/file/download?id={task_id}" if task_id else f"/api/file/download?path={urllib.parse.quote(match_info['path'])}"
                st_url = f"/api/file/stream?id={task_id}" if task_id else f"/api/file/stream?path={urllib.parse.quote(match_info['path'])}"
                return self.send_json(200, {
                    "code": 0,
                    "exists": True,
                    "file": match_info,
                    "taskId": task_id,
                    "downloadUrl": dl_url,
                    "streamUrl": st_url
                })
            else:
                return self.send_json(200, {
                    "code": 0,
                    "exists": False
                })

        # 11. 移除单条任务记录
        if path == "/api/task/remove":
            user = self.get_current_user(query)
            if not user:
                return self.send_json(401, {"code": 401, "message": "请先登录系统账号"})
            target_id = query.get("id", [""])[0].strip() or query.get("aid", [""])[0].strip()
            aid = query.get("aid", [""])[0].strip()
            if target_id:
                db.remove_user_task(user["id"], target_id)
                if aid:
                    with ACTIVE_TASKS_LOCK:
                        if str(aid) in ACTIVE_RUNNING_TASKS:
                            ACTIVE_RUNNING_TASKS[str(aid)]["cancelled"] = True
                            del ACTIVE_RUNNING_TASKS[str(aid)]
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/remove-finished/{aid}", timeout=2)
                    except:
                        pass
            return self.send_json(200, {"code": 0, "message": "已移除记录"})

        # 12. 清空当前用户的任务列表
        if path == "/api/tasks/clear":
            user = self.get_current_user(query)
            if not user:
                return self.send_json(401, {"code": 401, "message": "请先登录系统账号"})
            db.clear_user_tasks(user["id"])
            return self.send_json(200, {"code": 0, "message": "已清空个人下载记录"})

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 1. 用户注册接口
        if path == "/api/auth/register":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req = json.loads(post_body.decode("utf-8"))
                username = req.get("username", "")
                password = req.get("password", "")
                new_user, err = db.register_user(username, password, DEFAULT_DOWNLOAD_DIR)
                if err:
                    return self.send_json(400, {"code": -1, "message": err})
                token = db.create_session(new_user["id"])
                cookie_header = {"Set-Cookie": f"bilidown_session={token}; Path=/; Max-Age=2592000; SameSite=Lax"}
                return self.send_json(200, {
                    "code": 0,
                    "message": "注册成功",
                    "token": token,
                    "user": {
                        "id": new_user["id"],
                        "username": new_user["username"],
                        "save_dir": new_user["custom_save_dir"]
                    }
                }, extra_headers=cookie_header)
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"注册失败: {e}"})

        # 2. 用户登录接口
        if path == "/api/auth/login":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req = json.loads(post_body.decode("utf-8"))
                username = req.get("username", "")
                password = req.get("password", "")
                user, err = db.authenticate_user(username, password)
                if err:
                    return self.send_json(400, {"code": -1, "message": err})
                token = db.create_session(user["id"])
                cookie_header = {"Set-Cookie": f"bilidown_session={token}; Path=/; Max-Age=2592000; SameSite=Lax"}
                return self.send_json(200, {
                    "code": 0,
                    "message": "登录成功",
                    "token": token,
                    "user": {
                        "id": user["id"],
                        "username": user["username"],
                        "save_dir": get_user_work_dir(user)
                    }
                }, extra_headers=cookie_header)
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"登录失败: {e}"})

        # 3. 退出系统账号登录
        if path == "/api/auth/logout":
            token = extract_token_from_request(self.headers)
            if token:
                db.delete_session(token)
            cookie_header = {"Set-Cookie": "bilidown_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax"}
            return self.send_json(200, {"code": 0, "message": "已退出登录"}, extra_headers=cookie_header)

        # 4. 解绑/退出 B 站账号
        if path in ("/api/bili/logout", "/api/user/logout-bili", "/api/user/logout"):
            user = self.get_current_user()
            if user:
                db.update_user_cookie(user["id"], "")
                if user.get("username") == "admin":
                    clear_cookie_files()
            return self.send_json(200, {"code": 0, "message": "已解除 B 站账号绑定"})

        # 4.5 手动输入 B 站 Cookie 登录
        if path == "/api/bili/cookie-login":
            user = self.get_current_user()
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req = json.loads(post_body.decode("utf-8"))
                cookie_str = req.get("cookie", "").strip()
                if not cookie_str:
                    return self.send_json(400, {"code": -1, "message": "Cookie 不能为空"})
                info = get_user_info(cookie_str)
                if not info.get("is_login"):
                    return self.send_json(400, {"code": -1, "message": "验证失败：该 Cookie 无效或已过期，请确保包含有效 SESSDATA"})
                if user:
                    db.update_user_cookie(user["id"], cookie_str)
                    if user.get("username") == "admin":
                        save_cookie_to_files(cookie_str)
                return self.send_json(200, {"code": 0, "message": "登录成功", "cookie": cookie_str, "user": info})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 5. 更新保存目录
        if path == "/api/user/save-dir":
            user = self.get_current_user()
            if not user:
                return self.send_json(401, {"code": 401, "message": "请先登录系统账号"})
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req = json.loads(post_body.decode("utf-8"))
                new_dir = req.get("save_dir", "").strip()
                if not new_dir:
                    new_dir = os.path.join(DEFAULT_DOWNLOAD_DIR, "users", user["username"])
                if user.get("username") != "admin":
                    user_base = os.path.realpath(os.path.join(DEFAULT_DOWNLOAD_DIR, "users", user["username"]))
                    target = os.path.realpath(new_dir)
                    if os.path.commonpath([user_base, target]) != user_base:
                        return self.send_json(400, {"code": -1, "message": f"普通用户仅允许在专属目录下设置子目录：{user_base}"})
                os.makedirs(new_dir, exist_ok=True)
                db.update_user_save_dir(user["id"], new_dir)
                return self.send_json(200, {"code": 0, "message": "保存路径更新成功", "save_dir": new_dir})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 6. 手动设置当前用户的 B 站 Cookie
        if path == "/api/user/bili-cookie":
            user = self.get_current_user()
            if not user:
                return self.send_json(401, {"code": 401, "message": "请先登录系统账号"})
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req = json.loads(post_body.decode("utf-8"))
                cookie_str = req.get("cookie", "").strip()
                db.update_user_cookie(user["id"], cookie_str)
                if user.get("username") == "admin":
                    save_cookie_to_files(cookie_str)
                return self.send_json(200, {"code": 0, "message": "Cookie 设置成功"})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 7. 添加下载任务 (自动隔离保存目录与 B 站 Cookie，未登录默认走 TV API)
        if path == "/api/task/add":
            user = self.get_current_user()
            if not user:
                client_id = extract_client_id_from_request(self.headers)
                user, _ = db.get_or_create_client_user(client_id, DEFAULT_DOWNLOAD_DIR, CLIENT_CACHE_DIR)

            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req_data = json.loads(post_body.decode("utf-8"))
                work_dir = get_user_work_dir(user)
                title = req_data.get("Title", "")
                dfn_tag = req_data.get("DfnTag", "")
                expected_ext = req_data.get("ExpectedExt", ".mp4")
                overwrite = req_data.get("overwrite", False)
                conflict_mode = req_data.get("conflictMode", "normal")

                file_pattern = req_data.get("FilePattern", "<videoTitle> [<dfn>]")

                # 处理同名文件逻辑
                if overwrite or conflict_mode == "overwrite":
                    existing = find_specific_task_file(title, dfn_tag, expected_ext, work_dir)
                    if existing and os.path.isfile(existing["path"]):
                        try:
                            os.remove(existing["path"])
                            print(f"[*] 覆盖重新下载：已移除旧文件 {existing['path']}", flush=True)
                        except Exception as rm_err:
                            print(f"[!] 移除旧文件失败: {rm_err}", flush=True)
                    if req_data.get("Aid"):
                        db.remove_user_task(user["id"], req_data["Aid"])
                elif conflict_mode == "rename":
                    clean_title = get_bbdown_valid_title(title)
                    counter = 1
                    while True:
                        test_name = f"{clean_title} [{dfn_tag}] ({counter}){expected_ext}"
                        if not os.path.exists(os.path.join(work_dir, test_name)):
                            break
                        counter += 1
                    if "<dfn>" in file_pattern:
                        file_pattern = f"<videoTitle> [<dfn>] ({counter})"
                    else:
                        file_pattern = f"{file_pattern} ({counter})"
                    dfn_tag = f"{dfn_tag} ({counter})"

                aid = req_data.get("Aid")
                direct_url = req_data.get("DirectUrl")
                platform = req_data.get("Platform", "bilibili")

                # 处理 X (Twitter) 直链任务
                if platform == "twitter" or direct_url:
                    clean_title = get_bbdown_valid_title(title)
                    if "<dfn>" in file_pattern or "[<dfn>]" in file_pattern:
                        target_filename = f"{clean_title} [{dfn_tag}]{expected_ext}" if dfn_tag else f"{clean_title}{expected_ext}"
                    else:
                        target_filename = f"{clean_title}{expected_ext}"
                    target_file_path = os.path.join(work_dir, target_filename)

                    if not aid:
                        aid = f"X_{int(time.time() * 1000)}"
                    elif not str(aid).startswith("X_"):
                        aid = f"X_{aid}"

                    task_meta = {
                        "url": req_data.get("Url", ""),
                        "aid": str(aid),
                        "title": title,
                        "qualityLabel": req_data.get("QualityLabel", ""),
                        "dfnTag": dfn_tag,
                        "workDir": work_dir,
                        "expectedExt": expected_ext,
                        "filePattern": file_pattern,
                        "addTime": time.time()
                    }
                    task_id = db.add_user_task(user["id"], task_meta)

                    with ACTIVE_TASKS_LOCK:
                        ACTIVE_RUNNING_TASKS[str(aid)] = {
                            "user_id": user["id"],
                            "task_id": task_id,
                            "dfn_tag": dfn_tag,
                            "quality_label": req_data.get("QualityLabel") or dfn_tag,
                            "work_dir": work_dir,
                            "start_time": time.time(),
                            "is_direct": True,
                            "platform": "twitter",
                            "progress": 0.0,
                            "speed": 0,
                            "downloaded_bytes": 0,
                            "total_bytes": 0,
                            "title": title,
                            "url": req_data.get("Url", ""),
                            "direct_url": direct_url,
                            "audio_only": req_data.get("AudioOnly", False)
                        }

                    threading.Thread(
                        target=download_direct_file,
                        args=(direct_url, target_file_path, str(aid), user["id"], task_id, title, dfn_tag, req_data.get("AudioOnly", False)),
                        daemon=True
                    ).start()

                    return self.send_json(200, {
                        "code": 0,
                        "message": "已成功启动 X (Twitter) 原画下载",
                        "taskId": task_id
                    })

                if aid:
                    try:
                        urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/remove-finished/{aid}", timeout=2)
                    except Exception:
                        pass

                # 记录任务元数据至当前用户专属的 SQLite 任务表中
                task_meta = {
                    "url": req_data.get("Url", ""),
                    "aid": str(aid or ""),
                    "title": title,
                    "qualityLabel": req_data.get("QualityLabel", ""),
                    "dfnTag": dfn_tag,
                    "workDir": work_dir,
                    "expectedExt": expected_ext,
                    "filePattern": file_pattern,
                    "addTime": time.time()
                }
                task_id = db.add_user_task(user["id"], task_meta)

                if aid:
                    with ACTIVE_TASKS_LOCK:
                        ACTIVE_RUNNING_TASKS[str(aid)] = {
                            "user_id": user["id"],
                            "task_id": task_id,
                            "dfn_tag": dfn_tag,
                            "quality_label": req_data.get("QualityLabel") or dfn_tag,
                            "work_dir": work_dir,
                            "start_time": time.time()
                        }

                # Cookie 优先级：请求显式传入 > 请求头携带的 B站 Cookie > 当前客户端绑定的 B 站 Cookie
                cookie_to_use = req_data.get("Cookie") or extract_bili_cookie_from_request(self.headers) or user.get("bili_cookie")

                # 用户核心需求: "没登录默认走tv，并且仅显示没登录可下载的画质，登陆了就可以下载高清"
                use_tv_api = True if not cookie_to_use else req_data.get("UseTvApi", False)

                bbdown_payload = {
                    "Url": req_data.get("Url"),
                    "UseTvApi": use_tv_api,
                    "UserAgent": DEFAULT_UA,
                    "DfnPriority": req_data.get("DfnPriority"),
                    "AudioOnly": req_data.get("AudioOnly", False),
                    "FilePattern": file_pattern,
                    "WorkDir": work_dir
                }
                if cookie_to_use:
                    bbdown_payload["Cookie"] = cookie_to_use

                bbdown_req = urllib.request.Request(
                    f"http://127.0.0.1:{SERVER_PORT}/add-task",
                    data=json.dumps(bbdown_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(bbdown_req, timeout=5) as resp:
                    return self.send_json(200, {
                        "code": 0,
                        "message": "已成功添加至下载队列",
                        "taskId": task_id
                    })
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"添加任务失败: {e}"})

        return self.send_json(404, {"code": 404, "message": "API endpoint not found"})

def start_bbdown_server():
    cmd = ["BBDown", "dummy", "serve", "-l", f"http://127.0.0.1:{SERVER_PORT}"]
    print(f"[*] 正在启动 BBDown API 核心服务 (http://127.0.0.1:{SERVER_PORT})...", flush=True)
    log_file = open("/tmp/bbdown_server.log", "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
    return proc

def main():
    if not os.path.exists(os.path.join(WEB_DIR, "index.html")):
        print(f"[!] 找不到 Web UI 目录: {WEB_DIR}", flush=True)
        sys.exit(1)

    os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)

    # 0. 初始化数据库 (建表并创建初始管理员 admin / admin123)
    db.init_db(DEFAULT_DOWNLOAD_DIR)

    # 迁移旧版本存在于 ~/Downloads/clients 中的目录至新的临时缓存目录，并清理旧目录
    migrate_existing_clients_to_temp(DEFAULT_DOWNLOAD_DIR, CLIENT_CACHE_DIR)

    # 启动后台每日缓存自动清理守护线程 (仅针对临时缓存，严格隔离正式用户目录)
    cleaner_thread = threading.Thread(target=cache_cleaner_daemon, daemon=True)
    cleaner_thread.start()

    # 1. 启动 BBDown Api Server
    bbdown_proc = start_bbdown_server()
    time.sleep(1.2)

    # 2. 启动 Web 前端服务
    httpd = HTTPServer(("0.0.0.0", WEB_PORT), WebUIHandler)
    print("=" * 65, flush=True)
    print("🎉 BiliDown Web UI 多用户服务已成功启动！", flush=True)
    print(f"👉 浏览器访问: http://localhost:{WEB_PORT}", flush=True)
    print(f"📂 默认根保存路径: {DEFAULT_DOWNLOAD_DIR}", flush=True)
    print(f"🧹 访客临时缓存路径: {CLIENT_CACHE_DIR} (每日自动清理)", flush=True)
    print(f"🔐 默认管理员账号: admin / admin123", flush=True)
    print("=" * 65, flush=True)
    print("按 Ctrl+C 停止服务...", flush=True)

    def cleanup(sig=None, frame=None):
        print("\n[*] 正在关闭服务...", flush=True)
        try:
            httpd.server_close()
        except:
            pass
        try:
            bbdown_proc.terminate()
            bbdown_proc.wait(timeout=3)
        except:
            bbdown_proc.kill()
        print("[✓] 已退出。", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
