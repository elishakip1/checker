from flask import Flask, request, render_template, jsonify
import requests
import random
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
from datetime import datetime
import re
import os
import json

# --- Google Sheets Imports ---
import gspread
from oauth2client.service_account import ServiceAccountCredentials
# --- End Google Sheets Imports ---


# Disable SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Default configuration values
DEFAULT_SETTINGS = {
    "MAX_PASTE": 150,
    "MAX_WORKERS": 15, # Fallback/local value, gunicorn.conf.py overrides on Render
}

class MemoryStorage:
    def __init__(self):
        self.settings = DEFAULT_SETTINGS.copy()

storage = MemoryStorage()

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0",
]

REQUEST_TIMEOUT = 15
MIN_DELAY = 0.5
MAX_DELAY = 1.5

# --- Google Sheets Configuration ---
SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
JSON_CREDS_STR = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_NAME = "Used IPs" # Make sure this matches your Google Sheet name
WORKSHEET_NAME = "Sheet1" # Make sure this matches your worksheet tab name
IP_COLUMN_INDEX = 1 # Assuming IP is in the first column (A)

_sheet_cache = None
def get_sheet():
    """Gets the specific worksheet, using a simple cache."""
    global _sheet_cache
    if _sheet_cache:
        try:
            _sheet_cache.acell('A1').value # Check connection
            return _sheet_cache
        except Exception as e:
            logger.warning(f"Cached sheet connection stale ({e}), re-authorizing...")
            _sheet_cache = None

    if not JSON_CREDS_STR:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON env var not set.")
        raise ValueError("Missing Google credentials.")
    try:
        creds_dict = json.loads(JSON_CREDS_STR)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
        client = gspread.authorize(creds)
        spreadsheet = client.open(SHEET_NAME)
        sheet = spreadsheet.worksheet(WORKSHEET_NAME)
        _sheet_cache = sheet
        logger.info(f"Connected to Google Sheet: {SHEET_NAME}/{WORKSHEET_NAME}")
        return sheet
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(f"Spreadsheet '{SHEET_NAME}' not found or permission denied.")
        _sheet_cache = None
        raise
    except gspread.exceptions.WorksheetNotFound:
        logger.error(f"Worksheet '{WORKSHEET_NAME}' not found in spreadsheet '{SHEET_NAME}'.")
        _sheet_cache = None
        raise
    except Exception as e:
        logger.error(f"Error opening Google Sheet: {e}")
        _sheet_cache = None
        raise

def append_used_ip(ip, proxy_string):
    """Appends the IP, proxy string, and timestamp to the Google Sheet."""
    global _sheet_cache
    try:
        sheet = get_sheet()
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        sheet.append_row([ip, proxy_string, timestamp])
        logger.info(f"Appended used IP {ip} to Google Sheet.")
        return True
    except Exception as e:
        logger.error(f"Failed to append IP {ip} to Google Sheet: {e}")
        _sheet_cache = None # Reset cache on failure
        return False

_used_ips_cache = None
_cache_expiry = 0
CACHE_DURATION_SECONDS = 60 # Cache used IPs for 60 seconds

def get_used_ips_set():
    """Fetches all IPs from the sheet and returns them as a set. Uses caching."""
    global _used_ips_cache, _cache_expiry, _sheet_cache
    current_time = time.time()

    if _used_ips_cache is not None and current_time < _cache_expiry:
        return _used_ips_cache

    logger.info("Fetching used IPs from Google Sheet...")
    try:
        sheet = get_sheet()
        ip_list = sheet.col_values(IP_COLUMN_INDEX)[1:] # Assumes header in row 1
        used_ips = set(ip for ip in ip_list if ip)
        _used_ips_cache = used_ips
        _cache_expiry = current_time + CACHE_DURATION_SECONDS
        logger.info(f"Fetched and cached {len(used_ips)} used IPs.")
        return used_ips
    except Exception as e:
        logger.error(f"Failed to fetch used IPs from Google Sheet: {e}")
        _sheet_cache = None # Reset sheet cache as well
        return set() # Return an empty set on error
# --- End Google Sheets Configuration ---


