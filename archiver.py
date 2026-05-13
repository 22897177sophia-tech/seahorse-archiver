#!/usr/bin/env python3
"""Seahorse Planet Podcast Archiver v2"""
import os, re, json, time, smtplib, requests
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from bs4 import BeautifulSoup

SITE = "https://seahorseplanet.net/episodes/"
MF = "manifest.json"
TAG = "archive"
BATCH = 20
TIMEOUT = 900

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO = os.environ["GH_REPO"]
QQ_EMAIL = os.environ["QQ_EMAIL"]
QQ_AUTH = os.environ["QQ_AUTH_CODE"]
MODE = os.environ.get("MODE", "weekly")
RCPT = [x.strip() for x in os.environ["MAIL_RECIPIENTS_TEST" if MODE=="test" else "MAIL_RECIPIENTS_FULL"].split(",")]

MONTHS = {'January':'01','February':'02','March':'03','April':'04','May':'05','June':'06','July':'07','August':'08','September':'09','October':'10','November':'11','December':'12'}

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)

def load_mf():
    if os.path.exists(MF):
        return json.load(open(MF, "r", encoding="utf-8"))
    return {"processed":{}, "last_heartbeat":None, "filename_seq":{}}

def save_mf(m):
    json.dump(m, open(MF,"w",encoding="utf-8"), ensure_ascii=False, indent=2)

def clean(s): return re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]+', '', s)

def fetch_eps():
    eps, page = [], 1
    while page <= 30:
        url = SITE if page==1 else f"{SITE}page/{page}/"
        log(f"抓取分页: {url}")
        r = requests.get(url, timeout=30, headers={"User-Agent":"Mozilla/5.0"})
        if r.status_code == 404: break
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "lxml")
        links = soup.find_all("a", href=re.compile(r"/podcast-download/\d+/"))
        if not links: break
        before = len(eps)
        for a in links:
            mp3 = a.get("href","").split("?")[0]
            title = a.get("title","").strip()
            if not title: continue
            p, date_str, ym = a, None, None
            for _ in range(8):
                p = p.parent
                if p is None: break
                txt = p.get_text(" ", strip=True)
                m = re.search(r'Recorded on (\d{1,2})\.\s*(\w+)\s*(\d{4})', txt)
                if m:
                    d, mo, y = m.group(1), m.group(2), m.group(3)
                    ym = f"{y}{MONTHS.get(mo,'01')}"
                    date_str = f"{y}-{MONTHS.get(mo,'01')}-{int(d):02d}"
                    break
            if not date_str: continue
            if any(e["mp3"]==mp3 for e in eps): continue
            eps.append({"guid":mp3,"title":title,"mp3":mp3,"ym":ym,"date":date_str})
        if len(eps) == before: break
        page += 1
        time.sleep(1)
    eps.sort(key=lambda x: x["date"])
    log(f"全站共抓到 {len(eps)} 期")
    return eps

def make_fn(ep, mf):
    ym = ep["ym"]
    used = mf["filename_seq"].get(ym, [])
    key = f"{ep['date']}_{ep['mp3']}"
    if key in used:
        idx = used.index(key)
    else:
        idx = len(used)
        used.append(key)
        mf["filename_seq"][ym] = used
    return f"{ym}.mp3" if idx==0 else f"{ym}_{chr(ord('a')+idx-1)}.mp3"

def gh_release():
    h = {"Authorization":f"Bearer {GH_TOKEN}","Accept":"application/vnd.github+json"}
    api = "https://api.github.com"
    r = requests.get(f"{api}/repos/{GH_REPO}/releases/tags/{TAG}", headers=h, timeout=15)
    if r.status_code == 200: return r.json(), h, api
    r = requests.post(f"{api}/repos/{GH_REPO}/releases", headers=h, timeout=15,
        json={"tag_name":TAG,"name":"海马星球播客归档","body":"自动归档"})
    r.raise_for_status()
    return r.json(), h, api

def upload(rel, h, api, fp, fn):
    upload_url = rel["upload_url"].split("{")[0]
    data = open(fp,"rb").read()
    r = requests.post(f"{upload_url}?name={fn}", headers={**h,"Content-Type":"audio/mpeg"}, data=data, timeout=600)
    if r.status_code == 422:
        r2 = requests.get(f"{api}/repos/{GH_REPO}/releases/{rel['id']}/assets", headers=h, timeout=15)
        for a in r2.json():
            if a["name"]==fn:
                requests.delete(f"{api}/repos/{GH_REPO}/releases/assets/{a['id']}", headers=h, timeout=15)
                break
        r = requests.post(f"{upload_url}?name={fn}", headers={**h,"Content-Type":"audio/mpeg"}, data=data, timeout=600)
    r.raise_for_status()
    return r.json()["browser_download_url"]

