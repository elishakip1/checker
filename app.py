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
    "MAX_PASTE": 300,
    "MAX_WORKERS": 15,
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
SHEET_NAME = "Used IPs"
USED_IP_WORKSHEET_NAME = "UsedProxies" # Renamed from previous step
BAD_IP_WORKSHEET_NAME = "BAD"         # <<< NEW: Worksheet for bad IPs
IP_COLUMN_INDEX = 1 # Assuming IP is in the first column (A) for BOTH sheets

_used_sheet_cache = None
_bad_sheet_cache = None

# --- Helper to get specific worksheet ---
def get_worksheet(worksheet_name):
    global _used_sheet_cache, _bad_sheet_cache
    sheet_cache = _used_sheet_cache if worksheet_name == USED_IP_WORKSHEET_NAME else _bad_sheet_cache

    if sheet_cache:
        try:
            sheet_cache.acell('A1').value # Check connection
            # logger.debug(f"Using cached connection for sheet: {worksheet_name}")
            return sheet_cache
        except Exception as e:
            logger.warning(f"Cached sheet connection stale for {worksheet_name} ({e}), re-authorizing...")
            if worksheet_name == USED_IP_WORKSHEET_NAME: _used_sheet_cache = None
            else: _bad_sheet_cache = None

    if not JSON_CREDS_STR:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON env var not set.")
        raise ValueError("Missing Google credentials.")
    try:
        creds_dict = json.loads(JSON_CREDS_STR)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPE)
        client = gspread.authorize(creds)
        spreadsheet = client.open(SHEET_NAME)
        sheet = spreadsheet.worksheet(worksheet_name) # Open the specified worksheet

        # Update the correct cache
        if worksheet_name == USED_IP_WORKSHEET_NAME: _used_sheet_cache = sheet
        else: _bad_sheet_cache = sheet

        logger.info(f"Connected to Google Sheet: {SHEET_NAME}/{worksheet_name}")
        return sheet
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(f"Spreadsheet '{SHEET_NAME}' not found or permission denied.")
        raise
    except gspread.exceptions.WorksheetNotFound:
        logger.error(f"Worksheet '{worksheet_name}' not found in spreadsheet '{SHEET_NAME}'.")
        raise
    except Exception as e:
        logger.error(f"Error opening Google Sheet '{SHEET_NAME}/{worksheet_name}': {e}")
        raise

# --- Function to append to USED sheet ---
def append_used_ip(ip, proxy_string):
    global _used_sheet_cache
    try:
        sheet = get_worksheet(USED_IP_WORKSHEET_NAME)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        sheet.append_row([ip, proxy_string, timestamp]) # Assumes columns: IP, Proxy String, Timestamp
        logger.info(f"Appended used IP {ip} to sheet '{USED_IP_WORKSHEET_NAME}'.")
        return True
    except Exception as e:
        logger.error(f"Failed to append IP {ip} to sheet '{USED_IP_WORKSHEET_NAME}': {e}")
        _used_sheet_cache = None # Reset cache on failure
        return False

# --- NEW: Function to append to BAD sheet ---
def append_bad_ip(ip):
    global _bad_sheet_cache
    try:
        sheet = get_worksheet(BAD_IP_WORKSHEET_NAME)
        timestamp = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')
        # Check if IP already exists to avoid duplicates (optional but recommended)
        try:
            # This can be slow on large sheets, consider alternative if performance matters
            existing_ips = set(sheet.col_values(IP_COLUMN_INDEX)[1:])
            if ip in existing_ips:
                logger.info(f"IP {ip} already marked as bad in sheet '{BAD_IP_WORKSHEET_NAME}'.")
                return True # Treat as success if already there
        except Exception as e:
             logger.warning(f"Could not check for existing bad IP {ip}: {e}. Appending anyway.")

        sheet.append_row([ip, timestamp]) # Assumes columns: IP, Timestamp
        logger.info(f"Appended bad IP {ip} to sheet '{BAD_IP_WORKSHEET_NAME}'.")
        return True
    except Exception as e:
        logger.error(f"Failed to append IP {ip} to sheet '{BAD_IP_WORKSHEET_NAME}': {e}")
        _bad_sheet_cache = None # Reset cache on failure
        return False


_used_ips_cache_set = None
_used_cache_expiry = 0
_bad_ips_cache_set = None
_bad_cache_expiry = 0
CACHE_DURATION_SECONDS = 60 # Cache sets for 60 seconds

