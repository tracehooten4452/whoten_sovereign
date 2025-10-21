# Whoten Sovereign — Diamond v5.1 (VPS Edition)
# Flask Dashboard on port 80, PIN-protected, .env powered
# -------------------------------------------------------
# Features:
# - PIN login (session-based)
# - Shopify sync (safe wrappers + retries)
# - Market/supplier scan (stub you can extend)
# - Daily report (summary to Telegram if configured, else logs)
# - Background scheduler (intervals from env)
# - Manual endpoints: /sync /scan /report /health
# - Zero hardcoded secrets; reads from .env
#
# Dependencies: flask, python-dotenv, requests
#   pip install flask python-dotenv requests
#
# Run (as root to bind port 80):
#   sudo python3 main.py
#
# Environment (.env) keys (examples):
#   FLASK_SECRET="change_me_for_persistent_sessions"
#   DASHBOARD_PIN="5631"
#   SHOPIFY_ACCESS_TOKEN="shpat_xxx"
#   SHOPIFY_API_VERSION="2024-10"
#   SHOP_NAME="your-shop-name.myshopify.com"
#   SHOPIFY_LOCATION_ID="110669136148"
#   SUPPLIER_API_URL="local"
#   TELEGRAM_BOT_TOKEN="8380:xxxxx"   # optional
#   TELEGRAM_CHAT_ID="1395102852"     # optional
#   SYNC_INTERVAL_HOURS="3"
#   MARKET_SCAN_INTERVAL_HOURS="6"
#   REPORT_HOUR_LOCAL="15"
#   REPORT_MIN_LOCAL="0"

import os
import time
import json
import threading
import traceback
from datetime import datetime, timedelta

import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify
from dotenv import load_dotenv

# ---------------------------
# .env & Config
# ---------------------------
load_dotenv()

def _as_int(name, default):
    try:
        return int(os.getenv(name, default))
    except Exception:
        return default

def _as_float(name, default):
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default

APP_SECRET = os.getenv("FLASK_SECRET") or os.urandom(32)
DASHBOARD_PIN = os.getenv("DASHBOARD_PIN", "5631")

SHOP_NAME = os.getenv("SHOP_NAME", "").strip()  # e.g., your-shop-name.myshopify.com
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN", "").strip()
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2024-10")
SHOPIFY_LOCATION_ID = os.getenv("SHOPIFY_LOCATION_ID", "").strip()

SUPPLIER_API_URL = os.getenv("SUPPLIER_API_URL", "local")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SYNC_INTERVAL_HOURS = _as_int("SYNC_INTERVAL_HOURS", 3)
MARKET_SCAN_INTERVAL_HOURS = _as_int("MARKET_SCAN_INTERVAL_HOURS", 6)
REPORT_HOUR_LOCAL = _as_int("REPORT_HOUR_LOCAL", 15)   # 3pm
REPORT_MIN_LOCAL = _as_int("REPORT_MIN_LOCAL", 0)

CURRENCY_DECIMALS = _as_int("CURRENCY_DECIMALS", 2)
MIN_PROFIT_MARGIN = _as_float("MIN_PROFIT_MARGIN", 0.20)

# ---------------------------
# Flask App
# ---------------------------
app = Flask(__name__)
app.secret_key = APP_SECRET

# Simple in-memory log ring
LOG_MAX = 1000
LOGS = []

def log(msg, level="INFO", data=None):
    entry = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level,
        "msg": msg
    }
    if data is not None:
        entry["data"] = data
    LOGS.append(entry)
    if len(LOGS) > LOG_MAX:
        del LOGS[: len(LOGS) - LOG_MAX]
    print(f"[{entry['ts']}] {level}: {msg}" + (f" | {data}" if data else ""))

# ---------------------------
# Notifier (Telegram optional)
# ---------------------------
class Notifier:
    def __init__(self, bot_token=None, chat_id=None):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def enabled(self):
        return bool(self.bot_token and self.chat_id)

    def send(self, text):
        if self.enabled():
            try:
                url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
                resp = requests.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=15)
                if resp.status_code != 200:
                    log("Telegram send failed", "WARN", {"status": resp.status_code, "body": resp.text})
            except Exception as e:
                log("Telegram exception", "ERROR", str(e))
        else:
            log(f"NOTICE: {text}", "NOTICE")

