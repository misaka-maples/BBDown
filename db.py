#!/usr/bin/env python3
"""
BiliDown 用户与任务数据库模块 (基于 SQLite)
提供多用户注册、登录认证、独立会话、B站 Cookie 与下载任务隔离
"""

import os
import re
import time
import sqlite3
import hashlib
import secrets

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(CURRENT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "bilidown.db")

def get_connection():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000).hex()

def init_db(default_download_dir):
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        bili_cookie TEXT DEFAULT '',
        custom_save_dir TEXT DEFAULT '',
        created_at REAL NOT NULL
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)
    c.execute("""
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        aid TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT NOT NULL,
        quality_label TEXT DEFAULT '',
        dfn_tag TEXT DEFAULT '',
        work_dir TEXT NOT NULL,
        expected_ext TEXT DEFAULT '.mp4',
        file_pattern TEXT DEFAULT '',
        add_time REAL NOT NULL,
        is_removed INTEGER DEFAULT 0,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )
    """)
    conn.commit()

    # 初始化默认 admin 用户 (若尚无任何用户)
    c.execute("SELECT COUNT(*) as cnt FROM users")
    if c.fetchone()["cnt"] == 0:
        salt = secrets.token_hex(16)
        pwd_hash = hash_password("admin123", salt)
        admin_dir = os.path.join(default_download_dir, "users", "admin")
        os.makedirs(admin_dir, exist_ok=True)

        legacy_cookie = ""
        for p in [os.path.join(os.path.expanduser("~/.local/bin"), "BBDown.data"), os.path.join(CURRENT_DIR, "BBDown.data")]:
            if os.path.exists(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        c_str = f.read().strip()
                        if "SESSDATA=" in c_str:
                            legacy_cookie = c_str
                            break
                except Exception:
                    pass

        c.execute("""
        INSERT INTO users (username, password_hash, salt, bili_cookie, custom_save_dir, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, ("admin", pwd_hash, salt, legacy_cookie, admin_dir, time.time()))
        conn.commit()
        print(f"[*] 已初始化默认管理员账号：admin / admin123 (独立保存目录: {admin_dir})", flush=True)

    conn.close()

def register_user(username, password, default_download_dir):
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or len(username) < 2:
        return None, "用户名至少需要 2 个字符"
    if not password or len(password) < 4:
        return None, "密码至少需要 4 个字符"
    if not re.match(r'^[a-zA-Z0-9_\-\u4e00-\u9fa5]+$', username):
        return None, "用户名仅允许字母、数字、下划线及中文"

    salt = secrets.token_hex(16)
    pwd_hash = hash_password(password, salt)
    user_dir = os.path.join(default_download_dir, "users", username)
    os.makedirs(user_dir, exist_ok=True)

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("""
        INSERT INTO users (username, password_hash, salt, bili_cookie, custom_save_dir, created_at)
        VALUES (?, ?, ?, '', ?, ?)
        """, (username, pwd_hash, salt, user_dir, time.time()))
        user_id = c.lastrowid
        conn.commit()
        return get_user_by_id(user_id), None
    except sqlite3.IntegrityError:
        return None, "该用户名已被注册，请尝试其他名称"
    finally:
        conn.close()

def authenticate_user(username, password):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),))
    row = c.fetchone()
    conn.close()
    if not row:
        return None, "用户不存在"
    pwd_hash = hash_password((password or "").strip(), row["salt"])
    if pwd_hash != row["password_hash"]:
        return None, "密码不正确"
    user_dict = dict(row)
    user_dict.pop("password_hash", None)
    user_dict.pop("salt", None)
    return user_dict, None

def get_user_by_username(username):
    if not username:
        return None
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, bili_cookie, custom_save_dir, created_at FROM users WHERE username = ?", (username.strip(),))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def login_or_create_user_by_bili(cookie_str, user_info, default_download_dir):
    uname = (user_info.get("uname") or "").strip()
    mid = str(user_info.get("mid") or "")
    if not uname:
        uname = f"bili_{mid}" if mid else "bili_user"

    safe_uname = re.sub(r'[\\/:*?"<>|]', '_', uname).strip()
    if not safe_uname:
        safe_uname = f"bili_{mid}" if mid else "bili_user"

    conn = get_connection()
    c = conn.cursor()
    try:
        c.execute("SELECT * FROM users WHERE username = ?", (safe_uname,))
        row = c.fetchone()
        if row:
            user_id = row["id"]
            c.execute("UPDATE users SET bili_cookie = ? WHERE id = ?", (cookie_str or "", user_id))
            conn.commit()
            return get_user_by_id(user_id), None
        else:
            salt = secrets.token_hex(16)
            pwd_hash = hash_password(secrets.token_hex(16), salt)
            user_dir = os.path.join(default_download_dir, "users", safe_uname)
            os.makedirs(user_dir, exist_ok=True)
            c.execute("""
            INSERT INTO users (username, password_hash, salt, bili_cookie, custom_save_dir, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """, (safe_uname, pwd_hash, salt, cookie_str or "", user_dir, time.time()))
            user_id = c.lastrowid
            conn.commit()
            return get_user_by_id(user_id), None
    except Exception as e:
        return None, str(e)
    finally:
        conn.close()


def create_session(user_id):
    token = secrets.token_hex(32)
    now = time.time()
    expires_at = now + 30 * 86400  # 30 天有效
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    INSERT INTO sessions (token, user_id, created_at, expires_at)
    VALUES (?, ?, ?, ?)
    """, (token, user_id, now, expires_at))
    conn.commit()
    conn.close()
    return token

def get_user_by_token(token):
    if not token:
        return None
    conn = get_connection()
    c = conn.cursor()
    now = time.time()
    c.execute("""
    SELECT u.id, u.username, u.bili_cookie, u.custom_save_dir, u.created_at
    FROM users u
    JOIN sessions s ON u.id = s.user_id
    WHERE s.token = ? AND s.expires_at > ?
    """, (token, now))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def get_user_by_id(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT id, username, bili_cookie, custom_save_dir, created_at FROM users WHERE id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_session(token):
    conn = get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()

def update_user_cookie(user_id, cookie_str):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET bili_cookie = ? WHERE id = ?", (cookie_str or "", user_id))
    conn.commit()
    conn.close()

def update_user_save_dir(user_id, save_dir):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE users SET custom_save_dir = ? WHERE id = ?", (save_dir or "", user_id))
    conn.commit()
    conn.close()

def add_user_task(user_id, meta):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    INSERT INTO tasks (user_id, aid, url, title, quality_label, dfn_tag, work_dir, expected_ext, file_pattern, add_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        str(meta.get("aid", "")),
        meta.get("url", ""),
        meta.get("title", ""),
        meta.get("qualityLabel", ""),
        meta.get("dfnTag", ""),
        meta.get("workDir", ""),
        meta.get("expectedExt", ".mp4"),
        meta.get("filePattern", ""),
        meta.get("addTime", time.time())
    ))
    task_id = c.lastrowid
    conn.commit()
    conn.close()
    return task_id

def get_user_tasks(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    SELECT * FROM tasks WHERE user_id = ? AND is_removed = 0 ORDER BY add_time ASC
    """, (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_user_task_by_id(user_id, task_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    SELECT * FROM tasks WHERE user_id = ? AND id = ? AND is_removed = 0
    """, (user_id, task_id))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def remove_user_task(user_id, task_id_or_aid):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
    UPDATE tasks SET is_removed = 1
    WHERE user_id = ? AND (aid = ? OR id = ?)
    """, (user_id, str(task_id_or_aid), str(task_id_or_aid)))
    conn.commit()
    conn.close()

def clear_user_tasks(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("UPDATE tasks SET is_removed = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

