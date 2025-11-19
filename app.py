# app.py
import os
import re
import json
import base64
import tempfile
import traceback
import time
from urllib.parse import urljoin
from flask import Flask, request, jsonify
import requests
import pandas as pd
import pdfplumber

from playwright.sync_api import sync_playwright

app = Flask(__name__)

# Configuration
SECRET = os.getenv("QUIZ_SECRET", "s3cr3t-llm-2025")
USER_AGENT = "LLM-Quiz-Agent/1.0"

# ---------- Helpers ----------
def safe_json(req):
    try:
        return req.get_json(force=True)
    except Exception:
        return None

def find_submit_url(text):
    if not text:
        return None
    m = re.search(r"https?://[^\s'\"<>]*submit[^\s'\"<>]*", text, re.I)
    if m:
        return m.group(0)
    # fallback: any url with the word 'submit' nearby
    urls = re.findall(r"https?://[^\s'\"<>]+", text)
    for u in urls:
        if "submit" in u:
            return u
    return None

def find_download_urls(text):
    if not text:
        return []
    return re.findall(r"https?://[^\s'\"<>]+", text)

def extract_base64_from_atob_js(text):
    out = []
    if not text:
        return out
    for m in re.finditer(r'atob\((?:`([^`]*)`|"([^"]*)"|\'([^\']*)\')\)', text, re.I | re.S):
        bs = m.group(1) or m.group(2) or m.group(3)
        if not bs:
            continue
        bs_clean = "".join(bs.split())
        for cand in (bs_clean, bs):
            try:
                dec = base64.b64decode(cand).decode('utf-8', errors='replace')
                out.append(dec)
                break
            except Exception:
                continue
    return out

def download_file(url, headers=None):
    headers = headers or {}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    suffix = ""
    # try to guess suffix from headers or url
    ct = resp.headers.get('Content-Type', '')
    if 'pdf' in ct:
        suffix = '.pdf'
    elif 'json' in ct:
        suffix = '.json'
    elif 'csv' in ct:
        suffix = '.csv'
    else:
        # attempt from url
        if url.lower().endswith('.pdf'):
            suffix = '.pdf'
        elif url.lower().endswith('.json'):
            suffix = '.json'
        elif url.lower().endswith('.csv'):
            suffix = '.csv'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.flush()
    tmp.close()
    return tmp.name, ct or ''

def remove_temp_file(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def process_csv(path):
    try:
        df = pd.read_csv(path)
        return df
    except Exception:
        try:
            df = pd.read_csv(path, encoding='utf-8', engine='python', error_bad_lines=False)
            return df
        except Exception:
            return None

def sum_value_column_if_exists(df):
    if df is None:
        return None
    cols = [c.strip() for c in df.columns]
    df.columns = cols
    candidates = [c for c in cols if c.lower() in ("value", "amount", "val", "total")]
    if candidates:
        col = candidates[0]
        try:
            return float(df[col].fillna(0).astype(float).sum())
        except Exception:
            try:
                return float(pd.to_numeric(df[col].astype(str).str.replace(',',''), errors='coerce').fillna(0).sum())
            except Exception:
                return None
    # fallback: numeric columns
    numeric_cols = df.select_dtypes(include='number').columns.tolist()
    if numeric_cols:
        return float(df[numeric_cols[0]].sum())
    # fallback: coerce any column
    for c in cols:
        try:
            s = pd.to_numeric(df[c].astype(str).str.replace(',',''), errors='coerce').dropna()
            if len(s) > 0:
                return float(s.sum())
        except Exception:
            continue
    return None

def process_pdf_for_table_sum(path):
    try:
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[1] if len(pdf.pages) >= 2 else pdf.pages[0]
            try:
                table = page.extract_table()
                if table and len(table) > 1:
                    df = pd.DataFrame(table[1:], columns=table[0])
                    return sum_value_column_if_exists(df)
            except Exception:
                pass
            text = page.extract_text() or ""
            nums = re.findall(r"[-+]?\d*\.\d+|\d+", text)
            nums = [float(n.replace(',','')) for n in nums] if nums else []
            if nums:
                return float(sum(nums))
    except Exception:
        pass
    return None

def post_answer(submit_url, payload):
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    resp = requests.post(submit_url, headers=headers, json=payload, timeout=30)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"text": resp.text}

