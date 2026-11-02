#!/usr/bin/env python3
"""Log in to Keep and save each activity detail response as JSON.

Existing non-empty files are skipped, so a stopped download can continue.
This script does not write a database or convert the responses.
"""

import getpass
import json
import os
import sys
import time
from pathlib import Path

import requests

LOGIN_API = "https://api.gotokeep.com/v1.1/users/login"
LIST_API = "https://api.gotokeep.com/pd/v3/stats/detail?dateUnit=all&type={sport}&lastDate={last_date}"
DETAIL_API = "https://api.gotokeep.com/pd/v3/{sport}log/{run_id}"
DEFAULT_USER = "default"
SPORTS = ("running", "cycling")
PAUSE_SECONDS = 1
REQUEST_TIMEOUT = 30


def login(session, mobile, password):
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:78.0) Gecko/20100101 Firefox/78.0",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
    }
    response = session.post(
        LOGIN_API,
        headers=headers,
        data={"mobile": mobile, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    token = login_token(response)
    if token is None:
        message = response_text(response) or response.status_code
        raise SystemExit(f"Keep 登录失败：{message}")
    headers["Authorization"] = f"Bearer {token}"
    return headers


def response_json(response):
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def response_text(response):
    return response_json(response).get("text") or ""


def login_token(response):
    if not response.ok:
        return None
    data = response_json(response).get("data")
    if not isinstance(data, dict):
        return None
    return data.get("token")


def keep_id(run_id):
    parts = str(run_id).split("_")
    if len(parts) >= 2 and parts[1]:
        return parts[1]
    return parts[0]


def activity_ids_in(page):
    for day in page.get("records") or []:
        for item in day.get("logs") or []:
            stats = item.get("stats") or {}
            if stats.get("id"):
                yield stats["id"]


def fetch_list_page(session, headers, sport, last_date):
    response = session.get(
        LIST_API.format(sport=sport, last_date=last_date),
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise SystemExit(f"读取{sport}列表失败：{response.status_code}")
    return response.json().get("data") or {}


def list_activity_ids(session, headers, sport):
    ids = []
    last_date = 0
    while True:
        page = fetch_list_page(session, headers, sport, last_date)
        ids.extend(activity_ids_in(page))
        print(f"{sport} 已列出 {len(ids)} 条")
        last_date = page.get("lastTimestamp") or 0
        if not last_date:
            return ids
        time.sleep(PAUSE_SECONDS)


def fetch_detail(session, headers, sport, run_id):
    response = session.get(
        DETAIL_API.format(sport=sport, run_id=run_id),
        headers=headers,
        timeout=REQUEST_TIMEOUT,
    )
    if not response.ok:
        return None
    payload = response.json()
    if not isinstance(payload, dict) or "data" not in payload:
        return None
    return payload


def write_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def already_saved(path):
    return path.is_file() and path.stat().st_size > 0


def save_detail(session, headers, sport, run_id, raw_dir):
    path = raw_dir / f"{keep_id(run_id)}.json"
    if already_saved(path):
        return "skipped"
    time.sleep(PAUSE_SECONDS)
    payload = fetch_detail(session, headers, sport, run_id)
    if payload is None:
        print(f"失败：{keep_id(run_id)}")
        return "failed"
    write_json(path, payload)
    return "saved"


def download(raw_dir, mobile, password):
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    headers = login(session, mobile, password)
    saved = skipped = failed = 0
    for sport in SPORTS:
        for run_id in list_activity_ids(session, headers, sport):
            result = save_detail(session, headers, sport, run_id, raw_dir)
            if result == "saved":
                saved += 1
                if saved % 20 == 0:
                    print(f"已保存 {saved}")
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1
    print(f"原始 JSON 已写入 {raw_dir}：新保存 {saved}，已存在 {skipped}，失败 {failed}。")
    if failed:
        raise SystemExit(1)


def prompt_credentials():
    mobile = input("Keep 手机号: ").strip()
    password = getpass.getpass("Keep 密码: ")
    if not mobile or not password:
        raise SystemExit("需要手机号和密码")
    return mobile, password


def user_name(argv):
    if len(argv) > 1:
        raise SystemExit(f"用法: python download_keep.py [用户名]，默认 {DEFAULT_USER}")
    name = argv[0] if argv else DEFAULT_USER
    if name in {".", ".."} or any(part in name for part in ("/", "\\")):
        raise SystemExit("用户名无效")
    return name


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    raw_dir = Path("data") / user_name(argv) / "raw"
    mobile, password = prompt_credentials()
    os.umask(0o077)
    download(raw_dir, mobile, password)


if __name__ == "__main__":
    main()