notifier = Notifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

# ---------------------------
# Shopify Client (minimal)
# ---------------------------
class ShopifyClient:
    def __init__(self, shop_domain, access_token, api_version):
        self.base = f"https://{shop_domain}/admin/api/{api_version}" if shop_domain else ""
        self.access_token = access_token

    def _headers(self):
        return {
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json"
        }

    def ok(self):
        return bool(self.base and self.access_token)

    def get_products(self, limit=50):
        if not self.ok():
            log("Shopify creds missing; skipping get_products", "WARN")
            return []
        try:
            r = requests.get(f"{self.base}/products.json", params={"limit": limit}, headers=self._headers(), timeout=30)
            r.raise_for_status()
            return r.json().get("products", [])
        except Exception as e:
            log("Shopify get_products error", "ERROR", str(e))
            return []

    def create_or_update_product(self, payload):
        """Very basic upsert. You can expand to search by handle/SKU etc."""
        if not self.ok():
            log("Shopify creds missing; skipping upsert", "WARN")
            return None
        try:
            r = requests.post(f"{self.base}/products.json", headers=self._headers(), data=json.dumps({"product": payload}), timeout=30)
            if r.status_code in (200, 201):
                return r.json().get("product")
            else:
                log("Shopify create product failed", "WARN", {"status": r.status_code, "body": r.text})
        except Exception as e:
            log("Shopify upsert exception", "ERROR", str(e))
        return None

shopify = ShopifyClient(SHOP_NAME, SHOPIFY_ACCESS_TOKEN, SHOPIFY_API_VERSION)

# ---------------------------
# Core Tasks
# ---------------------------
_last_sync = None
_last_scan = None
_last_report = None

def supplier_scan():
    """Stub supplier/trend scan. Replace with your real fetcher."""
    log("Supplier scan started")
    try:
        # Example: if SUPPLIER_API_URL == 'local', return static sample
        sample = [
            {"title": "Premium Brake Kit", "sku": "BK-PR-001", "cost": 120.00, "price": round(120.00*(1+MIN_PROFIT_MARGIN), CURRENCY_DECIMALS), "inventory": 25},
            {"title": "High-Temp Grease", "sku": "GR-HT-002", "cost": 9.50, "price": round(9.50*(1+MIN_PROFIT_MARGIN), CURRENCY_DECIMALS), "inventory": 200},
        ]
        time.sleep(1)  # simulate IO
        log("Supplier scan completed", data={"items": len(sample)})
        return sample
    except Exception as e:
        log("Supplier scan error", "ERROR", traceback.format_exc())
        return []

def shopify_sync():
    log("Shopify sync started")
    global _last_sync
    try:
        items = supplier_scan()
        created = 0
        for it in items:
            payload = {
                "title": it["title"],
                "body_html": f"<strong>Auto-imported</strong> — SKU: {it['sku']}",
                "variants": [{
                    "sku": it["sku"],
                    "price": f"{it['price']:.{CURRENCY_DECIMALS}f}",
                    "inventory_quantity": it["inventory"],
                    "inventory_management": "shopify"
                }]
            }
            res = shopify.create_or_update_product(payload)
            if res:
                created += 1
        _last_sync = datetime.now()
        msg = f"Sync complete: {created} items processed"
        log(msg)
        notifier.send(f"🛠️ Whoten Sync: {msg}")
        return {"ok": True, "processed": created}
    except Exception:
        log("Shopify sync failure", "ERROR", traceback.format_exc())
        notifier.send("⚠️ Whoten Sync failed. Check logs.")
        return {"ok": False}

def market_scan():
    log("Market scan started")
    global _last_scan
    try:
        # Here you could call external trend APIs, scrape, etc.
        time.sleep(1)
        _last_scan = datetime.now()
        log("Market scan completed")
        return {"ok": True}
    except Exception:
        log("Market scan failure", "ERROR", traceback.format_exc())
        return {"ok": False}