def validate_proxy_format(proxy_line):
    # (Existing function)
    try:
        parts = proxy_line.strip().split(":")
        if len(parts) == 4:
            host, port, user, password = parts
            if host and port and user and password:
                return True
        return False
    except Exception as e:
        return False


def get_ip_from_proxy(proxy_line):
    # (Existing function)
    if not validate_proxy_format(proxy_line):
        return None

    try:
        host, port, user, pw = proxy_line.strip().split(":")
        proxies = {
            "http": f"http://{user}:{pw}@{host}:{port}",
            "https": f"http://{user}:{pw}@{host}:{port}",
        }

        session = requests.Session()
        retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        session.mount('http://', HTTPAdapter(max_retries=retries))
        session.mount('https://', HTTPAdapter(max_retries=retries))

        response = session.get(
            "https://api.ipify.org",
            proxies=proxies,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            verify=False
        )

        if response.status_code == 200:
            ip = response.text.strip()
            if ip and '.' in ip and 7 <= len(ip) <= 15:
                return {"proxy": proxy_line, "ip": ip}
            else:
                logger.warning(f"❌ Got invalid IP '{ip}' for proxy {host}")
                return None
        else:
            return None
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.ProxyError:
         return None
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ RequestException for {proxy_line.split(':')[0]}: {e}")
        return None
    except Exception as e:
        logger.error(f"💥 Unexpected error for {proxy_line.split(':')[0]}: {e}")
        return None


def single_check_proxy(proxy_line):
    # (Existing function)
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    ip_data = get_ip_from_proxy(proxy_line)
    return ip_data


@app.route("/", methods=["GET", "POST"])
def index():
    # (Updated logic for checking used IPs)
    settings = storage.settings
    MAX_PASTE = settings["MAX_PASTE"]
    MAX_WORKERS = int(os.environ.get('GUNICORN_WORKERS', settings["MAX_WORKERS"]))

    results_final = []
    message = ""

    if request.method == "POST":
        proxies = []
        all_lines = []
        input_count = 0

        if 'proxyfile' in request.files and request.files['proxyfile'].filename:
            try:
                file = request.files['proxyfile']
                content = file.read().decode("utf-8", errors='ignore')
                all_lines = content.strip().splitlines()
                input_count = len(all_lines)
                logger.info(f"Processing {input_count} lines from file.")
            except Exception as e:
                logger.error(f"Error reading uploaded file: {e}")
                message = "Error reading the uploaded file. Ensure it's valid text."
                return render_template("index.html", results=[], message=message, max_paste=MAX_PASTE, settings=settings)

        elif 'proxytext' in request.form and request.form['proxytext'].strip():
            proxytext = request.form.get("proxytext", "")
            all_lines = proxytext.strip().splitlines()
            input_count = len(all_lines)
            logger.info(f"Processing {input_count} lines from text area.")
        else:
             logger.info("No proxy input provided.")
             message = "Please upload a file or paste proxies."
             return render_template("index.html", results=[], message=message, max_paste=MAX_PASTE, settings=settings)

        original_input_count = input_count
        if input_count > MAX_PASTE:
            logger.warning(f"Input truncated from {input_count} to {MAX_PASTE} lines.")
            all_lines = all_lines[:MAX_PASTE]
            input_count = MAX_PASTE
        proxies = all_lines

        valid_proxies = []
        invalid_format_count = 0
        for proxy in proxies:
            proxy = proxy.strip()
            if proxy:
                if validate_proxy_format(proxy):
                    valid_proxies.append(proxy)
                else:
                    invalid_format_count += 1
        if invalid_format_count > 0:
             logger.warning(f"Skipped {invalid_format_count} lines due to invalid format.")

        processed_count = len(valid_proxies)
        intermediate_results = []

        if valid_proxies:
            logger.info(f"🔄 Extracting IPs from {processed_count} valid proxies using {MAX_WORKERS} workers...")
            start_time = time.time()

            try:
                with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                    futures = [executor.submit(single_check_proxy, proxy) for proxy in valid_proxies]
                    completed_count = 0
                    for future in as_completed(futures):
                        try:
                            result = future.result()
                            completed_count += 1
                            if result:
                                intermediate_results.append(result)
                            if completed_count % 25 == 0 or completed_count == processed_count:
                                logger.info(f"Progress: {completed_count}/{processed_count} processed...")
                        except Exception as exc:
                            logger.error(f'Worker task generated an exception: {exc}')

                end_time = time.time()
                processing_duration = end_time - start_time
                logger.info(f"⏰ IP extraction finished in {processing_duration:.2f} seconds.")

                # --- Check Used IPs ---
                if intermediate_results:
                    try:
                        used_ips_set = get_used_ips_set() # Fetch used IPs
                        for item in intermediate_results:
                            item['used'] = item['ip'] in used_ips_set # Add 'used' key
                            results_final.append(item)
                        logger.info(f"Checked {len(results_final)} extracted IPs against used list.")
                    except Exception as e:
                        logger.error(f"Error checking used IPs: {e}. Proceeding without usage status.")
                        results_final = intermediate_results # Fallback
                # --- End Check Used IPs ---


                if results_final:
                    message = f"✅ Extracted {len(results_final)} IPs from {processed_count} valid proxies ({processing_duration:.2f}s). See table."
                    if invalid_format_count > 0:
                        message += f" ({invalid_format_count} lines skipped)."
                    logger.info(f"🎉 Final result: {len(results_final)} IPs extracted!")
                else:
                    message = f"⚠️ Processed {processed_count} valid proxies ({processing_duration:.2f}s), but couldn't extract IPs."
                    if invalid_format_count > 0:
                        message += f" ({invalid_format_count} lines skipped)."
                    logger.warning(f"😞 No IPs extracted from {processed_count} valid proxies")

            except Exception as e:
                logger.exception("Error during ThreadPoolExecutor execution")
                message = "An error occurred during processing. Check logs."

        elif original_input_count > 0:
             message = f"❌ Submitted {original_input_count} lines, but none had valid format: host:port:user:pass"

    return render_template("index.html", results=results_final, message=message, max_paste=MAX_PASTE, settings=settings)


