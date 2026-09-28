#!/usr/bin/env python3
"""
BiliDown Web UI 启动脚本
1. 启动 BBDown Api Server (端口 58682)
2. 启动 Web UI 静态服务与 API 代理 (端口 58683)
3. 默认下载保存至 /home/maple/Downloads
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
from http.server import SimpleHTTPRequestHandler, HTTPServer

SERVER_PORT = 58682
WEB_PORT = 58683
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(CURRENT_DIR, "web")
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/Downloads")

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
    api_req = urllib.request.Request(api_url, headers={"User-Agent": "curl/8.5.0"})
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

class WebUIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def log_message(self, format, *args):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        
        # 本地解析代理接口，彻底解决浏览器跨域 CORS 问题
        if parsed.path == "/api/parse":
            query = urllib.parse.parse_qs(parsed.query)
            url = query.get("url", [""])[0].strip()
            if not url:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "缺少 url 参数"}).encode("utf-8"))
                return

            try:
                info = parse_bili_url(url)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"code": 0, "data": info}).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"code": -1, "message": str(e)}).encode("utf-8"))
            return

        if parsed.path == "/api/info":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "default_download_dir": DEFAULT_DOWNLOAD_DIR,
                "server_port": SERVER_PORT
            }).encode("utf-8"))
            return

        return super().do_GET()

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
