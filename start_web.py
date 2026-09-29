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
from http.server import SimpleHTTPRequestHandler, HTTPServer

import db

SERVER_PORT = 58682
WEB_PORT = 58683
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(CURRENT_DIR, "web")
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

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
        # admin 权限：允许访问默认下载目录根目录及其下任何用户目录
        if user.get("username") == "admin":
            allowed_roots = [os.path.realpath(DEFAULT_DOWNLOAD_DIR)]
            if user.get("custom_save_dir"):
                allowed_roots.append(os.path.realpath(user["custom_save_dir"]))
            for root in allowed_roots:
                if os.path.commonpath([root, target]) == root:
                    return True
            return False

        # 普通用户：严格限定在其个人保存目录内，彻底防止跨目录与路径遍历穿越攻击
        user_root = os.path.realpath(get_user_work_dir(user))
        return os.path.commonpath([user_root, target]) == user_root
    except Exception:
        return False

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

def fetch_qualities(bvid, cid, duration, cookie=""):
    try:
        play_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=127&fnval=4048&fourk=1"
        headers = {
            "User-Agent": DEFAULT_UA,
            "Referer": "https://www.bilibili.com"
        }
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
        return qualities
    except Exception as e:
        print(f"[!] 获取画质列表失败: {e}", flush=True)
        return [
            {"qn": 120, "desc": "4K / 1080P 原画", "dfn": "4K 超清, 4K 超高清, 1080P 高码率, 1080P 高清", "dfn_tag": "原画", "badge": "", "badge_type": "free", "size": "自动", "is_audio": False},
            {"qn": 64, "desc": "720P 准高清", "dfn": "720P 高清, 720P 准高清", "dfn_tag": "720P 高清", "badge": "", "badge_type": "free", "size": "约 30 MB", "is_audio": False},
            {"qn": 32, "desc": "480P 标清", "dfn": "480P 清晰, 480P 标清", "dfn_tag": "480P 清晰", "badge": "", "badge_type": "free", "size": "约 18 MB", "is_audio": False},
            {"qn": 0, "desc": "仅下载音频 (M4A)", "dfn": "", "dfn_tag": "仅音频", "badge": "纯音频", "badge_type": "audio", "size": "约 8 MB", "is_audio": True}
        ]

