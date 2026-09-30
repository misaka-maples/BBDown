#!/usr/bin/env python3
import subprocess
import re
import sys
import os
import time

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PUB_FILE = os.path.join(CURRENT_DIR, "public_url.txt")

def main():
    cmd = ["cloudflared", "tunnel", "--url", "http://127.0.0.1:58683"]
    print("[*] 正在启动 Cloudflare 公网安全隧道...", flush=True)
    
    url_pattern = re.compile(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com')
    
    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                m = url_pattern.search(line)
                if m:
                    public_url = m.group(0)
                    with open(PUB_FILE, "w", encoding="utf-8") as f:
                        f.write(public_url + "\n")
                    print("\n" + "=" * 60, flush=True)
                    print(f"🎉 公网访问地址已生成并生效！", flush=True)
                    print(f"👉 公网直达 URL: {public_url}", flush=True)
                    print("=" * 60 + "\n", flush=True)
            proc.wait()
        except Exception as e:
            print(f"[!] 隧道异常退出: {e}，5秒后重启...", flush=True)
        time.sleep(5)

if __name__ == "__main__":
    main()