# --- Helper function to get IP sets with caching ---
def get_ips_set_from_sheet(worksheet_name, cache_var_name, expiry_var_name):
    global _used_ips_cache_set, _used_cache_expiry, _bad_ips_cache_set, _bad_cache_expiry, _used_sheet_cache, _bad_sheet_cache

    cache = globals()[cache_var_name]
    expiry = globals()[expiry_var_name]
    current_time = time.time()

    if cache is not None and current_time < expiry:
        # logger.debug(f"Using cached IP set for {worksheet_name}.")
        return cache

    logger.info(f"Fetching IPs from sheet '{worksheet_name}'...")
    try:
        sheet = get_worksheet(worksheet_name) # Get the specific worksheet
        ip_list = sheet.col_values(IP_COLUMN_INDEX)[1:] # Assumes header in row 1
        ips_set = set(ip for ip in ip_list if ip)

        # Update the correct global cache variables
        globals()[cache_var_name] = ips_set
        globals()[expiry_var_name] = current_time + CACHE_DURATION_SECONDS

        logger.info(f"Fetched and cached {len(ips_set)} IPs from '{worksheet_name}'.")
        return ips_set
    except Exception as e:
        logger.error(f"Failed to fetch IPs from sheet '{worksheet_name}': {e}")
        # Reset specific sheet cache on error
        if worksheet_name == USED_IP_WORKSHEET_NAME: _used_sheet_cache = None
        else: _bad_sheet_cache = None
        return set() # Return empty set on error

# --- Specific functions calling the helper ---
def get_used_ips_set():
    return get_ips_set_from_sheet(USED_IP_WORKSHEET_NAME, '_used_ips_cache_set', '_used_cache_expiry')

def get_bad_ips_set():
    return get_ips_set_from_sheet(BAD_IP_WORKSHEET_NAME, '_bad_ips_cache_set', '_bad_cache_expiry')

# --- End Google Sheets Configuration ---

def validate_proxy_format(proxy_line):
    # (Existing function)
    try: parts = proxy_line.strip().split(":"); return len(parts) == 4 and all(parts)
    except: return False

def get_ip_from_proxy(proxy_line):
    # (Existing function - unchanged)
    if not validate_proxy_format(proxy_line): return None
    try:
        host, port, user, pw = proxy_line.strip().split(":")
        proxies = {"http": f"http://{user}:{pw}@{host}:{port}", "https": f"http://{user}:{pw}@{host}:{port}"}
        session = requests.Session()
        retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retries)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        response = session.get("https://api.ipify.org", proxies=proxies, timeout=REQUEST_TIMEOUT, headers={"User-Agent": random.choice(USER_AGENTS)}, verify=False)
        if response.status_code == 200:
            ip = response.text.strip()
            if ip and '.' in ip and 7 <= len(ip) <= 15: return {"proxy": proxy_line, "ip": ip}
        return None
    except Exception as e: logger.warning(f"Error getting IP for {proxy_line.split(':')[0]}: {type(e).__name__}"); return None


def single_check_proxy(proxy_line):
    # (Existing function - unchanged)
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    return get_ip_from_proxy(proxy_line)


@app.route("/", methods=["GET", "POST"])
def index():
    # (Updated logic for checking used AND bad IPs)
    settings = storage.settings
    MAX_PASTE = settings["MAX_PASTE"]
    MAX_WORKERS = int(os.environ.get('GUNICORN_WORKERS', settings["MAX_WORKERS"]))

    results_final = []
    message = ""

    if request.method == "POST":
        proxies = []
        all_lines = []
        input_count = 0
        file_error = False # Flag for file read issues

        if 'proxyfile' in request.files and request.files['proxyfile'].filename:
            try:
                file = request.files['proxyfile']
                content = file.read().decode("utf-8", errors='ignore')
                all_lines = content.strip().splitlines()
                input_count = len(all_lines)
                logger.info(f"Read {input_count} lines from file.")
            except Exception as e:
                logger.error(f"Error reading uploaded file: {e}")
                message = "Error reading the uploaded file. Ensure it's valid text."
                file_error = True # Set flag

        elif 'proxytext' in request.form and request.form['proxytext'].strip():
            proxytext = request.form.get("proxytext", "")
            all_lines = proxytext.strip().splitlines()
            input_count = len(all_lines)
            logger.info(f"Processing {input_count} lines from text area.")

        else:
             if not file_error: # Only show this if file read didn't fail
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

                # --- Check Used and Bad IPs ---
                if intermediate_results:
                    used_ips_set = set()
                    bad_ips_set = set()
                    try:
                        # Fetch both sets concurrently (minor optimization)
                        with ThreadPoolExecutor(max_workers=2) as sheet_executor:
                           future_used = sheet_executor.submit(get_used_ips_set)
                           future_bad = sheet_executor.submit(get_bad_ips_set)
                           used_ips_set = future_used.result()
                           bad_ips_set = future_bad.result()

                        for item in intermediate_results:
                            item['used'] = item['ip'] in used_ips_set # Add 'used' key
                            item['bad'] = item['ip'] in bad_ips_set   # <<< Add 'bad' key
                            results_final.append(item)
                        logger.info(f"Checked {len(results_final)} IPs against used ({len(used_ips_set)}) and bad ({len(bad_ips_set)}) lists.")

                    except Exception as e:
                        logger.error(f"Error checking used/bad IPs from Sheets: {e}. Proceeding without usage status.")
                        results_final = intermediate_results # Fallback
                # --- End Check ---


                if results_final:
                    message = f"✅ Extracted {len(results_final)} IPs from {processed_count} valid proxies ({processing_duration:.2f}s). See table."
                    if invalid_format_count > 0: message += f" ({invalid_format_count} lines skipped)."
                    logger.info(f"🎉 Final result: {len(results_final)} IPs extracted!")
                else:
                    message = f"⚠️ Processed {processed_count} valid proxies ({processing_duration:.2f}s), but couldn't extract IPs."
                    if invalid_format_count > 0: message += f" ({invalid_format_count} lines skipped)."
                    logger.warning(f"😞 No IPs extracted from {processed_count} valid proxies")

            except Exception as e:
                logger.exception("Error during ThreadPoolExecutor execution")
                message = "An error occurred during processing. Check logs."

        elif original_input_count > 0 and not file_error:
             message = f"❌ Submitted {original_input_count} lines, but none had valid format: host:port:user:pass"

    return render_template("index.html", results=results_final, message=message, max_paste=MAX_PASTE, settings=settings)