@app.route("/track-used", methods=["POST"])
def track_used():
    # (Existing endpoint logic, including cache invalidation)
    data = request.get_json()
    if not data or "proxy" not in data:
        logger.warning("Invalid request to /track-used: Missing proxy data.")
        return jsonify({"status": "error", "message": "Invalid request body"}), 400

    proxy_string = data["proxy"]
    if not validate_proxy_format(proxy_string):
         logger.warning(f"Invalid proxy format received at /track-used: {proxy_string}")
         return jsonify({"status": "error", "message": "Invalid proxy format"}), 400

    logger.info(f"Received request to track proxy: {proxy_string.split(':')[0]}")

    ip_data = get_ip_from_proxy(proxy_string) # Re-extract IP

    if ip_data and ip_data.get("ip"):
        real_ip = ip_data["ip"]
        # Invalidate IP cache immediately after logging a new one
        global _used_ips_cache, _cache_expiry
        _used_ips_cache = None
        _cache_expiry = 0
        logger.info("Invalidated used IPs cache after logging.")

        if append_used_ip(real_ip, proxy_string):
            return jsonify({"status": "success", "message": f"IP {real_ip} logged as used."})
        else:
            return jsonify({"status": "error", "message": "Failed to log IP to Google Sheet."}), 500
    else:
        logger.error(f"Could not re-extract IP for proxy {proxy_string.split(':')[0]} to log it.")
        return jsonify({"status": "error", "message": "Could not verify IP for logging."}), 500


@app.errorhandler(404)
def not_found(e):
    return "Page not found", 404

@app.errorhandler(500)
def internal_error(e):
    logger.exception("Internal Server Error")
    return f"Internal server error. Check logs.", 500

if __name__ == "__main__":
    if not os.path.exists("templates"):
        os.makedirs("templates")
    if not os.path.exists("templates/index.html"):
        with open("templates/index.html", "w") as f:
            f.write("<h1>Placeholder - Create templates/index.html</h1>")
        print("✅ Created placeholder templates/index.html")

    print("\n---")
    print("🚀 Starting IP Extractor on http://localhost:5000")
    print(f"📈 Max entries: {storage.settings['MAX_PASTE']}")
    print(f"👷 Max workers (app default): {storage.settings['MAX_WORKERS']}")
    print("---")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)