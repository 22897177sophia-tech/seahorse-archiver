#!/usr/bin/env python3
"""Seahorse Planet Podcast Archiver v4"""
import os, re, json, time, smtplib, requests
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import Header
from bs4 import BeautifulSoup
from collections import defaultdict

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
RCPT_TEST = [x.strip() for x in os.environ["MAIL_RECIPIENTS_TEST"].split(",")]
RCPT_FULL = [x.strip() for x in os.environ["MAIL_RECIPIENTS_FULL"].split(",")]
RCPT = RCPT_TEST if MODE == "test" else RCPT_FULL

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

def mail(sub, body, rcpt=None, html=False):
    rcpt = rcpt or RCPT
    if html:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = Header(sub, "utf-8")
        msg["From"] = QQ_EMAIL
        msg["To"] = ", ".join(rcpt)
        msg.attach(MIMEText("请使用支持 HTML 的邮箱查看本邮件。", "plain", "utf-8"))
        msg.attach(MIMEText(body, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(sub, "utf-8")
        msg["From"] = QQ_EMAIL
        msg["To"] = ", ".join(rcpt)
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
        s.login(QQ_EMAIL, QQ_AUTH)
        s.sendmail(QQ_EMAIL, rcpt, msg.as_string())
    log(f"邮件已发: {sub} -> {len(rcpt)}人")

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

def build_html_digest(mf, total_eps):
    """生成 HTML 表格邮件正文"""
    items = list(mf["processed"].values())
    items.sort(key=lambda x: x.get("date",""), reverse=True)
    
    # 按年分组
    by_year = defaultdict(list)
    for it in items:
        y = it.get("date","")[:4] or "未知"
        by_year[y].append(it)
    
    GREEN = "#1B3A2F"
    GREEN_LIGHT = "#2D5847"
    STRIPE = "#F4F7F5"
    
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:20px;background:#FAFBFA;font-family:-apple-system,'PingFang SC','Helvetica Neue',Arial,sans-serif;color:#222;line-height:1.6;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.04);">

<div style="background:{GREEN};color:#fff;padding:32px 28px;">
<div style="font-size:22px;font-weight:600;letter-spacing:0.5px;">海马星球播客 · 全站归档</div>
<div style="font-size:14px;opacity:0.85;margin-top:8px;">共 {len(items)} 期 · {datetime.now().strftime('%Y-%m-%d')}</div>
</div>

<div style="padding:28px;">
<p style="margin:0 0 12px 0;font-size:15px;">亲爱的姐妹：</p>
<p style="margin:0 0 12px 0;font-size:15px;">这里是海马星球播客的全站归档,共 {len(items)} 期。</p>
<p style="margin:0 0 12px 0;font-size:15px;">让女性的声音被听见、被流传——<br>不是一个人的事,是我们的事。</p>
<p style="margin:0 0 12px 0;font-size:15px;">愿这些声音陪伴你走过更长的路。</p>
<p style="margin:0 0 4px 0;font-size:15px;">点击 ↓ 即可下载。</p>
<p style="margin:0 0 24px 0;font-size:15px;text-align:right;color:#666;">——林晓兰</p>
"""
    
    for year in sorted(by_year.keys(), reverse=True):
        eps_y = by_year[year]
        html += f"""
<div style="margin-top:32px;margin-bottom:10px;font-size:17px;font-weight:600;color:{GREEN};border-left:3px solid {GREEN};padding-left:10px;">
{year} 年 <span style="font-size:13px;color:#888;font-weight:400;">({len(eps_y)} 期)</span>
</div>
<table cellpadding="0" cellspacing="0" border="0" style="width:100%;border-collapse:collapse;font-size:14px;">
<thead>
<tr style="background:{GREEN_LIGHT};color:#fff;">
<th style="padding:10px 12px;text-align:left;width:80px;font-weight:500;">编号</th>
<th style="padding:10px 12px;text-align:left;font-weight:500;">标题</th>
<th style="padding:10px 12px;text-align:center;width:60px;font-weight:500;">下载</th>
</tr>
</thead>
<tbody>
"""
        for i, it in enumerate(eps_y):
            bg = STRIPE if i % 2 == 0 else "#fff"
            fn_short = (it.get("filename") or "").replace(".mp3", "")
            title = it.get("title", "?")
            url = it.get("download_url", "#")
            date = it.get("date", "")
            html += f"""<tr style="background:{bg};">
<td style="padding:10px 12px;color:#888;font-family:'SF Mono',Menlo,monospace;font-size:13px;">{fn_short}</td>
<td style="padding:10px 12px;color:#222;">{title}<div style="color:#999;font-size:12px;margin-top:2px;">{date}</div></td>
<td style="padding:10px 12px;text-align:center;"><a href="{url}" style="display:inline-block;width:30px;height:30px;line-height:30px;background:{GREEN};color:#fff;text-decoration:none;border-radius:50%;font-size:14px;">↓</a></td>
</tr>
"""
        html += "</tbody></table>"
    
    html += f"""
<div style="margin-top:40px;padding-top:20px;border-top:1px solid #eee;font-size:12px;color:#999;text-align:center;">
本邮件由自动归档系统生成 · 海马星球播客
</div>
</div>
</div>
</body></html>"""
    return html

def main():
    log(f"=== 模式: {MODE} | 收件人: {len(RCPT)}人 ===")
    mf = load_mf()
    mf.setdefault("filename_seq", {})
    
    # finalize 模式: 不下载,只重发 HTML 汇总到 3 邮箱
    if MODE == "finalize":
        log("Finalize 模式: 重发归档汇总到全部收件人")
        eps = fetch_eps()
        html = build_html_digest(mf, len(eps))
        mail(f"海马星球播客 · 全站归档（{len(mf['processed'])} 期）", html, RCPT_FULL, html=True)
        log("=== Finalize 完成 ===")
        return
    
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
            mf["processed"][ep["guid"]] = {"filename":r["fn"], "download_url":r["url"], "title":ep["title"], "date":ep["date"], "processed_at":datetime.now().isoformat()}
            save_mf(mf)
            # 仅 weekly 模式每期单发
            if MODE == "weekly":
                body = f"《{ep['title']}》\n\n录制日期: {ep['date']}\n文件大小: {r['sz']}MB\n下载地址: {r['url']}\n"
                mail(f"海马星球新音频 · {r['fn'].replace('.mp3','')}", body)
        else:
            log(f"失败: {ep['title']} - {err}")
            fail.append({**ep, "error":err})
    
    rem = total_new - len(succ)
    
    # full / test 模式邮件策略
    if MODE in ("full", "test"):
        if rem == 0:
            # 全部归档完成,发 HTML 汇总仅到主邮箱测试
            log("全部归档完成,生成 HTML 汇总邮件")
            html = build_html_digest(mf, len(eps))
            mail(f"🎉 海马星球播客 · 全站归档完成（{len(mf['processed'])} 期）测试版",
                 html, [QQ_EMAIL], html=True)
            mail("✅ 归档完成提示",
                 f"全部 {len(mf['processed'])} 期已归档完成,HTML 汇总邮件已发到主邮箱测试。\n\n确认 OK 后,请触发 Run workflow 选 finalize 模式,将汇总群发给所有 3 个收件人。",
                 [QQ_EMAIL])
        else:
            mail(f"海马星球归档进度 · {len(mf['processed'])}/{len(eps)}",
                 f"本批已完成 {len(succ)} 期,剩余 {rem} 期。\n请再次手动 Run workflow 继续(选 full)。\n",
                 [QQ_EMAIL])
    
    if fail:
        mail("⚠️ 海马星球部分失败", "失败清单:\n" + "\n".join(f"- {f['title']}: {f['error']}" for f in fail), [QQ_EMAIL])
    save_mf(mf)
    log(f"=== 完成 | 成功 {len(succ)} | 失败 {len(fail)} ===")

if __name__ == "__main__":
    main()