def mail(sub, body, rcpt=None):
    rcpt = rcpt or RCPT
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(sub, "utf-8")
    msg["From"] = QQ_EMAIL
    msg["To"] = ", ".join(rcpt)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
        s.login(QQ_EMAIL, QQ_AUTH)
        s.sendmail(QQ_EMAIL, rcpt, msg.as_string())
    log(f"邮件已发: {sub}")

def process(ep, rel, h, api, mf):
    fn = make_fn(ep, mf)
    start = time.time()
    try:
        log(f"下载: {fn} ({ep['title']})")
        with requests.get(ep["mp3"], timeout=300, stream=True, headers={"User-Agent":"Mozilla/5.0"}) as r:
            r.raise_for_status()
            with open(fn,"wb") as f:
                for c in r.iter_content(65536):
                    if time.time()-start > TIMEOUT: raise TimeoutError("超时")
                    f.write(c)
        sz = os.path.getsize(fn) / 1048576
        log(f"上传: {fn} ({sz:.1f}MB)")
        url = upload(rel, h, api, fn, fn)
        os.remove(fn)
        return {"fn":fn, "url":url, "sz":round(sz,1)}, None
    except Exception as e:
        if os.path.exists(fn): os.remove(fn)
        return None, str(e)

def main():
    log(f"=== 模式: {MODE} | 收件人: {len(RCPT)}人 ===")
    mf = load_mf()
    mf.setdefault("filename_seq", {})
    try:
        eps = fetch_eps()
    except Exception as e:
        mail("⚠️ 海马星球抓取失败", f"错误: {e}", [QQ_EMAIL]); raise
    if not eps:
        mail("⚠️ 海马星球抓取异常", "未抓到音频", [QQ_EMAIL]); return
    new = [e for e in eps if e["guid"] not in mf["processed"]]
    total_new = len(new)
    if MODE == "test":
        new = new[-2:]
    else:
        new = new[:BATCH]
    log(f"本批: {len(new)} | 总待处理: {total_new}")
    if not new:
        now = datetime.now()
        last = mf.get("last_heartbeat")
        hb = not last or (now.year, now.month) != (datetime.fromisoformat(last).year, datetime.fromisoformat(last).month)
        if hb:
            mail("海马星球月报 · 一切正常", f"本月无新音频。已归档 {len(mf['processed'])} 期。", [QQ_EMAIL])
            mf["last_heartbeat"] = now.isoformat()
        save_mf(mf)
        return
    rel, h, api = gh_release()
    succ, fail = [], []
    for i, ep in enumerate(new, 1):
        log(f"--- [{i}/{len(new)}] ---")
        r, err = process(ep, rel, h, api, mf)
        if r:
            succ.append({**ep, **r})
            mf["processed"][ep["guid"]] = {"filename":r["fn"], "download_url":r["url"], "processed_at":datetime.now().isoformat()}
            save_mf(mf)
            body = f"《{ep['title']}》\n\n录制日期: {ep['date']}\n文件大小: {r['sz']}MB\n下载地址: {r['url']}\n"
            mail(f"海马星球新音频 · {r['fn'].replace('.mp3','')}", body)
        else:
            log(f"失败: {ep['title']} - {err}")
            fail.append({**ep, "error":err})
    rem = total_new - len(succ)
    if rem > 0 and MODE != "test":
        body = f"本批已完成 {len(succ)} 期,总剩余 {rem} 期。\n请再次手动 Run workflow 继续。\n"
        if fail:
            body += "\n失败清单(下次重试):\n" + "\n".join(f"- {f['title']}: {f['error']}" for f in fail)
        mail(f"海马星球归档进度 · {len(mf['processed'])}/{len(eps)}", body, [QQ_EMAIL])
    elif fail:
        mail("⚠️ 海马星球部分失败", "失败清单:\n" + "\n".join(f"- {f['title']}: {f['error']}" for f in fail), [QQ_EMAIL])
    save_mf(mf)
    log(f"=== 完成 | 成功 {len(succ)} | 失败 {len(fail)} ===")

if __name__ == "__main__":
    main()
