# app.py
import os
import re
import json
import base64
import tempfile
import traceback
from urllib.parse import urljoin
from flask import Flask, request, jsonify, abort
import requests

# Playwright (sync API)
from playwright.sync_api import sync_playwright

# Data tools
import pandas as pd
import pdfplumber

app = Flask(__name__)

# Configure via env
SECRET = os.getenv("QUIZ_SECRET", "s3cr3t-llm-2025")  # override in prod
USER_AGENT = "LLM-Quiz-Agent/1.0"

# Helpers
def safe_json(req):
    try:
        return req.get_json(force=True)
    except Exception:
        return None

def find_submit_url(text, base_url=None):
    # common patterns: https://.../submit or "post to https://..."
    m = re.search(r"https?://[^\s'\"<>]+/submit[^\s'\"<>]*", text, re.I)
    if m:
        return m.group(0)
    # fallback: any https url that appears with "submit" within 200 chars
    for u in re.findall(r"https?://[^\s'\"<>]+", text):
        if "submit" in u:
            return u
    return None

def find_download_urls(text, base_url=None):
    urls = re.findall(r"https?://[^\s'\"<>]+", text)
    return urls

def extract_base64_from_atob_js(page_text):
    # finds atob(`...`) or atob("...") patterns and returns decoded text blocks
    out = []
    for m in re.finditer(r'atob\((?:`([^`]*)`|"([^"]*)"|\'([^\']*)\')\)', page_text, re.I|re.S):
        bs = m.group(1) or m.group(2) or m.group(3)
        try:
            dec = base64.b64decode(bs).decode('utf-8', errors='replace')
            out.append(dec)
        except Exception:
            pass
    return out

def download_file(url, headers=None):
    headers = headers or {}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    # save to temp file
    tmp = tempfile.NamedTemporaryFile(delete=False)
    tmp.write(resp.content)
    tmp.flush()
    tmp.close()
    return tmp.name, resp.headers.get('Content-Type', '')

def process_csv(path):
    df = pd.read_csv(path)
    return df

def sum_value_column_if_exists(df):
    # attempt many variants of 'value' column
    candidates = [c for c in df.columns if c.strip().lower() in ("value","amount","val","total")]
    if not candidates:
        # if any numeric columns, sum first numeric column
        numeric_cols = df.select_dtypes(include='number').columns.tolist()
        if numeric_cols:
            return float(df[numeric_cols[0]].sum())
        return None
    return float(df[candidates[0]].sum())

def process_pdf_for_table_sum(path):
    # simplistic: search text for "value" columns on page 2 if possible
    try:
        with pdfplumber.open(path) as pdf:
            if len(pdf.pages) >= 2:
                page = pdf.pages[1]
            else:
                page = pdf.pages[0]
            # try extract_table
            try:
                table = page.extract_table()
                if table:
                    df = pd.DataFrame(table[1:], columns=table[0])
                    return sum_value_column_if_exists(df)
            except Exception:
                pass
            # fallback: regex numbers
            text = page.extract_text() or ""
            nums = re.findall(r"[-+]?\d*\.\d+|\d+", text)
            nums = [float(n) for n in nums]
            if nums:
                return sum(nums)
    except Exception:
        pass
    return None

def post_answer(submit_url, payload):
    headers = {"Content-Type":"application/json", "User-Agent": USER_AGENT}
    resp = requests.post(submit_url, headers=headers, json=payload, timeout=30)
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"text": resp.text}

