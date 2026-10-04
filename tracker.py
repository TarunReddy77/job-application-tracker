#!/usr/bin/env python3
"""Daily HiringCafe tracker: fetch -> dedupe -> email new -> rebuild UI.

Plain requests/curl get a Cloudflare challenge; curl_cffi with Chrome TLS
impersonation gets through. Env: GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO
(email is skipped when unset, e.g. local dry runs).
"""
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from curl_cffi import requests

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text())
JOBS_PATH = ROOT / "data" / "jobs.json"
UI_PATH = ROOT / "docs" / "index.html"
BASE = "https://hiringcafe.com"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

session = requests.Session(impersonate="chrome")


def get_page_props(url, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=45)
            m = NEXT_DATA.search(r.text) if r.status_code == 200 else None
            if m:
                return json.loads(m.group(1))["props"]["pageProps"]
            err = f"HTTP {r.status_code}, no __NEXT_DATA__ (cf-mitigated={r.headers.get('cf-mitigated')})"
        except Exception as e:  # network/parse errors
            err = repr(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url[:90]}... failed: {err}")


def search(query):
    state = {"searchQuery": query, "dateFetchedPastNDays": CONFIG["past_n_days"]}
    for key, cfg in [("workplaceTypes", "workplace_types"),
                     ("seniorityLevel", "seniority_levels"),
                     ("locations", "locations")]:
        if CONFIG.get(cfg):
            state[key] = CONFIG[cfg]
    qs = urllib.parse.quote(json.dumps(state, separators=(",", ":")))
    hits, seen = [], set()
    for page in range(CONFIG["max_pages"]):
        pp = get_page_props(f"{BASE}/classic?searchState={qs}&page={page}")
        fresh = [h for h in pp["ssrHits"] if h["objectID"] not in seen]
        if not fresh:
            break
        seen.update(h["objectID"] for h in fresh)
        hits += fresh
        time.sleep(1)  # hit counts per page are irregular, so stop only on an empty page
    return hits


jd_blocked = False  # set on a 429/challenge so the rest of the run stops hammering detail pages


def fetch_description(hit):
    # /job/<requisition_id> resolves without the pretty slug.
    global jd_blocked
    if jd_blocked:
        return None
    try:
        r = session.get(f"{BASE}/job/{hit['requisition_id']}", timeout=45)
        m = NEXT_DATA.search(r.text) if r.status_code == 200 else None
        if not m:
            jd_blocked = r.status_code in (403, 429)
            raise RuntimeError(f"HTTP {r.status_code}")
        return json.loads(m.group(1))["props"]["pageProps"]["job"]["job_information"]["description"]
    except Exception as e:
        print(f"  JD fetch failed for {hit['objectID']}: {e}", file=sys.stderr)
        return None


def to_record(hit, query):
    p = hit["v5_processed_job_data"]
    return {
        "id": hit["objectID"],
        "req_id": hit["requisition_id"],
        "title": p.get("core_job_title") or hit["job_information"].get("title"),
        "company": p.get("company_name"),
        "location": p.get("formatted_workplace_location"),
        "workplace_type": p.get("workplace_type"),
        "pay_min": p.get("yearly_min_compensation"),
        "pay_max": p.get("yearly_max_compensation"),
        "published_ms": p.get("estimated_publish_date_millis"),
        "seniority": p.get("seniority_level"),
        "role_type": p.get("role_type"),
        "clearance": p.get("security_clearance"),
        "apply_url": hit.get("apply_url"),
        "hc_url": f"{BASE}/job/{hit['requisition_id']}",
        "queries": [query],
        "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "description": None,
        # reserved for the future resume-tailoring agent
        "status": "new",  # new | tailored | applied | skipped
        "tailored": None,
    }


def fmt_pay(r):
    lo, hi = r.get("pay_min"), r.get("pay_max")
    k = lambda v: f"${v / 1000:.0f}k"
    if lo and hi:
        return f"{k(lo)}-{k(hi)}"
    return k(lo or hi) if (lo or hi) else "—"


def fmt_age(ms):
    if not ms:
        return "—"
    h = (time.time() * 1000 - ms) / 3.6e6
    return f"{max(h, 0):.0f}h ago" if h < 48 else f"{h / 24:.0f}d ago"