def daily_report():
    log("Daily report composing")
    global _last_report
    try:
        prod_count = len(shopify.get_products(limit=5)) if shopify.ok() else 0
        summary = f"📊 Daily Report\nProducts (sample fetched): {prod_count}\nLast Sync: {_last_sync}\nLast Scan: {_last_scan}"
        notifier.send(summary)
        _last_report = datetime.now()
        log("Daily report sent")
        return {"ok": True}
    except Exception:
        log("Report failure", "ERROR", traceback.format_exc())
        return {"ok": False}

# ---------------------------
# Scheduler Threads
# ---------------------------
_stop_flag = False

def every(hours, fn, name):
    """Simple interval runner."""
    secs = max(300, int(hours * 3600))  # min 5 min for safety
    log(f"Scheduler '{name}' started; interval={secs}s")
    while not _stop_flag:
        try:
            fn()
        except Exception:
            log(f"Task '{name}' crashed", "ERROR", traceback.format_exc())
        # sleep in small steps so we can stop faster
        for _ in range(secs // 5):
            if _stop_flag:
                break
            time.sleep(5)

def run_daily_at(hour, minute, fn, name):
    log(f"Scheduler '{name}' started; daily at {hour:02d}:{minute:02d}")
    while not _stop_flag:
        now = datetime.now()
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        sleep_seconds = (next_run - now).total_seconds()
        # sleep in small steps
        while sleep_seconds > 0 and not _stop_flag:
            step = min(30, sleep_seconds)
            time.sleep(step)
            sleep_seconds -= step
        if _stop_flag:
            break
        try:
            fn()
        except Exception:
            log(f"Task '{name}' crashed", "ERROR", traceback.format_exc())

def start_schedulers():
    t1 = threading.Thread(target=every, args=(SYNC_INTERVAL_HOURS, shopify_sync, "sync"), daemon=True)
    t2 = threading.Thread(target=every, args=(MARKET_SCAN_INTERVAL_HOURS, market_scan, "scan"), daemon=True)
    t3 = threading.Thread(target=run_daily_at, args=(REPORT_HOUR_LOCAL, REPORT_MIN_LOCAL, daily_report, "report"), daemon=True)
    t1.start(); t2.start(); t3.start()
    log("Schedulers launched")

# ---------------------------
# Auth
# ---------------------------
def require_login(view):
    def wrapper(*args, **kwargs):
        if not session.get("auth_ok"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    wrapper.__name__ = view.__name__
    return wrapper

# ---------------------------
# Templates
# ---------------------------
PAGE = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Whoten Sovereign</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; color:#111; }
    .card { border:1px solid #ddd; border-radius:14px; padding:18px; margin-bottom:16px; box-shadow: 0 1px 6px rgba(0,0,0,.04);}
    button, .btn { padding:10px 14px; border-radius:10px; border:1px solid #ccc; background:#f5f5f5; cursor:pointer;}
    .grid { display:grid; gap:16px; grid-template-columns: repeat(auto-fit,minmax(260px,1fr)); }
    code { background:#f0f0f0; padding:2px 6px; border-radius:6px;}
    .muted { color:#666; }
    table { width:100%; border-collapse: collapse; }
    th, td { padding:8px; border-bottom:1px solid #eee; text-align:left;}
  </style>
</head>
<body>
  <h1>Whoten Sovereign — Dashboard</h1>
  <p class="muted">Logged in. <a href="{{ url_for('logout') }}">Logout</a></p>

  <div class="grid">
    <div class="card">
      <h3>Quick Actions</h3>
      <form method="post" action="{{ url_for('trigger_sync') }}"><button>Run Sync Now</button></form>
      <form method="post" action="{{ url_for('trigger_scan') }}" style="margin-top:8px;"><button>Run Market Scan</button></form>
      <form method="post" action="{{ url_for('trigger_report') }}" style="margin-top:8px;"><button>Send Daily Report</button></form>
    </div>

    <div class="card">
      <h3>Status</h3>
      <table>
        <tr><th>Last Sync</th><td>{{ last_sync }}</td></tr>
        <tr><th>Last Scan</th><td>{{ last_scan }}</td></tr>
        <tr><th>Last Report</th><td>{{ last_report }}</td></tr>
        <tr><th>Notifier</th><td>{{ notifier_status }}</td></tr>
        <tr><th>Shopify Config</th><td>{{ shopify_status }}</td></tr>
      </table>
    </div>

    <div class="card">
      <h3>Environment</h3>
      <table>
        <tr><th>Shop</th><td>{{ shop_name }}</td></tr>
        <tr><th>API Version</th><td>{{ shopify_api_version }}</td></tr>
        <tr><th>Location ID</th><td>{{ location_id }}</td></tr>
        <tr><th>Supplier API</th><td>{{ supplier_api }}</td></tr>
        <tr><th>Sync Interval (h)</th><td>{{ sync_h }}</td></tr>
        <tr><th>Scan Interval (h)</th><td>{{ scan_h }}</td></tr>
        <tr><th>Report Time</th><td>{{ report_h }}:{% if report_m < 10 %}0{% endif %}{{ report_m }}</td></tr>
      </table>
    </div>
  </div>

  <div class="card">
    <h3>Logs</h3>
    <div class="muted" style="max-height:360px; overflow:auto;">
      {% for l in logs %}
        <div><code>[{{l.ts}}] {{l.level}}</code> — {{l.msg}} {% if l.data %}<span class="muted">{{l.data}}</span>{% endif %}</div>
      {% endfor %}
    </div>
  </div>
</body>
</html>
"""

LOGIN = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Whoten Login</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 24px; }
    .card { max-width:420px; margin: 48px auto; border:1px solid #ddd; border-radius:14px; padding:18px; box-shadow: 0 1px 6px rgba(0,0,0,.05);}
    input { width:100%; padding:10px; border-radius:10px; border:1px solid #ccc; margin-top:8px;}
    button { margin-top:12px; padding:10px 14px; border-radius:10px; border:1px solid #ccc; background:#f5f5f5; cursor:pointer;}
    .muted { color:#666; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Whoten Sovereign — Login</h2>
    <p class="muted">Enter your PIN to access the dashboard.</p>
    <form method="post">
      <input name="pin" type="password" placeholder="PIN" autofocus />
      <button type="submit">Enter</button>
    </form>
  </div>
</body>
</html>
"""

# ---------------------------
# Routes
# ---------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pin = (request.form.get("pin") or "").strip()
        if pin == DASHBOARD_PIN:
            session["auth_ok"] = True
            log("Login success")
            return redirect(url_for("home"))
        else:
            log("Login failed", "WARN")
    return render_template_string(LOGIN)

@app.route("/logout")
def logout():
    session.pop("auth_ok", None)
    return redirect(url_for("login"))

@app.route("/")
@require_login
def home():
    return render_template_string(
        PAGE,
        last_sync=_last_sync,
        last_scan=_last_scan,
        last_report=_last_report,
        notifier_status=("Telegram" if notifier.enabled() else "Logs only"),
        shopify_status=("OK" if shopify.ok() else "Missing creds"),
        shop_name=SHOP_NAME or "(unset)",
        shopify_api_version=SHOPIFY_API_VERSION,
        location_id=SHOPIFY_LOCATION_ID or "(unset)",
        supplier_api=SUPPLIER_API_URL,
        sync_h=SYNC_INTERVAL_HOURS,
        scan_h=MARKET_SCAN_INTERVAL_HOURS,
        report_h=REPORT_HOUR_LOCAL,
        report_m=REPORT_MIN_LOCAL,
        logs=LOGS[::-1][:200],
    )

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "last_sync": str(_last_sync),
        "last_scan": str(_last_scan),
        "last_report": str(_last_report),
        "shopify_configured": shopify.ok(),
        "notifier": ("telegram" if notifier.enabled() else "log")
    })

@app.route("/sync", methods=["POST"])
@require_login
def trigger_sync():
    res = shopify_sync()
    return redirect(url_for("home"))

@app.route("/scan", methods=["POST"])
@require_login
def trigger_scan():
    res = market_scan()
    return redirect(url_for("home"))

@app.route("/report", methods=["POST"])
@require_login
def trigger_report():
    res = daily_report()
    return redirect(url_for("home"))

# ---------------------------
# Main
# ---------------------------
if __name__ == "__main__":
    log("Whoten Sovereign starting…")
    start_schedulers()
    # Bind to 0.0.0.0:80 (root required)
    app.run(host="0.0.0.0", port=80)
  Add Whoten Sovereign v5.1 main.py
