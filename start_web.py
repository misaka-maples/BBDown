#!/usr/bin/env python3
"""
BiliDown Web UI 启动脚本
1. 启动 BBDown Api Server (端口 58682)
2. 启动 Web UI 静态服务 (端口 58683)
3. 浏览器访问 http://localhost:58683 即可使用类似 SnapAny 的图形化解析下载界面
"""

import os
import sys
import time
import signal
import subprocess
from http.server import SimpleHTTPRequestHandler, HTTPServer
import threading

SERVER_PORT = 58682
WEB_PORT = 58683
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(CURRENT_DIR, "web")

class WebUIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def log_message(self, format, *args):
        # 简化日志输出
        return

def start_bbdown_server():
    cmd = ["BBDown", "dummy", "serve", "-l", f"http://127.0.0.1:{SERVER_PORT}"]
    print(f"[*] 正在启动 BBDown API 核心服务 (http://127.0.0.1:{SERVER_PORT})...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc

def main():
    if not os.path.exists(os.path.join(WEB_DIR, "index.html")):
        print(f"[!] 找不到 Web UI 目录: {WEB_DIR}")
        sys.exit(1)

    # 1. 启动 BBDown Api Server
    bbdown_proc = start_bbdown_server()
    time.sleep(1.5)

    # 2. 启动 Web 前端服务
    httpd = HTTPServer(("0.0.0.0", WEB_PORT), WebUIHandler)
    print("=" * 60)
    print(f"🎉 BiliDown Web UI 已成功启动！")
    print(f"👉 请在浏览器中打开: http://localhost:{WEB_PORT}")
    print(f"👉 本地网络访问地址: http://127.0.0.1:{WEB_PORT}")
    print("=" * 60)
    print("按 Ctrl+C 停止服务...")

    def cleanup(sig=None, frame=None):
        print("\n[*] 正在关闭服务...")
        try:
            httpd.server_close()
        except:
            pass
        try:
            bbdown_proc.terminate()
            bbdown_proc.wait(timeout=3)
        except:
            bbdown_proc.kill()
        print("[✓] 已退出。")
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
