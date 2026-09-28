#!/usr/bin/env python3
"""
BiliDown Web UI 启动脚本 (集成 Web 扫码登录与 API 代理)
1. 启动 BBDown Api Server (端口 58682)
2. 启动 Web UI 静态服务 (端口 58683)
3. 提供 /api/parse, /api/login/qrcode, /api/login/poll, /api/user/status, /api/user/logout
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
from http.server import SimpleHTTPRequestHandler, HTTPServer

SERVER_PORT = 58682
WEB_PORT = 58683
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(CURRENT_DIR, "web")
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

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

REGISTERED_TASKS = []

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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

def parse_bili_url(raw_url):
    req = urllib.request.Request(raw_url, headers={"User-Agent": "curl/8.5.0"})
    try:
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
    cookie = get_saved_cookie()
    headers = {"User-Agent": "curl/8.5.0"}
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
    if not cookie_str:
        cookie_str = get_saved_cookie()
    if not cookie_str or "SESSDATA=" not in cookie_str:
        return {"is_login": False}

    api_url = "https://api.bilibili.com/x/web-interface/nav"
    headers = {
        "User-Agent": "curl/8.5.0",
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

def find_specific_task_file(title, dfn_tag, expected_ext, save_dir):
    if not os.path.exists(save_dir) or not title:
        return None
    clean_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:25]
    
    # 1. 尝试精确查找
    if dfn_tag:
        exact_name = f"{clean_title} [{dfn_tag}]{expected_ext}"
        exact_path = os.path.join(save_dir, exact_name)
        if os.path.isfile(exact_path):
            size_mb = os.path.getsize(exact_path) / (1024 * 1024)
            return {"name": exact_name, "path": exact_path, "size": f"{size_mb:.1f} MB"}

    # 2. 依据 title 前缀与特定画质标签模糊搜索
    tag_keywords = []
    if dfn_tag:
        dt = dfn_tag.lower()
        if "8k" in dt:
            tag_keywords.extend(["8k", "8000"])
        elif "4k" in dt:
            tag_keywords.extend(["4k", "4096"])
        elif "1080p" in dt or "1080" in dt:
            tag_keywords.extend(["1080p", "1080"])
        elif "720p" in dt or "720" in dt:
            tag_keywords.extend(["720p", "720"])
        elif "480p" in dt or "480" in dt:
            tag_keywords.extend(["480p", "480"])
        elif "360p" in dt or "360" in dt:
            tag_keywords.extend(["360p", "360"])
        elif "音频" in dt:
            tag_keywords.extend(["仅音频", "audio", ".m4a"])
        else:
            tag_keywords.append(dt)

    try:
        clean_title_sub = clean_title.lower()[:15]
        for fname in os.listdir(save_dir):
            if not fname.endswith((".mp4", ".m4a", ".mkv")):
                continue
            fname_lower = fname.lower()
            if clean_title_sub in fname_lower:
                if tag_keywords:
                    if any(kw in fname_lower for kw in tag_keywords):
                        fpath = os.path.join(save_dir, fname)
                        if os.path.isfile(fpath):
                            size_mb = os.path.getsize(fpath) / (1024 * 1024)
                            return {"name": fname, "path": fpath, "size": f"{size_mb:.1f} MB"}
                else:
                    fpath = os.path.join(save_dir, fname)
                    if os.path.isfile(fpath):
                        size_mb = os.path.getsize(fpath) / (1024 * 1024)
                        return {"name": fname, "path": fpath, "size": f"{size_mb:.1f} MB"}
    except Exception:
        pass
    return None

class WebUIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def log_message(self, format, *args):
        return

    def send_json(self, status_code, data):
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 根目录与首页处理：注入全局安全防崩溃脚本
        if path in ("/", "/index.html"):
            html_file = os.path.join(WEB_DIR, "index.html")
            if os.path.exists(html_file):
                try:
                    with open(html_file, "r", encoding="utf-8") as f:
                        html_content = f.read()
                    
                    guard = '<script>var currentUser = null; window.currentUser = null;</script>'
                    if 'var currentUser = null;' not in html_content:
                        html_content = html_content.replace('<head>', f'<head>\n    {guard}')
                    html_content = html_content.replace('const isVip = currentUser &&', 'const isVip = window.currentUser &&')
                    
                    raw_bytes = html_content.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw_bytes)))
                    self.end_headers()
                    self.wfile.write(raw_bytes)
                    return
                except Exception as e:
                    print(f"[!] 读取 index.html 失败: {e}", flush=True)

        # 0. 封面图片代理 (彻底解决 B 站图片防盗链 403 问题)
        if path == "/api/image-proxy":
            query = urllib.parse.parse_qs(parsed.query)
            img_url = query.get("url", [""])[0].strip()
            if not img_url:
                self.send_response(400)
                self.end_headers()
                return
            try:
                img_req = urllib.request.Request(img_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
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
            except Exception as e:
                self.send_response(500)
                self.end_headers()
            return

        # 1. 视频解析接口
        if path == "/api/parse":
            query = urllib.parse.parse_qs(parsed.query)
            url = query.get("url", [""])[0].strip()
            if not url:
                return self.send_json(400, {"code": -1, "message": "缺少 url 参数"})
            try:
                info = parse_bili_url(url)
                return self.send_json(200, {"code": 0, "data": info})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 2. 系统信息接口
        if path == "/api/info":
            return self.send_json(200, {
                "default_download_dir": DEFAULT_DOWNLOAD_DIR,
                "server_port": SERVER_PORT
            })

        # 3. 申请登录二维码
        if path == "/api/login/qrcode":
            gen_url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header"
            try:
                req = urllib.request.Request(gen_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return self.send_json(200, data)
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"生成二维码失败: {e}"})

        # 4. 轮询二维码扫码状态
        if path == "/api/login/poll":
            query = urllib.parse.parse_qs(parsed.query)
            qrcode_key = query.get("key", [""])[0].strip()
            if not qrcode_key:
                return self.send_json(400, {"code": -1, "message": "缺少 key 参数"})

            try:
                cj = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
                poll_url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}&source=main-fe-header"
                req = urllib.request.Request(poll_url, headers={"User-Agent": "Mozilla/5.0"})
                resp = opener.open(req, timeout=6)
                body = json.loads(resp.read().decode("utf-8"))

                poll_data = body.get("data", {})
                poll_code = poll_data.get("code")

                # code 0 表示扫码并确认登录成功
                if poll_code == 0:
                    # 从 CookieJar 中提取所有 cookie
                    cookies = [f"{c.name}={c.value}" for c in cj]
                    cookie_str = "; ".join(cookies)

                    # 如果从 cj 未捕获完全，尝试从 url 参数解析
                    if "SESSDATA=" not in cookie_str and poll_data.get("url"):
                        u = poll_data["url"]
                        if "?" in u:
                            qs = u.split("?", 1)[1]
                            cookie_str = qs.replace("&", "; ")

                    # 保存到 BBDown.data
                    save_cookie_to_files(cookie_str)

                    # 获取当前登录用户信息
                    user_info = get_user_info(cookie_str)
                    return self.send_json(200, {
                        "code": 0,
                        "message": "登录成功",
                        "cookie": cookie_str,
                        "user": user_info
                    })
                elif poll_code == 86090:
                    return self.send_json(200, {"code": 86090, "message": "扫码成功，请在手机上点击确认"})
                elif poll_code == 86038:
                    return self.send_json(200, {"code": 86038, "message": "二维码已过期"})
                else:
                    return self.send_json(200, {"code": 86101, "message": "等待扫码"})

            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"轮询状态失败: {e}"})

        # 5. 获取当前用户登录态
        if path == "/api/user/status":
            info = get_user_info()
            info["cookie"] = get_saved_cookie()
            return self.send_json(200, info)

        # 6. 打开本地保存目录
        if path == "/api/open-folder":
            query = urllib.parse.parse_qs(parsed.query)
            target_dir = query.get("dir", [DEFAULT_DOWNLOAD_DIR])[0].strip()
            if not os.path.exists(target_dir):
                target_dir = DEFAULT_DOWNLOAD_DIR
            try:
                subprocess.Popen(["xdg-open", target_dir])
                return self.send_json(200, {"code": 0, "message": "已在系统文件管理器中打开"})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        # 7. 任务列表代理与自愈接口
        if path == "/api/tasks":
            tasks_data = {"Running": [], "Finished": []}
            try:
                tasks_req = urllib.request.Request(f"http://127.0.0.1:{SERVER_PORT}/get-tasks/")
                with urllib.request.urlopen(tasks_req, timeout=3) as resp:
                    tasks_data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                # 出现 500 (通常是 BBDown 底层浮点 NaN 序列化崩溃)，自动调用清理自愈
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/remove-finished/", timeout=2)
                except:
                    pass

            # 为任务关联注册元数据并精确检测本地文件
            finished_tasks = tasks_data.get("Finished", [])
            for idx, t in enumerate(finished_tasks):
                title = t.get("Title") or ""
                url = t.get("Url") or ""

                meta = None
                matched_metas = [m for m in REGISTERED_TASKS if m.get("url") == url or (title and m.get("title") == title)]
                if matched_metas:
                    meta = matched_metas[idx] if idx < len(matched_metas) else matched_metas[-1]

                quality_label = meta.get("qualityLabel", "") if meta else ""
                dfn_tag = meta.get("dfnTag", "") if meta else ""
                work_dir = meta.get("workDir", DEFAULT_DOWNLOAD_DIR) if meta else DEFAULT_DOWNLOAD_DIR
                expected_ext = meta.get("expectedExt", ".mp4") if meta else ".mp4"

                t["QualityLabel"] = quality_label
                t["DfnTag"] = dfn_tag
                t["WorkDir"] = work_dir

                # 精准查找该任务对应的文件，绝不串用其他画质的文件
                match_info = find_specific_task_file(title, dfn_tag, expected_ext, work_dir)
                if match_info:
                    t["ActualFileName"] = match_info["name"]
                    t["ActualSize"] = match_info["size"]
                    t["ActualFilePath"] = match_info["path"]
                    t["FileExists"] = True
                else:
                    clean_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:40]
                    t["ActualFileName"] = f"{clean_title} [{dfn_tag}]{expected_ext}" if dfn_tag else f"{clean_title}{expected_ext}"
                    t["FileExists"] = False
                    if t.get("TotalDownloadedBytes", 0) > 0:
                        t["ActualSize"] = f"{t['TotalDownloadedBytes'] / (1024 * 1024):.1f} MB"
                    else:
                        t["ActualSize"] = "文件已移出目录"

            # 运行中任务也关联标签
            running_tasks = tasks_data.get("Running", [])
            for idx, t in enumerate(running_tasks):
                url = t.get("Url") or ""
                title = t.get("Title") or ""
                matched_metas = [m for m in REGISTERED_TASKS if m.get("url") == url or (title and m.get("title") == title)]
                if matched_metas:
                    meta = matched_metas[-1]
                    t["QualityLabel"] = meta.get("qualityLabel", "")
                    t["DfnTag"] = meta.get("dfnTag", "")

            return self.send_json(200, tasks_data)

        # 8. 清空任务列表
        if path == "/api/tasks/clear":
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/remove-finished/", timeout=3)
                REGISTERED_TASKS.clear()
                return self.send_json(200, {"code": 0, "message": "已清空任务记录"})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": str(e)})

        return super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        # 退出登录
        if path == "/api/user/logout":
            clear_cookie_files()
            return self.send_json(200, {"code": 0, "message": "已退出登录"})

        # 添加下载任务接口代理
        if path == "/api/task/add":
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len)
            try:
                req_data = json.loads(post_body.decode("utf-8"))

                # 记录任务元数据，便于任务列表精确显示各清晰度与对应文件
                task_meta = {
                    "url": req_data.get("Url", ""),
                    "title": req_data.get("Title", ""),
                    "qualityLabel": req_data.get("QualityLabel", ""),
                    "dfnTag": req_data.get("DfnTag", ""),
                    "workDir": req_data.get("WorkDir", DEFAULT_DOWNLOAD_DIR),
                    "expectedExt": req_data.get("ExpectedExt", ".mp4"),
                    "addTime": time.time()
                }
                REGISTERED_TASKS.append(task_meta)

                bbdown_payload = {
                    "Url": req_data.get("Url"),
                    "UseTvApi": req_data.get("UseTvApi", False),
                    "UserAgent": req_data.get("UserAgent", "curl/8.5.0"),
                    "DfnPriority": req_data.get("DfnPriority"),
                    "AudioOnly": req_data.get("AudioOnly", False),
                    "FilePattern": req_data.get("FilePattern", "<videoTitle> [<dfn>]"),
                    "WorkDir": req_data.get("WorkDir", DEFAULT_DOWNLOAD_DIR)
                }
                if req_data.get("Cookie"):
                    bbdown_payload["Cookie"] = req_data.get("Cookie")

                bbdown_req = urllib.request.Request(
                    f"http://127.0.0.1:{SERVER_PORT}/add-task",
                    data=json.dumps(bbdown_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(bbdown_req, timeout=5) as resp:
                    return self.send_json(200, {"code": 0, "message": "已成功添加至下载队列"})
            except Exception as e:
                return self.send_json(500, {"code": -1, "message": f"添加任务失败: {e}"})

        return super().do_POST()

def start_bbdown_server():
    cmd = ["BBDown", "dummy", "serve", "-l", f"http://127.0.0.1:{SERVER_PORT}"]
    print(f"[*] 正在启动 BBDown API 核心服务 (http://127.0.0.1:{SERVER_PORT})...", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc

def main():
    if not os.path.exists(os.path.join(WEB_DIR, "index.html")):
        print(f"[!] 找不到 Web UI 目录: {WEB_DIR}", flush=True)
        sys.exit(1)

    os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)

    # 1. 启动 BBDown Api Server
    bbdown_proc = start_bbdown_server()
    time.sleep(1.2)

    # 2. 启动 Web 前端服务
    httpd = HTTPServer(("0.0.0.0", WEB_PORT), WebUIHandler)
    print("=" * 65, flush=True)
    print("🎉 BiliDown Web UI 已成功启动！", flush=True)
    print(f"👉 浏览器访问: http://localhost:{WEB_PORT}", flush=True)
    print(f"📂 默认下载保存路径: {DEFAULT_DOWNLOAD_DIR}", flush=True)
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