@app.route("/api/quiz", methods=["POST"])
def quiz_handler():
    data = safe_json(request)
    if data is None:
        return ("Invalid JSON", 400)

    email = data.get("email")
    secret = data.get("secret")
    url = data.get("url")
    if not (email and secret and url):
        return jsonify({"error": "missing fields (email, secret, url required)"}), 400

    if secret != SECRET:
        return jsonify({"error": "invalid secret"}), 403

    # Respond 200 quickly to acknowledge secret match, but still return helpful diagnostics
    # We'll perform the solving and include results in the JSON response.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()
            page.goto(url, timeout=60000)  # 60s
            page.wait_for_load_state("networkidle", timeout=15000)

            # collect DOM text and HTML
            page_text = page.content()
            visible_text = page.inner_text("body")

            # try to decode any atob-embedded base64
            decoded_blocks = extract_base64_from_atob_js(page_text)

            # find submit url
            submit_url = find_submit_url(page_text) or find_submit_url(visible_text)
            if submit_url and not submit_url.startswith("http"):
                submit_url = urljoin(url, submit_url)

            # find download URLs and pick plausible ones
            downloads = find_download_urls(page_text + "\n" + visible_text)
            downloads = [d for d in downloads if d != url]  # exclude the page itself

            # Also scan decoded blocks for urls/instructions
            for blk in decoded_blocks:
                downloads += find_download_urls(blk)
                if not submit_url:
                    submit_url = find_submit_url(blk)
            downloads = list(dict.fromkeys(downloads))  # unique

            # Some pages include instruction JSON in a <pre> - attempt to parse
            instruction_json = None
            try:
                pre_texts = page.eval_on_selector_all("pre", "elements => elements.map(e=>e.innerText)")
                for t in pre_texts:
                    try:
                        obj = json.loads(t)
                        instruction_json = obj
                        break
                    except Exception:
                        pass
            except Exception:
                pass

            answer_payload = None
            # If instruction JSON present and includes "answer" instruction, follow that
            if instruction_json:
                # try to parse common sample structure
                if "url" in instruction_json and "answer" in instruction_json:
                    # maybe sample, but we expect to compute answer; leave for logic below
                    pass

            # Attempt to solve a typical 'sum value column' question:
            computed_answer = None
            used_file = None

            # Try downloads one by one until we find a file we can work on
            for d in downloads:
                try:
                    fname, ctype = download_file(d, headers={"User-Agent": USER_AGENT})
                except Exception:
                    continue
                used_file = fname
                if ctype.startswith("text/csv") or fname.lower().endswith(".csv"):
                    df = process_csv(fname)
                    s = sum_value_column_if_exists(df)
                    if s is not None:
                        computed_answer = s
                        break
                elif ctype.startswith("application/json") or fname.lower().endswith(".json"):
                    with open(fname, "r", encoding="utf-8") as fh:
                        obj = json.load(fh)
                    # if it's a list of dicts
                    if isinstance(obj, list):
                        df = pd.DataFrame(obj)
                        s = sum_value_column_if_exists(df)
                        if s is not None:
                            computed_answer = s
                            break
                else:
                    # try PDF
                    if fname.lower().endswith(".pdf") or "pdf" in ctype:
                        s = process_pdf_for_table_sum(fname)
                        if s is not None:
                            computed_answer = s
                            break
                    # try reading text file
                    try:
                        with open(fname, "r", encoding="utf-8") as fh:
                            txt = fh.read()
                        # try to find numeric sum in text
                        nums = re.findall(r"[-+]?\d*\.\d+|\d+", txt)
                        if nums:
                            computed_answer = sum(float(n) for n in nums)
                            break
                    except Exception:
                        pass

            # If we didn't find downloads, try analyzing visible_text for a direct answer
            if computed_answer is None:
                # look for phrases like "sum of the "value" column is X"
                m = re.search(r'sum of the ["“]?value["”]? column.*?([+-]?\d[\d,\.]*)', visible_text, re.I|re.S)
                if m:
                    computed_answer = float(m.group(1).replace(",",""))
                else:
                    # attempt to find a number near the question line
                    m2 = re.search(r'What is .*sum.*\?[\s\S]*?([-+]?\d[\d,\.]+)', visible_text, re.I)
                    if m2:
                        computed_answer = float(m2.group(1).replace(",",""))

            # Build submission payload
            if computed_answer is not None and submit_url:
                payload = {
                    "email": email,
                    "secret": secret,
                    "url": url,
                    "answer": computed_answer
                }
                status_code, submit_resp = post_answer(submit_url, payload)
                result = {
                    "status": "submitted",
                    "submit_status": status_code,
                    "submit_response": submit_resp,
                    "computed_answer": computed_answer,
                    "used_file": used_file,
                    "submit_url": submit_url
                }
            else:
                result = {
                    "status": "unable_to_solve",
                    "computed_answer": computed_answer,
                    "found_downloads": downloads,
                    "decoded_blocks": decoded_blocks[:3],  # small sample
                    "submit_url": submit_url
                }

            browser.close()
            return jsonify({"ok": True, "result": result}), 200

    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({"ok": False, "error": str(e), "trace": tb}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