def parse_bili_url(raw_url, cookie=None):
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
    if cookie is None:
        cookie = get_saved_cookie()
    headers = {"User-Agent": DEFAULT_UA}
    if cookie:
        headers["Cookie"] = cookie

    api_req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(api_req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise ValueError(data.get("message", "B站接口错误"))
        v = data["data"]
        cid = v.get("cid") or (v.get("pages") and v["pages"][0].get("cid")) or 0
        duration = v.get("duration", 0)
        qualities = fetch_qualities(v["bvid"], cid, duration, cookie)
        return {
            "title": v["title"],
            "pic": v["pic"].replace("http://", "https://"),
            "bvid": v["bvid"],
            "aid": v["aid"],
            "cid": cid,
            "duration": duration,
            "owner": v["owner"]["name"],
            "qualities": qualities
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
        if not token:
            return None
        return db.get_user_by_token(token)

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

        # 0. 封面图片代理 (彻底解决 B 站图片防盗链 403 问题)
        if path == "/api/image-proxy":
            img_url = query.get("url", [""])[0].strip()
            if not img_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                img_req = urllib.request.Request(img_url, headers={
                    "User-Agent": DEFAULT_UA,
                    "Referer": "https://www.bilibili.com/"
                })
                with urllib.request.urlopen(img_req, timeout=6) as img_resp:
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
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            if not user:
                return self.send_json(200, {"code": 0, "is_authenticated": False, "user": None})
            bili_cookie = user.get("bili_cookie", "")
            if not bili_cookie and user.get("username") == "admin":
                bili_cookie = get_saved_cookie()
            bili_status = get_user_info(bili_cookie)
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
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            if not user:
                return self.send_json(401, {"code": 401, "message": "未登录或登录已过期，请先登录系统"})

            file_path = ""
            task_id_str = query.get("id", [""])[0].strip()
            if task_id_str.isdigit():
                task = db.get_user_task_by_id(user["id"], int(task_id_str))
                if task:
                    work_dir = task["work_dir"] or get_user_work_dir(user)
                    match_info = find_specific_task_file(task["title"], task["dfn_tag"], task["expected_ext"], work_dir)
                    if match_info:
                        file_path = match_info["path"]

            if not file_path:
                req_path = query.get("path", [""])[0].strip()
                if req_path and is_path_safe_for_user(user, req_path):
                    file_path = req_path

            if not file_path or not os.path.isfile(file_path):
                return self.send_json(404, {"code": 404, "message": "文件未找到或已被移出保存目录"})

            is_stream = (path == "/api/file/stream")
            return self.send_file_range(file_path, is_stream=is_stream)

        # 3. 视频解析接口 (自动使用当前登录用户的 B 站 Cookie 获取专属画质)
        if path == "/api/parse":
            url = query.get("url", [""])[0].strip()
            if not url:
                return self.send_json(400, {"code": -1, "message": "缺少 url 参数"})
            user = self.get_current_user(query)
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            cookie = user.get("bili_cookie", "") if user else ""
            if not cookie and user and user.get("username") == "admin":
                cookie = get_saved_cookie()
            try:
                info = parse_bili_url(url, cookie=cookie)
                return self.send_json(200, {"code": 0, "data": info})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 4. 系统信息接口
        if path == "/api/info":
            user = self.get_current_user(query)
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            user_dir = get_user_work_dir(user) if user else DEFAULT_DOWNLOAD_DIR
            return self.send_json(200, {
                "default_download_dir": user_dir,
                "server_port": SERVER_PORT
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

        # 6. 轮询二维码扫码状态 (自动绑定至当前系统用户，或通过 B 站扫码直接自动登录/创建用户)
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
                    cookies = [f"{c.name}={c.value}" for c in cj]
                    cookie_str = "; ".join(cookies)

                    if "SESSDATA=" not in cookie_str and poll_data.get("url"):
                        u = poll_data["url"]
                        if "?" in u:
                            qs = u.split("?", 1)[1]
                            cookie_str = qs.replace("&", "; ")

                    user_info = get_user_info(cookie_str)
                    token = ""
                    user = self.get_current_user(query)

                    if user:
                        # 当前已有登录用户，直接绑定 B 站 Cookie
                        db.update_user_cookie(user["id"], cookie_str)
                        token = extract_token_from_request(self.headers, query)
                        if not token:
                            token = db.create_session(user["id"])
                    else:
                        # 当前未登录任何系统用户！通过 B 站扫码自动登录/注册独立账号
                        user, err = db.login_or_create_user_by_bili(cookie_str, user_info, DEFAULT_DOWNLOAD_DIR)
                        if user:
                            token = db.create_session(user["id"])

                    if not user or user.get("username") == "admin":
                        save_cookie_to_files(cookie_str)

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
        if path == "/api/user/status":
            user = self.get_current_user(query)
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            if not user:
                return self.send_json(200, {"is_login": False, "system_user": None})

            cookie = user.get("bili_cookie", "")
            if not cookie and user.get("username") == "admin":
                cookie = get_saved_cookie()
            info = get_user_info(cookie)
            info["cookie"] = cookie
            info["system_user"] = {
                "id": user["id"],
                "username": user["username"],
                "save_dir": get_user_work_dir(user)
            }
            return self.send_json(200, info)

        # 8. 打开本地保存目录 (仅限本地环境)
        if path == "/api/open-folder":
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
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            if not user:
                return self.send_json(401, {"code": 401, "message": "请先登录系统账号"})

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

            # 筛选属于该用户的正在下载任务
            user_running = []
            bb_running = bbdown_tasks.get("Running", [])
            for r in bb_running:
                r_url = r.get("Url") or ""
                r_aid = str(r.get("Aid") or "")
                r_title = r.get("Title") or ""

                matching_meta = None
                for mt in user_db_tasks:
                    if (r_aid and mt["aid"] == r_aid) or (r_url and mt["url"] == r_url) or (r_title and mt["title"] == r_title):
                        matching_meta = mt
                        break
                if matching_meta:
                    r_copy = dict(r)
                    r_copy["QualityLabel"] = matching_meta["quality_label"]
                    r_copy["DfnTag"] = matching_meta["dfn_tag"]
                    user_running.append(r_copy)

            # 筛选已完成任务并精准关联磁盘文件
            user_finished = []
            bb_finished_map = {}
            for f in bbdown_tasks.get("Finished", []):
                f_aid = str(f.get("Aid") or "")
                if f_aid:
                    bb_finished_map[f_aid] = f

            for mt in reversed(user_db_tasks):
                # 如果该任务当前正在运行队列中，则不重复出现在已完成列表中
                if any((r.get("Aid") and str(r["Aid"]) == mt["aid"]) or (r.get("Url") == mt["url"]) for r in user_running):
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

        # 10. 检查保存目录中是否已存在同名/同画质文件
        if path == "/api/check-file":
            user = self.get_current_user(query)
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            title = query.get("title", [""])[0].strip()
            dfn_tag = query.get("dfnTag", [""])[0].strip()
            expected_ext = query.get("expectedExt", [".mp4"])[0].strip()
            work_dir = query.get("workDir", [""])[0].strip()
            if not work_dir or not user or not is_path_safe_for_user(user, work_dir):
                work_dir = get_user_work_dir(user)

            match_info = find_specific_task_file(title, dfn_tag, expected_ext, work_dir)
            if match_info:
                return self.send_json(200, {
                    "code": 0,
                    "exists": True,
                    "file": match_info
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
        if path in ("/api/user/logout", "/api/user/logout-bili"):
            user = self.get_current_user()
            if user:
                db.update_user_cookie(user["id"], "")
                if user.get("username") == "admin":
                    clear_cookie_files()
            return self.send_json(200, {"code": 0, "message": "已解除 B 站账号绑定"})

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

        # 7. 添加下载任务 (自动隔离保存目录与 B 站 Cookie)
        if path == "/api/task/add":
            user = self.get_current_user()
            if not user and (self.client_address[0] in ("127.0.0.1", "::1", "localhost")):
                user = db.get_user_by_username("admin")
            if not user:
                return self.send_json(401, {"code": 401, "message": "请先登录系统账号"})

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

                # Cookie 优先级：请求显式传入 > 当前系统用户的专属 B 站 Cookie > 全局备用 Cookie
                cookie_to_use = req_data.get("Cookie") or user.get("bili_cookie") or (get_saved_cookie() if user.get("username") == "admin" else "")

                bbdown_payload = {
                    "Url": req_data.get("Url"),
                    "UseTvApi": req_data.get("UseTvApi", False),
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

        return super().do_POST()

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

    # 1. 启动 BBDown Api Server
    bbdown_proc = start_bbdown_server()
    time.sleep(1.2)

    # 2. 启动 Web 前端服务
    httpd = HTTPServer(("0.0.0.0", WEB_PORT), WebUIHandler)
    print("=" * 65, flush=True)
    print("🎉 BiliDown Web UI 多用户服务已成功启动！", flush=True)
    print(f"👉 浏览器访问: http://localhost:{WEB_PORT}", flush=True)
    print(f"📂 默认根保存路径: {DEFAULT_DOWNLOAD_DIR}", flush=True)
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