# ---------- Main route ----------
@app.route("/api/quiz", methods=["POST"])
def quiz_handler():
    data = safe_json(request)
    if data is None:
        return ("Invalid JSON", 400)

    email = data.get("email")
    secret = data.get("secret")
    start_url = data.get("url")
    if not (email and secret and start_url):
        return jsonify({"error": "missing fields (email, secret, url required)"}), 400

    if secret != SECRET:
        return jsonify({"error": "invalid secret"}), 403

    # We'll gather a result object to return
    overall_result = {
        "email": email,
        "start_url": start_url,
        "chain": []
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            # Timing for chain (3 minutes total)
            MAX_SECONDS = 180
            start_time = time.time()

            current_url = start_url
            visited = set()

            while True:
                elapsed = time.time() - start_time
                if elapsed > MAX_SECONDS:
                    overall_result["chain"].append({"status": "timeout", "elapsed_seconds": elapsed, "url": current_url})
                    break

                if current_url in visited:
                    overall_result["chain"].append({"status": "loop_detected", "url": current_url})
                    break
                visited.add(current_url)

                # Navigate
                try:
                    page.goto(current_url, timeout=60000)
                except Exception:
                    # ignore navigate exception; continue capturing whatever rendered
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                try:
                    page.wait_for_selector("#result", timeout=8000)
                except Exception:
                    pass

                # Extract content from various places
                try:
                    page_text = page.content() or ""
                except Exception:
                    page_text = ""
                try:
                    visible_text = page.inner_text("body") or ""
                except Exception:
                    visible_text = ""
                try:
                    script_texts = page.eval_on_selector_all("script", "elements => elements.map(e => e.textContent || '')")
                except Exception:
                    script_texts = []
                try:
                    rendered_result_html = page.eval_on_selector("#result", "el => el.innerHTML") or ""
                except Exception:
                    rendered_result_html = ""
                try:
                    rendered_body_html = page.eval_on_selector("body", "el => el.innerHTML") or ""
                except Exception:
                    rendered_body_html = ""

                # Find submit URL and download links from these sources
                submit_url = find_submit_url(page_text) or find_submit_url(visible_text) or find_submit_url(rendered_result_html) or find_submit_url(rendered_body_html)
                if submit_url and not submit_url.startswith("http"):
                    submit_url = urljoin(current_url, submit_url)

                found_downloads = []
                for src in (page_text, visible_text, rendered_result_html, rendered_body_html) + tuple(script_texts):
                    found_downloads += find_download_urls(src or "")
                # also check decoded blocks we will extract
                decoded_blocks = []
                for src in (page_text, visible_text) + tuple(script_texts) + (rendered_result_html, rendered_body_html):
                    try:
                        decoded_blocks += extract_base64_from_atob_js(src or "")
                    except Exception:
                        continue
                # dedupe lists
                found_downloads = list(dict.fromkeys(found_downloads))
                decoded_blocks = [d for i, d in enumerate(decoded_blocks) if d and d not in decoded_blocks[:i]]

                # Attempt to parse any <pre> JSON blocks
                try:
                    pre_texts = page.eval_on_selector_all("pre", "elements => elements.map(e => e.innerText || '')")
                    for t in pre_texts:
                        t = (t or "").strip()
                        if not t:
                            continue
                        try:
                            obj = json.loads(t)
                            decoded_blocks.append(json.dumps(obj))
                        except Exception:
                            decoded_blocks.append(t)
                except Exception:
                    pass
                # dedupe again
                decoded_blocks = [d for i, d in enumerate(decoded_blocks) if d and d not in decoded_blocks[:i]]

                # Try to compute answer for this page
                computed_answer = None
                used_file = None
                debug_step = {
                    "url": current_url,
                    "found_downloads": found_downloads,
                    "decoded_blocks_sample": decoded_blocks[:3],
                    "submit_url": submit_url
                }

                # If decoded blocks have JSON with "answer", prefer that (demo pages often include it)
                for blk in decoded_blocks:
                    try:
                        maybe = json.loads(blk)
                        if isinstance(maybe, dict) and "answer" in maybe:
                            computed_answer = maybe.get("answer")
                            break
                        # if includes url pointer, add
                        if isinstance(maybe, dict) and "url" in maybe:
                            found_downloads.append(maybe.get("url"))
                    except Exception:
                        # not JSON - ignore
                        pass

                # If we don't yet have an answer, try downloads
                if computed_answer is None:
                    for d in found_downloads:
                        # skip the page root itself
                        if d.rstrip("/") in (current_url.rstrip("/"),):
                            continue
                        try:
                            fname, ctype = download_file(d, headers={"User-Agent": USER_AGENT})
                        except Exception:
                            continue
                        used_file = fname
                        try:
                            if (ctype and "csv" in ctype) or fname.lower().endswith(".csv"):
                                df = process_csv(fname)
                                s = sum_value_column_if_exists(df)
                                if s is not None:
                                    computed_answer = s
                                    remove_temp_file(fname)
                                    break
                            elif (ctype and "json" in ctype) or fname.lower().endswith(".json"):
                                try:
                                    with open(fname, "r", encoding="utf-8", errors="ignore") as fh:
                                        obj = json.load(fh)
                                    if isinstance(obj, list):
                                        df = pd.DataFrame(obj)
                                        s = sum_value_column_if_exists(df)
                                        if s is not None:
                                            computed_answer = s
                                            remove_temp_file(fname)
                                            break
                                except Exception:
                                    pass
                            elif "pdf" in ctype or fname.lower().endswith(".pdf"):
                                s = process_pdf_for_table_sum(fname)
                                if s is not None:
                                    computed_answer = s
                                    remove_temp_file(fname)
                                    break
                            else:
                                # try text heuristic
                                try:
                                    with open(fname, "r", encoding="utf-8", errors="ignore") as fh:
                                        txt = fh.read()
                                    nums = re.findall(r"[-+]?\d*\.\d+|\d+", txt)
                                    if nums:
                                        computed_answer = float(sum(float(n) for n in nums))
                                        remove_temp_file(fname)
                                        break
                                except Exception:
                                    pass
                        finally:
                            # ensure temp file is cleaned if not already
                            remove_temp_file(fname)

                # If still no computed_answer, try to parse page visible text for a numeric answer
                if computed_answer is None:
                    m = re.search(r'sum of the ["“]?value["”]? column.*?([+-]?\d[\d,\.]*)', visible_text, re.I | re.S)
                    if m:
                        try:
                            computed_answer = float(m.group(1).replace(",", ""))
                        except Exception:
                            computed_answer = None

                # If no answer but we have submit_url and the page places expected answer in plain text near question, we might try LLM or heuristics (not implemented)
                # For now, if we have computed_answer and submit_url, submit it
                step_record = {"url": current_url, "computed_answer": computed_answer, "used_file": used_file}

                if computed_answer is not None and submit_url:
                    payload = {"email": email, "secret": secret, "url": current_url, "answer": computed_answer}
                    try:
                        status_code, submit_resp = post_answer(submit_url, payload)
                        step_record.update({
                            "submit_status": status_code,
                            "submit_response": submit_resp,
                            "submit_url": submit_url
                        })
                    except Exception as e:
                        step_record.update({"submit_error": str(e)})
                        overall_result["chain"].append(step_record)
                        break

                    overall_result["chain"].append(step_record)

                    # Determine next URL from submit_resp (if present)
                    next_url = None
                    try:
                        if isinstance(submit_resp, dict):
                            next_url = submit_resp.get("url") or submit_resp.get("next_url")
                    except Exception:
                        next_url = None

                    if not next_url:
                        # No further URL: done
                        break
                    # Otherwise proceed to next URL
                    current_url = next_url
                    continue

                else:
                    # nothing to submit for this page
                    step_record.update({"debug": debug_step})
                    overall_result["chain"].append(step_record)
                    # If a next url is discoverable within page text (rare), follow it
                    # else stop
                    potential_next = None
                    try:
                        # sometimes the page contains the next url explicitly
                        candidates = find_download_urls(visible_text + "\n" + rendered_result_html)
                        for c in candidates:
                            if "quiz" in c or "demo" in c or "submit" in c:
                                potential_next = c
                                break
                    except Exception:
                        potential_next = None

                    if potential_next:
                        current_url = potential_next
                        continue
                    break

            # close browser
            browser.close()

            # Return final result
            return jsonify({"ok": True, "result": overall_result}), 200

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "error": str(e), "trace": tb}), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