@app.route("/track-used", methods=["POST"])
def track_used():
    # (Logic remains largely the same, just invalidates used cache)
    data = request.get_json()
    if not data or "proxy" not in data: return jsonify({"status": "error", "message": "Invalid request body"}), 400
    proxy_string = data["proxy"]
    if not validate_proxy_format(proxy_string): return jsonify({"status": "error", "message": "Invalid proxy format"}), 400
    logger.info(f"Tracking used proxy: {proxy_string.split(':')[0]}")
    ip_data = get_ip_from_proxy(proxy_string)
    if ip_data and ip_data.get("ip"):
        real_ip = ip_data["ip"]
        global _used_ips_cache_set, _used_cache_expiry # Invalidate cache
        _used_ips_cache_set = None
        _used_cache_expiry = 0
        logger.info("Invalidated used IPs cache after logging.")
        if append_used_ip(real_ip, proxy_string): return jsonify({"status": "success", "message": f"IP {real_ip} logged."})
        else: return jsonify({"status": "error", "message": "Failed to log to GSheet."}), 500
    else: return jsonify({"status": "error", "message": "Could not verify IP."}), 500


# --- NEW Endpoint: /mark-bad ---
@app.route("/mark-bad", methods=["POST"])
def mark_bad():
    """Receives an IP address and logs it to the BAD Google Sheet."""
    data = request.get_json()
    if not data or "ip" not in data:
        logger.warning("Invalid request to /mark-bad: Missing IP data.")
        return jsonify({"status": "error", "message": "Invalid request body, missing 'ip'"}), 400

    ip_to_mark = data["ip"]
    # Basic IP format validation (optional but good practice)
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip_to_mark):
         logger.warning(f"Invalid IP format received at /mark-bad: {ip_to_mark}")
         return jsonify({"status": "error", "message": "Invalid IP format"}), 400

    logger.info(f"Received request to mark IP as bad: {ip_to_mark}")

    # Invalidate bad IP cache immediately
    global _bad_ips_cache_set, _bad_cache_expiry
    _bad_ips_cache_set = None
    _bad_cache_expiry = 0
    logger.info("Invalidated bad IPs cache after logging.")

    if append_bad_ip(ip_to_mark):
        return jsonify({"status": "success", "message": f"IP {ip_to_mark} logged as bad."})
    else:
        # Error logged within append_bad_ip
        return jsonify({"status": "error", "message": "Failed to log bad IP to Google Sheet."}), 500
# --- End /mark-bad Endpoint ---


@app.errorhandler(404)
def not_found(e): return "Page not found", 404
@app.errorhandler(500)
def internal_error(e): logger.exception("Internal Server Error"); return f"Internal server error. Check logs.", 500

if __name__ == "__main__":
    if not os.path.exists("templates"): os.makedirs("templates")
    if not os.path.exists("templates/index.html"):
        with open("templates/index.html", "w") as f: f.write("<h1>Placeholder - Create templates/index.html</h1>")
        print("✅ Created placeholder templates/index.html")
    print("\n---"); print(f"🚀 Starting IP Extractor on http://localhost:5000"); print(f"📈 Max entries: {storage.settings['MAX_PASTE']}"); print(f"👷 Max workers (app default): {storage.settings['MAX_WORKERS']}"); print("---")
    port = int(os.environ.get("PORT", 5000)); app.run(host="0.0.0.0", port=port, debug=False)