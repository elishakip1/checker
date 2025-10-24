from flask import Flask, request, render_template, jsonify, redirect, url_for
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
    "MAX_WORKERS": 4, # Keep this low for Render's free plan
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
MIN_DELAY = 0.3
MAX_DELAY = 1.0

def validate_proxy_format(proxy_line):
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
    if not validate_proxy_format(proxy_line):
        logger.warning(f"❌ Invalid format for IP check: {proxy_line.split(':')[0]}")
        return None
        
    try:
        host, port, user, pw = proxy_line.strip().split(":")
        proxies = {
            "http": f"http://{user}:{pw}@{host}:{port}",
            "https": f"http://{user}:{pw}@{host}:{port}",
        }
        
        session = requests.Session()
        retries = Retry(
            total=2, 
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504]
        )
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
                logger.info(f"✅ Got IP: {ip} from proxy {host}")
                # Return the original proxy string AND the IP
                return {"proxy": proxy_line, "ip": ip} 
            else:
                logger.warning(f"❌ Got invalid IP '{ip}' from ipify.org")
                return None
        else:
            logger.warning(f"❌ ipify.org returned {response.status_code}")
            return None
    except Exception as e:
        logger.error(f"❌ Failed to get IP from proxy {proxy_line.split(':')[0]}: {e}")
        return None

def single_check_proxy(proxy_line):
    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    if not validate_proxy_format(proxy_line):
        return None
    ip_data = get_ip_from_proxy(proxy_line) 
    return ip_data # Returns {"proxy": "...", "ip": "..."} or None

@app.route("/", methods=["GET", "POST"])
def index():
    settings = storage.settings
    MAX_PASTE = settings["MAX_PASTE"]
    MAX_WORKERS = settings["MAX_WORKERS"]

    results = [] # List of {"proxy": "...", "ip": "..."}
    message = ""

    if request.method == "POST":
        proxies = []
        all_lines = []
        input_count = 0

        if 'proxyfile' in request.files and request.files['proxyfile'].filename:
            file = request.files['proxyfile']
            all_lines = file.read().decode("utf-8").strip().splitlines()
            input_count = len(all_lines)
            if input_count > MAX_PASTE:
                all_lines = all_lines[:MAX_PASTE]
            proxies = all_lines
        elif 'proxytext' in request.form:
            proxytext = request.form.get("proxytext", "")
            all_lines = proxytext.strip().splitlines()
            input_count = len(all_lines)
            if input_count > MAX_PASTE:
                all_lines = all_lines[:MAX_PASTE]
            proxies = all_lines

        valid_proxies = []
        for proxy in proxies:
            proxy = proxy.strip()
            if proxy and validate_proxy_format(proxy):
                valid_proxies.append(proxy)

        processed_count = len(valid_proxies)

        if valid_proxies:
            logger.info(f"🔄 Extracting IPs from {len(valid_proxies)} valid proxies...")

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [executor.submit(single_check_proxy, proxy) for proxy in valid_proxies]
                for future in as_completed(futures):
                    result = future.result() 
                    if result:
                        results.append(result)

            if results:
                message = f"✅ Extracted {len(results)} IPs. See the table below."
                logger.info(f"🎉 Final result: {len(results)} IPs extracted!")
            else:
                message = f"⚠️ Processed {processed_count} proxies, but could not extract any IPs."
                logger.warning(f"😞 No IPs extracted from {processed_count} valid proxies")
        else:
            message = f"❌ No valid proxies found. Required format: host:port:username:password"

    return render_template("index.html", results=results, message=message, max_paste=MAX_PASTE, settings=settings)

@app.errorhandler(404)
def not_found(e):
    return "Page not found", 404

@app.errorhandler(500)
def internal_error(e):
    return f"Internal server error: {str(e)}", 500

if __name__ == "__main__":
    if not os.path.exists("templates"):
        os.makedirs("templates")
        
    if not os.path.exists("templates/index.html"):
        # Create the initial index.html if it doesn't exist
        # Note: You'll want to replace this with the final HTML content below
        with open("templates/index.html", "w") as f:
            f.write("<h1>Placeholder - Replace with final index.html content</h1>") 
        print("✅ Created placeholder templates/index.html")
    
    print("\n---")
    print("🚀 Starting IP Extractor on http://localhost:5000")
    print(f"📈 Max entries: {storage.settings['MAX_PASTE']}")
    print("---")
    port = int(os.environ.get("PORT", 5000))
    # Set debug=False for Render, debug=True for local testing if needed
    app.run(host="0.0.0.0", port=port, debug=False)
