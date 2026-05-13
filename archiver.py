#!/usr/bin/env python3
"""Seahorse Planet Podcast Archiver -> GitHub Release"""
import os, re, json, time, smtplib, requests
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from bs4 import BeautifulSoup

# ===== 配置 =====
SITE_EPISODES = "https://seahorseplanet.net/episodes/"
MANIFEST_FILE = "manifest.json"
RELEASE_TAG = "archive"

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
    return {"processed": {}, "last_heartbeat": None}

def save_manifest(m):
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)

def clean_title(title):
    """清洗为紧凑文件名,只保留中英文数字"""
    return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '', title)

MONTH_MAP = {
    'January':'01','February':'02','March':'03','April':'04','May':'05','June':'06',
    'July':'07','August':'08','September':'09','October':'10','November':'11','December':'12'
}

def fetch_all_episodes():
    """爬取 /episodes/ 所有分页"""
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
                "guid": mp3_url,
                "title": title_attr,
                "filename": f"{ym}{clean_title(title_attr)}.mp3",
                "mp3_url": mp3_url,
                "ym": ym,
                "pub_date": date_str,
            })
        if len(episodes) == before_count:
            break
        page += 1
        if page > 30:
            break
        time.sleep(1)
    episodes.sort(key=lambda x: x["pub_date"])
    log(f"全站共抓到 {len(episodes)} 期")
    return episodes

class GHRelease:
    def __init__(self, repo, token):
        self.repo = repo
        self.token = token
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
        return r.json()["browser_download_url"]

def send_mail(subject, body, recipients=None):
    recipients = recipients or RECIPIENTS
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = QQ_EMAIL
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
        s.login(QQ_EMAIL, QQ_AUTH_CODE)
        s.sendmail(QQ_EMAIL, recipients, msg.as_string())
    log(f"邮件已发: {subject} -> {len(recipients)}人")

def main():
    log(f"=== 模式: {MODE} | 收件人: {RECIPIENTS} ===")
    manifest = load_manifest()
    try:
        episodes = fetch_all_episodes()
    except Exception as e:
        send_mail("⚠️ 海马星球抓取失败", f"错误: {e}", recipients=[QQ_EMAIL])
        raise
    if not episodes:
        send_mail("⚠️ 海马星球抓取异常", "未抓到任何音频,可能网站改版", recipients=[QQ_EMAIL])
        return
    is_first_run = len(manifest["processed"]) == 0
    new_eps = [e for e in episodes if e["guid"] not in manifest["processed"]]
    if MODE == "test":
        new_eps = new_eps[-2:]
        log(f"测试模式: 只处理最近 2 期")
    log(f"待处理: {len(new_eps)} 期")
    if not new_eps:
        today = datetime.now()
        last_hb = manifest.get("last_heartbeat")
        send_hb = not last_hb or (today.year, today.month) != (
            datetime.fromisoformat(last_hb).year, datetime.fromisoformat(last_hb).month)
        if send_hb:
            send_mail("海马星球月报 · 一切正常",
                f"本月检测完成,无新音频。\n已归档总数: {len(manifest['processed'])} 期\n下次自动检测: 下周一 09:00",
                recipients=[QQ_EMAIL])
            manifest["last_heartbeat"] = today.isoformat()
        save_manifest(manifest)
        log("无新音频,结束")
        return
    gh = GHRelease(GH_REPO, GH_TOKEN)
    release = gh.get_or_create_release(RELEASE_TAG)
    success, failed = [], []
    for ep in new_eps:
        try:
            log(f"下载: {ep['filename']}")
            r = requests.get(ep["mp3_url"], timeout=600, stream=True,
                            headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            with open(ep["filename"], "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
            size_mb = os.path.getsize(ep["filename"]) / 1024 / 1024
            log(f"上传到 Release: {ep['filename']} ({size_mb:.1f}MB)")
            download_url = gh.upload_asset(release, ep["filename"], ep["filename"])
            os.remove(ep["filename"])
            success.append({**ep, "download_url": download_url, "size_mb": round(size_mb, 1)})
            manifest["processed"][ep["guid"]] = {
                "filename": ep["filename"],
                "download_url": download_url,
                "processed_at": datetime.now().isoformat(),
            }
            save_manifest(manifest)
        except Exception as e:
            log(f"失败: {ep['filename']} - {e}")
            failed.append({**ep, "error": str(e)})
    if is_first_run and MODE != "test":
        body = f"海马星球播客全站音频已归档,共 {len(success)} 期。\n"
        body += "点击链接直接下载 mp3,可导入任何音频播放器。\n\n"
        body += "=" * 40 + "\n\n"
        for s in success:
            body += f"《{s['title']}》\n录制: {s['pub_date']} | 大小: {s['size_mb']}MB\n{s['download_url']}\n\n"
        if failed:
            body += "\n" + "=" * 40 + "\n以下处理失败,下周自动重试:\n"
            for f in failed:
                body += f"- {f['title']}: {f['error']}\n"
        body += "\n之后每周一自动检测,有新音频单独发信。"
        send_mail("海马星球播客 · 历史音频整理完成", body)
    else:
        for s in success:
            body = f"《{s['title']}》\n\n录制日期: {s['pub_date']}\n文件大小: {s['size_mb']}MB\n下载地址: {s['download_url']}\n"
            send_mail(f"海马星球新音频 · {s['filename'].replace('.mp3','')}", body)
        if failed:
            err = "\n".join(f"- {f['title']}: {f['error']}" for f in failed)
            send_mail("⚠️ 海马星球部分处理失败", f"以下音频失败,下周重试:\n\n{err}",
                      recipients=[QQ_EMAIL])
    save_manifest(manifest)
    log("=== 完成 ===")

if __name__ == "__main__":
    main()