def send_mail(subject, text, html_body=None):
    user, pw, to = (os.environ.get(k) for k in ("GMAIL_USER", "GMAIL_APP_PASSWORD", "MAIL_TO"))
    if not (user and pw and to):
        print(f"[mail skipped: secrets unset] {subject}")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def new_jobs_email(new):
    cap = CONFIG["max_in_email"]
    shown = sorted(new, key=lambda r: r.get("published_ms") or 0, reverse=True)[:cap]
    e = html.escape
    rows, lines = [], []
    for r in shown:
        meta = f"{r['company']} · {r['location']} · {r['workplace_type']} · {fmt_pay(r)} · {fmt_age(r['published_ms'])}"
        link = r["apply_url"] or r["hc_url"]
        rows.append(f'<p style="margin:0 0 14px"><a href="{e(link)}"><b>{e(r["title"] or "")}</b></a><br>'
                    f'<span style="color:#555">{e(meta)}</span> · <a href="{e(r["hc_url"])}">HiringCafe</a></p>')
        lines.append(f"{r['title']}\n  {meta}\n  {link}")
    more = f"\n(+{len(new) - cap} more — see the site)" if len(new) > cap else ""
    site = os.environ.get("SITE_URL", "")
    foot = f'<p><a href="{e(site)}">Browse all jobs</a></p>' if site else ""
    return "\n\n".join(lines) + more, "".join(rows) + (f"<p>+{len(new) - cap} more</p>" if more else "") + foot


def build_ui(jobs):
    items = sorted(jobs.values(), key=lambda r: r.get("published_ms") or 0, reverse=True)
    data = json.dumps(items, separators=(",", ":"), ensure_ascii=False)
    data = data.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = (ROOT / "ui_template.html").read_text()
    UI_PATH.write_text(page.replace("__JOBS__", data).replace("__UPDATED__", updated))


def main():
    jobs = json.loads(JOBS_PATH.read_text()) if JOBS_PATH.exists() else {}
    seeding = not jobs
    new, errors = [], []
    for q in CONFIG["queries"]:
        try:
            hits = search(q)
        except Exception as e:
            errors.append(f"{q}: {e}")
            print("ERROR", errors[-1], file=sys.stderr)
            continue
        added = 0
        for h in hits:
            rec = jobs.get(h["objectID"])
            if rec:
                if q not in rec["queries"]:
                    rec["queries"].append(q)
                continue
            rec = jobs[h["objectID"]] = to_record(h, q)
            new.append(rec)
            added += 1
        print(f"{q!r}: {len(hits)} hits, {added} new")

    # JDs are fetched lazily, newest first, capped per run: detail pages rate-limit (429) fast.
    missing = sorted((r for r in jobs.values() if not r["description"]),
                     key=lambda r: r.get("published_ms") or 0, reverse=True)
    for rec in missing[:CONFIG["max_jd_fetches"]]:
        rec["description"] = fetch_description({"objectID": rec["id"], "requisition_id": rec["req_id"]})
        time.sleep(3)
    print(f"JDs: {sum(1 for r in jobs.values() if r['description'])}/{len(jobs)} stored")

    if len(errors) == len(CONFIG["queries"]):
        send_mail("HiringCafe tracker: ALL queries failed", "\n".join(errors))
        sys.exit(1)

    JOBS_PATH.write_text(json.dumps(jobs, indent=1, ensure_ascii=False, sort_keys=True))
    build_ui(jobs)

    warn = ("\n\nWarnings:\n" + "\n".join(errors)) if errors else ""
    site = os.environ.get("SITE_URL", "")
    if seeding:
        send_mail(f"HiringCafe tracker seeded with {len(new)} jobs",
                  f"First run: stored {len(new)} jobs silently. Daily alerts start tomorrow.\n{site}{warn}")
    elif new:
        text, body = new_jobs_email(new)
        send_mail(f"{len(new)} new HiringCafe jobs", text + warn, body + (f"<pre>{html.escape(warn)}</pre>" if warn else ""))
    else:
        send_mail("HiringCafe tracker: 0 new jobs", f"Ran OK. {len(jobs)} jobs stored.{warn}")
    print(f"done: {len(new)} new, {len(jobs)} total, {len(errors)} query errors")
    if errors:
        sys.exit(2)


if __name__ == "__main__":
    main()
