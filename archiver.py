#!/usr/bin/env python3
"""Seahorse Planet Podcast Archiver -> GitHub Release (v2)"""
import os, re, json, time, smtplib, requests
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from bs4 import BeautifulSoup

# ===== 配置 =====
SITE_EPISODES = "https://seahorseplanet.net/episodes/"
MANIFEST_FILE = "manifest.json"
RELEASE_TAG = "archive"
BATCH_SIZE = 20
SINGLE_TIMEOUT = 900

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO = os.environ["GH_REPO"]
QQ_EMAIL = os.environ["QQ_EMAIL"]
QQ_AUTH_CODE = os.environ["QQ_AUTH_CODE"]
MODE = os.environ.get("MODE", "weekly")

if MODE == "test":
    RECIPIENTS = [x.strip() for x in os.environ["MAIL_RECIPIENTS_TEST"].split(",")]
else:
    RECIPIENTS = [x.strip() for x in os.environ["MAIL_RECIPIENTS_FULL"].split(",")]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"processed": {}, "last_heartbeat": None, "filename_seq": {}}

def save_manifest(m):
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

def clean_title(title):
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '', title)

MONTH_MAP = {
    'January':'01','February':'02','March':'03','April':'04','May':'05','June':'06',
    'July':'07','August':'08','September':'09','October':'10','November':'11','December':'12'
}

def fetch_all_episodes():
    episodes = []
    page = 1
    while True:
        url = SITE_EPISODES if page == 1 else f"{SITE_EPISODES}page/{page}/"
        log(f"抓取分页: {url}")
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 404:
            break
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "lxml")
        download_links = soup.find_all("a", href=re.compile(r"/podcast-download/\d+/"))
        if not download_links:
            break
        before_count = len(episodes)
        for a in download_links:
            href = a.get("href", "")
            mp3_url = href.split("?")[0]
            title_attr = a.get("title", "").strip()
            if not title_attr:
                continue
            parent = a
            date_str, ym = None, None
            for _ in range(8):
                parent = parent.parent
                if parent is None: break
                text = parent.get_text(" ", strip=True)
                m = re.search(r'Recorded on (\d{1,2})\.\s*(\w+)\s*(\d{4})', text)
                if m:
                    day, month_en, year = m.group(1), m.group(2), m.group(3)
                    mm = MONTH_MAP.get(month_en, "01")
                    date_str = f"{year}-{mm}-{int(day):02d}"
                    ym = f"{year}{mm}"
                    break
            if not date_str:
                continue
            if any(e["mp3_url"] == mp3_url for e in episodes):
                continue
            episodes.append({
                "guid": mp3_url, "title": title_attr,
                "title_clean": clean_title(title_attr),
                "mp3_url": mp3_url, "ym": ym, "pub_date": date_str,
            })
        if len(episodes) == before_count:
            break
        page += 1
        if page > 30: break
        time.sleep(1)
    episodes.sort(key=lambda x: x["pub_date"])
    log(f"全站共抓到 {len(episodes)} 期")
    return episodes

def make_filename(ep, manifest):
    ym = ep["ym"]
    used = manifest["filename_seq"].get(ym, [])
    key = f"{ep['pub_date']}_{ep['mp3_url']}"
    if key in used:
        idx = used.index(key)
    else:
        idx = len(used)
        used.append(key)
        manifest["filename_seq"][ym] = used
    if idx == 0:
        return f"{ym}.mp3"
    suffix = chr(ord('a') + idx - 1)
    return f"{ym}_{suffix}.mp3"

class GHRelease:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token
        self.api = "https://api.github.com"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }
    def get_or_create_release(self, tag):
        r = requests.get(f"{self.api}/repos/{self.repo}/releases/tags/{tag}",
                         headers=self.headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        r = requests.post(f"{self.api}/repos/{self.repo}/releases",
                          headers=self.headers, timeout=15,
                          json={"tag_name": tag, "name": "海马星球播客归档",
                                "body": "自动归档,请勿删除"})
        r.raise_for_status()
        return r.json()
    def upload_asset(self, release, filepath, filename):
        upload_url = release["upload_url"].split("{")[0]
        with open(filepath, "rb") as f:
            data = f.read()
        r = requests.post(f"{upload_url}?name={filename}",
            headers={**self.headers, "Content-Type": "audio/mpeg"},
            data=data, timeout=600)
        if r.status_code == 422:
            r2 = requests.get(f"{self.api}/repos/{self.repo}/releases/{release['id']}/assets",
                              headers=self.headers, timeout=15)
            for asset in r2.json():
                if asset["name"] == filename:
                    requests.delete(f"{self.api}/repos/{self.repo}/releases/assets/{asset['id']}",
                                    headers=self.headers, timeout=15)
                    break
            r = requests.post(f"{upload_url}?name={filename}",
                headers={**self.headers, "Content-Type": "audio/mpeg"},
                data=data, timeout=600)
        r.raise_for_status()
        return r.json()
