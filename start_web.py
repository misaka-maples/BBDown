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
        return {
            "title": v["title"],
            "pic": v["pic"].replace("http://", "https://"),
            "bvid": v["bvid"],
            "aid": v["aid"],
            "duration": v["duration"],
            "owner": v["owner"]["name"]
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

def find_matching_files(title, save_dir):
    matches = []
    if not os.path.exists(save_dir) or not title:
        return matches
    clean_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:25]
    try:
        for fname in os.listdir(save_dir):
            if fname.endswith((".mp4", ".m4a", ".m4s", ".mkv")):
                if clean_title in fname or title[:15] in fname:
                    fpath = os.path.join(save_dir, fname)
                    if os.path.isfile(fpath):
                        size_mb = os.path.getsize(fpath) / (1024 * 1024)
                        matches.append({"name": fname, "size": f"{size_mb:.1f} MB", "path": fpath})
    except Exception:
        pass
    return matches

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

            # 为已完成任务匹配本地实际文件体积
            for t in tasks_data.get("Finished", []):
                title = t.get("Title") or ""
                if title:
                    matched = find_matching_files(title, DEFAULT_DOWNLOAD_DIR)
                    if matched:
                        t["ActualFiles"] = matched
                        t["ActualSize"] = matched[0]["size"]
                        t["ActualFileName"] = matched[0]["name"]

            return self.send_json(200, tasks_data)

        # 8. 清空任务列表
        if path == "/api/tasks/clear":
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{SERVER_PORT}/remove-finished/", timeout=3)
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
                bbdown_req = urllib.request.Request(
                    f"http://127.0.0.1:{SERVER_PORT}/add-task",
                    data=json.dumps(req_data).encode("utf-8"),
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
