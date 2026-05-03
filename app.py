# app.py
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, send_from_directory, send_file, abort, current_app, make_response
)

#from datetime import datetime, date
import datetime as dt

import os, time, requests, re, json, uuid, mimetypes, unicodedata
from bs4 import BeautifulSoup
from contextlib import closing
from pathlib import Path
from calendar import monthrange
from uuid import uuid4
import socket
from flask_cors import CORS

from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
    UAE_TZ = ZoneInfo("Asia/Dubai")
except Exception:
    UAE_TZ = None


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")
CORS(app)

BASE_DIR = os.environ.get("RENDER_DISK_PATH", ".")

DAYS_AR = {
    "Saturday": "السبت",
    "Sunday": "الأحد",
    "Monday": "الاثنين",
    "Tuesday": "الثلاثاء",
    "Wednesday": "الأربعاء",
    "Thursday": "الخميس",
    "Friday": "الجمعة",
}

DEFAULT_VAT = 0.05            # 5%
DEFAULT_KARAT_FACTOR = 1 # 24 قيراط كمثال
DEFAULT_MARGIN_PER_G = 2      # هامش شراء المحل عند شراءه منك (درهم/جم)
AED_TO_USD = 1 / 3.6725

# Karat factors relative to 24K
KARAT_FACTORS = {
    24: 24/24,
    22: 22/24,
    21: 21/24,
    18: 18/24,
}

def today_name_ar():
    now = dt.datetime.now(UAE_TZ) if UAE_TZ else dt.datetime.now()
    return DAYS_AR.get(now.strftime("%A"), now.strftime("%A"))

def calc_buy_mode(w, p_g, offered_total, karat_factor, vat):
    gold_value = w * p_g * karat_factor
    
    subtotal = gold_value
    vat_value = subtotal * vat
    fair_price_buy = subtotal + vat_value

    diff = offered_total - fair_price_buy
    seller_profit = max(diff, 0.0)
    profit_pct = seller_profit / fair_price_buy if fair_price_buy else 0.0

    making_fee_per_g_calculated = diff / w if w else 0.0

    advice = None
    if profit_pct > 0.10:
        advice = {
            "text": "ربح البائع مرتفع (أكثر من 10%) - يمكنك التفاوض على سعر أفضل",
            "type": "danger"   # أحمر
        }
    elif diff <= -0.05 * fair_price_buy:
        advice = {
            "text": "السعر المعروض أقل من العادل، فرصة جيدة للشراء",
            "type": "success"  # أخضر
        }

    return {
        "mode": "buy",
        "weight": w,
        "price_per_g": p_g,
        "karat_factor": karat_factor,
        "vat_rate": vat,
        "offered_total": offered_total,

        "gold_value": gold_value,
        "vat_value": vat_value,
        "fair_price": fair_price_buy,
        "diff": diff,
        "seller_profit": seller_profit,
        "profit_pct": profit_pct,
        "making_fee_per_g": making_fee_per_g_calculated,
        "advice": advice
    }

def calc_sell_mode(w, p_g, offered_total, karat_factor, margin_per_g):
    gold_value = w * p_g * karat_factor
    fair_price_sell_to_shop = gold_value - (w * margin_per_g)
    diff = offered_total - fair_price_sell_to_shop
    advice = None
    if offered_total < fair_price_sell_to_shop:
        advice = {
            "text": "لا يُنصح بالبيع (السعر المعروض منخفض)",
            "type": "danger"
        }
    elif diff > 0:
        advice = {
            "text": "عرض جيد لبيع الذهب (أفضل من السعر العادل للتاجر)",
            "type": "success"
        }
    return {
        "mode": "sell",
        "weight": w,
        "price_per_g": p_g,
        "karat_factor": karat_factor,
        "margin_per_g": margin_per_g,
        "offered_total": offered_total,

        "gold_value": gold_value,
        "fair_price": fair_price_sell_to_shop,
        "diff": diff,
        "advice": advice
    }

def _compute_change(latest_price: float, previous_price: float):
    try:
        latest = float(latest_price)
        previous = float(previous_price)
    except (TypeError, ValueError):
        return 0.0, 0.0
    if previous == 0:
        return round(latest - previous, 2), 0.0
    diff = latest - previous
    pct = diff / previous * 100.0
    return round(diff, 2), round(pct, 2)


UPLOAD_DIR = Path("data/uploads")
ALLOWED = {"png","jpg","jpeg","webp","pdf"}

def save_upload(file_storage):
    if not file_storage: return None
    ext = (file_storage.filename.rsplit(".",1)[-1] or "").lower()
    if ext not in ALLOWED: return None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    fname = secure_filename(f"{int(time.time()*1000)}_{file_storage.filename}")
    path = UPLOAD_DIR / fname
    file_storage.save(path)
    return str(path)   # store in DB

@app.route("/", methods=["GET", "POST"])
def index():
    # keep selected tab (supports: click on tab, submit, or ?mode= in URL)
    mode = (
        request.form.get("mode")
        or request.form.get("current_mode")
        or request.args.get("mode", "buy")
    )

    # gather raw values (strings) so we can re-fill the form
    values = {
        "weight": request.form.get("weight", ""),
        "price_per_g": request.form.get("price_per_g", ""),
        "karat_factor": request.form.get("karat_factor", "1.0") if request.method == "POST" else "1.0",
        "offered_total": request.form.get("offered_total", ""),
        # buy-only fields (still safe to pass on sell)
        "vat": request.form.get("vat", "0.05"),
        # sell-only field
        "margin_per_g": request.form.get("margin_per_g", "2.0"),
    }
    
    # define karat safely for BOTH GET and POST
    k_str = values["karat_factor"]
    try:
        k = float(k_str)
    except ValueError:
        k = 1.0
        k_str = "1.0"
        values["karat_factor"] = k_str

    result = None
    if request.method == "POST":
        # parse numbers safely
        def to_f(x, default=0.0):
            try:
                return float(x) if x not in (None, "") else default
            except ValueError:
                return default

        w  = to_f(values["weight"])
        p  = to_f(values["price_per_g"])
        t  = to_f(values["offered_total"])

        if mode == "buy":
            v  = to_f(values["vat"], 0.05)
            result = calc_buy_mode(w, p, t, k, v)
        else:
            mg = to_f(values["margin_per_g"], 2.0)
            result = calc_sell_mode(w, p, t, k, mg)

    return render_template(
        "Smart_Calculator.html", # "index.html"
        mode=mode,
        defaults=dict(vat=0.05, karat_factor=k_str, margin_per_g=2.0),
        values=values,       # <-- important: send values back
        result=result
    )

# Optional: keep a live igold peek (does not affect manual trend)
IGOLD_URL = "https://igold.ae/gold-rate/"
IGOLD_TTL = 60 # seconds
_igold_cache = {"at": 0.0, "payload": None}
IGOLD_SNAPSHOT = Path(BASE_DIR) / "data" / "igold_snapshot.json"

CHARTS_URL = "https://charts.igold.ae/api/data?metal=xau&currency=aed&period=live&weight=ounce&purity=1000&_=1760175353677"

def fetch_igold_chart_latest():
    """
    Returns {"per_oz_aed": float, "per_g_aed": float, "raw": dict} from charts.igold.ae.
    Falls back to {} if request fails.
    """
    try:
        # These params mirror what the chart uses (from your Network tab).
        # If the URL you saw includes extra params, add them here.
        params = {
            "metal": "xau",
            "currency": "aed",
            "weight": "ounce",   # we want AED/oz directly
            "timespan": "live",  # matches the "Live" tab
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/127.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://igold.ae/gold-rate/",
            "Origin": "https://igold.ae",
        }
        r = requests.get(CHARTS_URL, params=params, timeout=10, headers=headers)
        r.raise_for_status()
        j = r.json()
        per_oz = float(j.get("last_price"))
        # derive AED/gram from AED/oz to avoid a second call
        per_g = per_oz / 31.1034768
        return {"per_oz_aed": per_oz, "per_g_aed": per_g, "raw": j}
    except Exception as e:
        print(f"Error fetching chart: {e}")

        return {}


def get_igold_rates():
    now = time.time()
    if _igold_cache["payload"] and now - _igold_cache["at"] < IGOLD_TTL:
        return _igold_cache["payload"]

    try:
        chart_latest = fetch_igold_chart_latest() 

        r = requests.get(IGOLD_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        #=====================================

        # --- Pure Metal table
        pure_match = re.search(
            r"Pure Metal Rate in AED.*?Gold\s+([\d.]+)\s+Silver\s+([\d.]+)\s+Price updated:\s+([0-9/:\s]+)",
            text
        )

        pure = {
            "gold_per_g_aed": float(pure_match.group(1)) if pure_match else None,
            "silver_per_g_aed": float(pure_match.group(2)) if pure_match else None,
            "updated_at": pure_match.group(3).strip() if pure_match else None,
        }

        # --- Retail per gram table
        ret_block = re.search(
            r"Current Live Retail Gold Rate in Dubai UAE\s+24K\s+22K\s+21K\s+18K\s+([\d.]+)\s*AED\s+([\d.]+)\s*AED\s+([\d.]+)\s*AED\s+([\d.]+)\s*AED",
            text
        )
        retail = {}
        if ret_block:
            retail = {
                "24K_per_g_aed": float(ret_block.group(1)),
                "22K_per_g_aed": float(ret_block.group(2)),
                "21K_per_g_aed": float(ret_block.group(3)),
                "18K_per_g_aed": float(ret_block.group(4)),
            }
        ts2 = re.search(r"Prices updated:\s+([0-9/:\s]+)", text)
        retail["updated_at"] = ts2.group(1).strip() if ts2 else pure.get("updated_at")

        # --- Build payload ONCE (keep chart_latest!)
        data = {
            "source": IGOLD_URL,
            "pure": pure,
            "retail": retail,
            "chart_latest": {
                "per_oz_aed": chart_latest.get("per_oz_aed"),
                "per_g_aed": chart_latest.get("per_g_aed"),
            },
        }

        # cache + snapshot
        _igold_cache.update(at=now, payload=data)
        IGOLD_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        IGOLD_SNAPSHOT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    
    except Exception:
        if IGOLD_SNAPSHOT.exists():
            return json.loads(IGOLD_SNAPSHOT.read_text(encoding="utf-8"))
        return {"source": IGOLD_URL, "pure": {}, "retail": {}, "chart_latest": {}}
        #=====================================

#========================================================================================================
#========================================================================================================
def _label_to_year_month(label: str):
    # labels like "5/2025"
    try:
        m, y = label.split("/")
        return int(y), int(m)
    except Exception:
        return None, None

def filter_month_end_points_only(points):
    """
    `points` like [{"label":"5/2025","value": 1771.8}, ...]
    Returns same shape, but hides the current month unless today is the month's last day.
    """
    if not points:
        return []

    out = list(points)  # already month-end points in your JSON
    now = dt.datetime.now()
    _, last_day = monthrange(now.year, now.month)
    is_month_end_today = now.day == last_day

    # If NOT month end today, drop the current month label (e.g., "10/2025")
    if not is_month_end_today:
        this_label = f"{now.month}/{now.year}"
        out = [p for p in out if p.get("label") != this_label]

    # sort safely by year/month
    out.sort(key=lambda p: (_label_to_year_month(p["label"]) or (0, 0)))
    return out

#========================================================================================================
#========================================================================================================

def fetch_aed_per_gram_24k() -> float:
    """
    Returns AED/gram for 24K using your igold scraping:
      1) retail 24K per gram (best match to store displays)
      2) pure metal per gram
      3) convert chart AED/oz -> AED/g
    Falls back to 250.0 if nothing is available.
    """
    try:
        rates = get_igold_rates() or {}
        retail_24 = (rates.get("retail") or {}).get("24K_per_g_aed")
        if retail_24:
            return float(retail_24)

        pure_g = (rates.get("pure") or {}).get("gold_per_g_aed")
        if pure_g:
            return float(pure_g)

        chart_oz = (rates.get("chart_latest") or {}).get("per_oz_aed")
        if chart_oz:
            return float(chart_oz) / 31.1034768  # oz -> g

    except Exception:
        pass

    # Safe fallback so the page still renders
    return 250.0

# ---- settings storage ----
SETTINGS_FILE = Path(BASE_DIR) / "data" / "settings.json"
DEFAULT_SETTINGS = {
    "ui": {"lang": "ar", "theme": "gold", "default_mode": "buy", "unit": "gram", "dark": False},
    "pricing": {"source": "karat", "auto_refresh_sec": 60, "vat": 0.05, "bar_fee": 40.0,
                "karat_factors": {"24": 1.0, "22": 22/24, "21": 21/24, "18": 18/24}},
    "zakat": {"nisab_ref": "24k_85g", "rate": 0.025, "hawl_start": ""},
    "notifications": {"price_change_pct": 2.0, "zakat_reminder": False},
    "about": {"version": "1.0.0", "developer": "Your Name"}
}

def load_settings():
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
    return DEFAULT_SETTINGS

def save_settings(payload: dict):
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/gold/trend-monthend")
def api_gold_trend_monthend():
    data = load_trend()
    points = filter_month_end_points_only(data.get("points", []))

    labels = [p["label"] for p in points]
    # values in file are AED per OUNCE
    prices_usd_per_oz = [round(float(p["value"]) * AED_TO_USD, 2) for p in points]

    return jsonify({
        "labels": labels,
        "prices": prices_usd_per_oz,
        "currency": "$",
        "last_updated": data.get("last_updated_at") or  dt.datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
    })

@app.get("/api/gold/live")
def api_gold_live():
    rates = get_igold_rates() or {}

    # Prefer the chart "Latest Price" (AED/oz). Fallbacks keep your previous behavior.
    chart_oz = (rates.get("chart_latest") or {}).get("per_oz_aed")
    chart_g  = (rates.get("chart_latest") or {}).get("per_g_aed")
    pure_g   = (rates.get("pure") or {}).get("gold_per_g_aed")
    retail_g = (rates.get("retail") or {}).get("24K_per_g_aed")

    if chart_oz:                                    # best: exact AED/oz from igold chart
        live_oz_usd = chart_oz * AED_TO_USD
    else:
        per_g_aed = chart_g or pure_g or retail_g   # next best: per-gram
        if not per_g_aed:
            return jsonify({
                "price": 0.0, "change_abs": 0.0, "change_pct": 0.0,
                "ts": dt.datetime.now().isoformat(timespec="seconds")
            })
        # live price: AED/g → USD/oz
        live_oz_usd = per_g_aed * 31.1034768 * AED_TO_USD

    _update_daily_history_usd(round(live_oz_usd, 2))
    
    # reference from trend: AED/oz → USD/oz (NO 31.103 here)
    data = load_trend()
    points = filter_month_end_points_only(data.get("points", []))
    ref_oz_usd = float(points[-1]["value"]) * AED_TO_USD if points else None
    if ref_oz_usd is None:
        ref_oz_usd = live_oz_usd 

    diff = live_oz_usd - ref_oz_usd
    pct = (diff / ref_oz_usd * 100.0) if ref_oz_usd else 0.0

    price_5d = _price_n_days_ago_usd(5)
    if price_5d:
        diff_5d = live_oz_usd - price_5d
        pct_5d  = (diff_5d / price_5d * 100.0)
    else:
        diff_5d = pct_5d = None

    return jsonify({
        "price": round(live_oz_usd, 2),
        "change_abs": round(diff, 2),
        "change_pct": round(pct, 2),
        "change_5d_abs": (round(diff_5d, 2) if diff_5d is not None else None),
        "change_5d_pct": (round(pct_5d, 2) if pct_5d is not None else None),
        "ts": dt.datetime.now().isoformat(timespec="seconds")
    })
#========================================================================================================
#========================================================================================================





@app.get("/api/gold")
def api_gold_manual():
    data = load_trend()
    points = data.get("points", [])
    months = [p["label"] for p in points]
    prices_aed = [round(float(p["value"]), 2) for p in points]

    # convert AED → USD
    prices_usd = [round(p * AED_TO_USD, 2) for p in prices_aed]

    latest_price = prices_usd[-1] if prices_usd else 0.0
    prev_price = prices_usd[-2] if len(prices_usd) > 1 else latest_price
    change_val, change_pct = _compute_change(latest_price, prev_price)

    payload = {
        "latest_price": latest_price,
        "change_val": change_val,
        "change_pct": change_pct,
        "last_updated": data.get("last_updated_at") or dt.datetime.now().strftime("%H:%M:%S %d/%m/%Y"),
        "months": months,
        "prices": prices_usd,
        "currency": "$"
    }
    return jsonify(payload)

@app.get("/api/igold")
def api_igold():
    return jsonify(get_igold_rates())

# ---------- Local storage for manual updates ----------
DATA_DIR = Path(BASE_DIR) / "data"

TREND_FILE = DATA_DIR / "trend_data.json"

def load_trend():
    if TREND_FILE.exists():
        return json.loads(TREND_FILE.read_text(encoding="utf-8"))
    return {
        "series_name": "Gold Price per Ounce (AED)",
        "points": [
            {"label": "5/2025", "value": 12074.44},
            {"label": "6/2025", "value": 12089.81},
            {"label": "7/2025", "value": 12055.69},
            {"label": "8/2025", "value": 12662.26},
            {"label": "9/2025", "value": 13809.32},
            {"label": "10/2025","value": 14349.7081},
        ],
        "last_updated_at":  dt.datetime.now().strftime("%H:%M:%S %d/%m/%Y"),  # <-- add this
    }

def save_trend(payload):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload["last_updated_at"] =  dt.datetime.now().strftime("%H:%M:%S %d/%m/%Y")
    TREND_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- Daily history for N-day comparisons ----------
HISTORY_FILE = DATA_DIR / "gold_history.json"
KEEP_DAYS_DEFAULT = 90    
KEEP_DAYS_MIN = 5  

def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list) and data and isinstance(data[0], dict):
                data = data[0]   # flatten the list
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}

def _save_history(hist: dict, keep_days: int = KEEP_DAYS_DEFAULT):
    keep_days = max(keep_days, KEEP_DAYS_MIN)
    cutoff = (_uae_today_date() - dt.timedelta(days=keep_days-1)).isoformat()
    trimmed = {d: v for d, v in hist.items() if d >= cutoff}
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


def _uae_today_date():
    try:
        import pytz, datetime as _dt
        tz = pytz.timezone("Asia/Dubai")
        return _dt.datetime.now(tz).date()
    except Exception:
        return dt.date.today()
    
def _update_daily_history_usd(price_usd: float):
    hist = _load_history()
    key = _uae_today_date().isoformat()   # "YYYY-MM-DD"
    hist[key] = round(float(price_usd), 2)
    _save_history(hist)    


def _price_n_days_ago_usd(n: int) -> float | None:
    """Return price n days ago, or nearest earlier within KEEP_DAYS_MIN."""
    hist = _load_history()
    if not hist:
        return None

    target = _uae_today_date() - dt.timedelta(days=n)
    for back in range(0, KEEP_DAYS_MIN + 1):  # 0..5
        key = (target - dt.timedelta(days=back)).isoformat()
        if key in hist:
            return float(hist[key])
    return None

# ---- iGold karat prices helper ----
_karat_cache = {"at": 0, "ttl": 180, "data": None}

def _num(x):
    return float(str(x).replace(",", "").strip() or 0)

def get_igold_karat_prices():
    """Return {'24': .., '22': .., '21': .., '18': ..} in AED/gram."""
    now = time.time()
    if _karat_cache["data"] and now - _karat_cache["at"] < _karat_cache["ttl"]:
        return _karat_cache["data"]

    url = "https://igold.ae/gold-rate/"
    data = {"24": 0.0, "22": 0.0, "21": 0.0, "18": 0.0}

    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        pat = lambda k: [
            rf"(?:{k}\s*K(?:arat)?|عيار\s*{k}).{{0,60}}?(\d{{2,5}}(?:[.,]\d{{1,2}})?)\s*(?:AED|درهم)",
            rf"(?:AED|درهم)\s*(\d{{2,5}}(?:[.,]\d{{1,2}})?)\s*(?:for\s*)?(?:{k}\s*K|{k}\s*Carat|عيار\s*{k})",
        ]
        for k in ("24", "22", "21", "18"):
            for p in pat(k):
                m = re.search(p, text, re.I)
                if m:
                    data[k] = _num(m.group(1))
                    break

        price24 = data["24"]
        unique = {round(v, 2) for v in data.values()}
        if (price24 > 0 and len(unique) == 1) or 0.0 in data.values():
            # derive others from 24K purity ratios
            data["24"] = round(price24, 2)
            data["22"] = round(price24 * (22/24), 2)
            data["21"] = round(price24 * (21/24), 2)
            data["18"] = round(price24 * (18/24), 2)

    except Exception:
        # keep zeros; UI will allow manual entry
        pass

    _karat_cache.update({"at": now, "data": data})
    return data

# ---------- Route ----------
@app.route("/trend", methods=["GET", "POST"])
def trend():
    data = load_trend()

    # Manual update via form
    if request.method == "POST":
        label = (request.form.get("label") or "").strip()
        value_raw = (request.form.get("value") or "").strip()
        if label and value_raw:
            try:
                value = float(value_raw)
            except ValueError:
                value = None
            if value is not None:
                # Update existing or add new
                updated = False
                for p in data["points"]:
                    if p["label"] == label:
                        p["value"] = value
                        updated = True
                        break
                if not updated:
                    data["points"].append({"label": label, "value": value})
                data["points"] = sorted(data["points"], key=lambda x: x["label"])
                save_trend(data)
                return redirect(url_for("trend"))

    points = data.get("points", [])
    last_value = points[-1]["value"] if points else 0.0
    prev_value = points[-2]["value"] if len(points) > 1 else last_value
    diff = round(last_value - prev_value, 2) if prev_value else 0.0
    pct = round((diff / prev_value * 100), 2) if prev_value else 0.0
    status = "Up" if diff > 0 else ("Down" if diff < 0 else "Flat")

    last_updated_at = data.get("last_updated_at") or dt.datetime.now().strftime("%H:%M:%S %d/%m/%Y")
    
    labels = [p["label"] for p in points]
    values = [p["value"] for p in points]

    today_name = today_name_ar()

    return render_template(
        "trend.html",
        labels=labels,
        values=values,
        series_name=data.get("series_name", "Gold"),
        last_value=last_value,
        diff=diff,
        pct=pct,
        status=status,
        last_updated_at=last_updated_at,   
        today_name=today_name              
    )


@app.route("/basic", methods=["GET", "POST"])
def basic_calc():
    price_24 = fetch_aed_per_gram_24k() # AED per gram for 24K
    karats = [24, 22, 21, 18]

    grams = float(request.form.get("grams", "0") or 0)
    karat = int(request.form.get("karat", "24"))

    gold_type = request.form.get("gold_type", "raw")  # "raw" أو "bar"

    default_bar_fee = 40.0
    bar_fee = 0.0

    if gold_type == "bar" and grams > 0:
        try:
            bar_fee = float(request.form.get("bar_fee", default_bar_fee))
        except ValueError:
            bar_fee = default_bar_fee
        bar_fee = max(bar_fee, 0.0)

    price_per_g = round(price_24 * KARAT_FACTORS[karat], 2)
    total = round(grams * price_per_g, 2)
    final_total = total if gold_type == "raw" else round(total + bar_fee, 2)

    prices_by_karat = {k: round(price_24 * KARAT_FACTORS[k], 2) for k in karats}
    prices_json = json.dumps(prices_by_karat)  

    return render_template(
        "basic.html",
        grams=grams,
        karat=karat,
        karats=karats,
        price_per_g=price_per_g,
        total=total,
        final_total=final_total,
        gold_type=gold_type,
        bar_fee=bar_fee, 
        prices_json=prices_json,                
        currency="AED",
    )
    

@app.route("/zakat", methods=["GET", "POST"])
def zakat():
    karat_prices = get_igold_karat_prices()     # {'24': ..., '22': ..., '21': ..., '18': ...}
    zakat_value = None
    resolved_price = None

    # Defaults for the UI
    price_source = request.form.get("price_source", "karat")  # 'karat' or 'manual'
    selected_karat = request.form.get("selected_karat", "24")
    manual_price = request.form.get("manual_price", "")

    if request.method == "POST":
        gold_amount = float(request.form.get("gold_amount", 0) or 0)

        if price_source == "karat":
            resolved_price = float(karat_prices.get(selected_karat, 0) or 0)
        else:
            resolved_price = float(manual_price or 0)

        total_value = gold_amount * resolved_price
        zakat_value = total_value * 0.025  # 2.5%

    return render_template(
        "zakat.html",
        zakat_value=zakat_value,
        karat_prices=karat_prices,
        price_source=price_source,
        selected_karat=selected_karat,
        manual_price=manual_price,
        resolved_price=resolved_price,
    )

@app.route("/more", methods=["GET","POST"])
def more():
    s = load_settings()

    # small helpers so empty strings don't crash float()/int()
    def f_int(name, default=None):
        v = request.form.get(name, "")
        try: return int(v) if v != "" else (default if default is not None else None)
        except ValueError: return default if default is not None else None

    def f_float(name, default=None):
        v = request.form.get(name, "")
        try: return float(v) if v != "" else (default if default is not None else None)
        except ValueError: return default if default is not None else None

    def f_bool(name):
        # checkbox sends "on" when checked, missing when unchecked
        return request.form.get(name) in ("on", "true", "1", "yes")

    if request.method == "POST":
        # ===== UI =====
        s["ui"]["lang"]         = request.form.get("lang",  s["ui"]["lang"])
        s["ui"]["theme"]        = request.form.get("theme", s["ui"]["theme"])
        s["ui"]["default_mode"] = request.form.get("default_mode", s["ui"]["default_mode"])
        s["ui"]["unit"]         = request.form.get("unit",  s["ui"]["unit"])
        s["ui"]["dark"]         = f_bool("dark_mode")

        # ===== Pricing =====
        s["pricing"]["source"]          = request.form.get("source", s["pricing"]["source"])
        s["pricing"]["auto_refresh_sec"] = f_int("auto_refresh_sec", s["pricing"]["auto_refresh_sec"])
        s["pricing"]["vat"]             = f_float("vat", s["pricing"]["vat"])
        s["pricing"]["bar_fee"]         = f_float("bar_fee", s["pricing"]["bar_fee"])

        # (optional) if you add manual base price field:
        # s["pricing"]["manual_24k_aed_per_g"] = f_float("manual_24k_aed_per_g", s["pricing"].get("manual_24k_aed_per_g"))

        # ===== Zakat =====
        s["zakat"]["nisab_ref"]  = request.form.get("nisab_ref", s["zakat"]["nisab_ref"])
        s["zakat"]["rate"]       = f_float("zakat_rate", s["zakat"]["rate"])
        s["zakat"]["hawl_start"] = request.form.get("hawl_start", s["zakat"]["hawl_start"])

        # NEW (optional): reminder window days + time (HH:MM)
        s["zakat"]["reminder_days_before"] = f_int("reminder_days_before", s["zakat"].get("reminder_days_before", 15))
        s["zakat"]["reminder_time"]        = request.form.get("reminder_time", s["zakat"].get("reminder_time", "09:00"))

        # ===== Notifications =====
        s["notifications"]["zakat_reminder"]  = f_bool("zakat_reminder")
        s["notifications"]["price_change_pct"] = f_float("price_change_pct", s["notifications"]["price_change_pct"])

        # ===== Inventory defaults (optional, if you add these inputs) =====
        s.setdefault("inventory", {})
        s["inventory"]["currency"] = request.form.get("inventory_currency", s["inventory"].get("currency", "AED"))
        s["inventory"]["exclude_jewelry_from_zakat"] = f_bool("exclude_jewelry_from_zakat")

        save_settings(s)
        return redirect(url_for("more"))  # PRG pattern

    return render_template("more.html", s=s)

@app.route("/about")                 
def about_app():          return render_template("about.html")
@app.route("/how-it-works")          
def how_it_works():       return render_template("how_it_works.html")
@app.route("/disclaimer")            
def disclaimer():         return render_template("disclaimer.html")

@app.route("/portfolio")             
def portfolio():          return render_template("portfolio.html")
@app.route("/media")                 
def media():              return render_template("media.html")               # receipts & photos
@app.route("/prices")                
def prices_charts():      return render_template("prices.html")
@app.route("/calculators")           
def smart_calculators():  return render_template("calculators.html")
@app.route("/zakat_reminder")                 
def zakat_reminder():              return render_template("zakat_reminder.html")
@app.route("/logs")                  
def logs():               return render_template("logs.html")
@app.route("/notifications")         
def notifications():      return render_template("notifications.html")

@app.route("/settings")              
def settings():           return render_template("settings.html")
@app.route("/location")              
def location():           return render_template("location.html")
@app.route("/export")                
def export_backup():      return render_template("export_backup.html")

@app.route("/invest")                
def investment_strategy():return render_template("invest.html")
@app.route("/knowledge")             
def knowledge_hub():      return render_template("knowledge.html")
@app.route("/goals")                 
def goals():              return render_template("goals.html")

@app.route("/rate")                  
def rate_app():           return render_template("rate.html")
@app.route("/support")               
def support():            return render_template("support.html")
@app.route("/developer")             
def developer():          return render_template("developer.html")

# --------------------------------
UPLOAD_DIR = Path("data/uploads")
ALLOWED_EXTENSIONS = {"png","jpg","jpeg","webp","pdf"}
MAX_FILE_SIZE_MB = 5 # per-file limit (you can keep 5 if you want)

BASE_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{os.path.join(BASE_DIR, 'gold.db')}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")

app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024 # 25 MB

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)

class GoldItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(20), nullable=False)  # raw | bar | jewelry
    karat = db.Column(db.Integer, default=24)
    karat_factor = db.Column(db.Float, default=1.0)
    weight_g = db.Column(db.Float, nullable=False)
    price_per_g = db.Column(db.Float)
    total_paid = db.Column(db.Float)
    vendor = db.Column(db.String(120))
    purchase_date = db.Column(db.Date, default=dt.date.today)
    location = db.Column(db.String(120))
    notes = db.Column(db.Text)
    zakat_exempt = db.Column(db.Boolean, default=False)
    hawl_date = db.Column(db.Date)
    image_path  = db.Column(db.String(300))
    receipt_path = db.Column(db.String(300))


def karat_to_factor(karat):
    return {24: 1.0, 22: 0.9167, 21: 0.875, 18: 0.75}.get(int(karat), 1.0)

ALLOWED_IMG_EXTENSIONS  = {"png", "jpg", "jpeg", "webp","heic","heif","bmp","tif","tiff"}          # images only
ALLOWED_RCPT_EXTENSIONS = ALLOWED_IMG_EXTENSIONS | {"pdf"}   # images or PDF

def slugify(text: str, allow_arabic: bool = True) -> str:
    """Collapse text to a safe slug. Keeps Arabic letters if allow_arabic=True."""
    if not text:
        return "na"
    text = text.strip()
    if not allow_arabic:
        text = unicodedata.normalize("NFKD", text).encode("ascii","ignore").decode("ascii")
    # keep word chars + Arabic range + dashes/underscores    
    text = re.sub(r"[^\w\u0600-\u06FF\-]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-").lower()
    return text or "na"

def build_upload_name(item_id: int, kind: str, category: str, ext: str,
                      idx: int = 1, ts: str | None = None) -> str:
    """
    item_id: DB id (int)
    kind: 'image' or 'receipt'
    category: raw/bar/jewelry/... (any string)
    ext: file extension WITHOUT dot
    idx: sequence number starting at 1
    ts: optional timestamp string; if None uses now

    Human-readable pattern:
      ####_<kind>_<category>_<YYYYMMDD>_<idx>.<ext>
      0007_image_سبيكة_20251020_153601_01.jpg
    Falls back to UUID if args are missing.
    """
    ts = ts or dt.datetime.now().strftime("%Y%m%d")
    kind_slug = slugify(kind, allow_arabic=False)
    cat_slug  = slugify(category or "uncat")
    return f"{item_id:04d}_{kind_slug}_{cat_slug}_{ts}_{idx:02d}.{ext.lower()}"

def allowed_ext(filename: str, kind: str) -> bool:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if kind == "image":
        return ext in ALLOWED_IMG_EXTENSIONS
    if kind == "receipt":
        return (ext in ALLOWED_IMG_EXTENSIONS) or (ext in ALLOWED_RCPT_EXTENSIONS)
    return False


def save_file(fs, allowed_ext, *, kind: str | None = None,
              item_id: int | None = None, category: str | None = None,
              idx: int | None = None):
    """
    Return stored filename (not full path) or None.

    Backward compatible:
      save_file(fs, allowed_ext) -> UUID filename
    Human-readable (recommended):
      save_file(fs, allowed_ext, kind="image"/"receipt",
                item_id=item.id, category=item.category, idx=1)
    """
    if not fs or not getattr(fs, "filename", ""):
        return None
    
    # ---- extension check ----------------------------------------------------
    if "." not in fs.filename:
        flash("❌ ملف بدون امتداد.", "danger")
        return None
    ext = fs.filename.rsplit(".", 1)[-1].lower()
    if ext not in allowed_ext:
        flash("❌ ملف غير مسموح به لهذا الحقل.", "danger")
        return None

    # ---- size check (per-file limit) ----------------------------------------
    try:
        pos = fs.stream.tell()
        fs.stream.seek(0, os.SEEK_END)
        size = fs.stream.tell()
        fs.stream.seek(pos)
    except Exception:
        size = None

    # Prefer a per-file limit if you defined it; otherwise fall back
    max_bytes = None
    if "MAX_FILE_SIZE_MB" in globals():
        try:
            max_bytes = int(MAX_FILE_SIZE_MB) * 1024 * 1024
        except Exception:
            max_bytes = None
    if max_bytes is None:
        # LAST RESORT: use request limit if configured (applies to whole request)
        max_bytes = app.config.get("MAX_CONTENT_LENGTH", None)

    if size is not None and max_bytes and size > max_bytes:
        flash("⚠️ حجم الملف أكبر من الحد المسموح.", "danger")
        return None

    # ---- build a filename ---------------------------------------------------
    if None in (kind, item_id, category, idx):
        # Fallback to UUID if context missing
        fname = f"{uuid4().hex}.{ext}"
    else:
        fname = build_upload_name(item_id=item_id, kind=kind, category=category, ext=ext, idx=idx)

    # keep Arabic; just minimal cleaning
    fname = re.sub(r"[^\w\-. \u0600-\u06FF]", "_", fname)

    # ---- save ---------------------------------------------------------------
    dest = os.path.join(app.config["UPLOAD_FOLDER"], fname)
    fs.save(dest)
    return fname


def csv_to_list(s):
    s = (s or "").strip()
    if not s:
        return []
    # ينظّف أقواس أو اقتباسات محتملة
    s = s.replace("[", "").replace("]", "").replace('"', "").replace("'", "")
    parts = [p.strip().split("/")[-1] for p in s.split(",")]
    return [p for p in parts if p]

def list_to_csv(lst):
    return ",".join([x for x in lst if x])

def store_files(files, allowed):
    out = []
    for fs in files:
        if not fs or not fs.filename:
            continue
        ext = (fs.filename.rsplit('.', 1)[-1] or '').lower()
        if ext not in allowed:
            continue
        fname = secure_filename(f"{uuid.uuid4().hex}.{ext}")
        fs.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
        out.append(fname)
    return out

def get_local_ip():
    """Returns your machine's local IP address on the Wi-Fi network."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # Google DNS to determine outbound IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@app.route("/inventory/add", methods=["GET", "POST"], endpoint="add_inventory")
def add_inventory():
    if request.method == "POST":
        # ---- safe getters ---------------------------------------------------
        def f_str(name, default=""):
            return (request.form.get(name) or default).strip()

        def f_float(name, default=0.0):
            v = request.form.get(name, "")
            try: return float(v) if v not in ("", None) else default
            except ValueError: return default

        def f_date(name):
            v = request.form.get(name, "")
            try: return dt.datetime.strptime(v, "%Y-%m-%d").date() if v else None
            except ValueError: return None

        # ---- base fields ----------------------------------------------------
        category     = f_str("category", "raw")
        karat        = int(request.form.get("karat") or 24)
        weight_g     = f_float("weight_g", 0.0)
        price_per_g  = f_float("price_per_g", 0.0) or None
        total_paid   = f_float("total_paid", 0.0) or None
        vendor       = f_str("vendor")
        location     = f_str("location")
        notes        = f_str("notes")
        zakat_exempt = bool(request.form.get("zakat_exempt"))

        purchase_date = f_date("purchase_date") or dt.date.today()
        # hijri year
        hawl_date = f_date("hawl_date") or (purchase_date + dt.timedelta(days=354))

        # ---- files (MULTI) ---------------------------------------------------
        MAX_IMAGES = 5
        MAX_RECEIPTS = 2


        image_files = request.files.getlist('images[]')
        receipt_files = request.files.getlist('receipts[]')


        if not image_files and not receipt_files:
            flash("لم تُرفَع أي مرفقات. يمكنك المتابعة بدون صور، أو أضف صورًا/فواتير.", "warning")

        if len(image_files) > MAX_IMAGES:
            flash(f"الحد الأقصى للصور هو {MAX_IMAGES}.", "danger")
            return redirect(request.url)
        if len(receipt_files) > MAX_RECEIPTS:
            flash(f"الحد الأقصى للإيصالات هو {MAX_RECEIPTS}.", "danger")
            return redirect(request.url)

        # ---- create row ------------------------------------------------------
        item = GoldItem(
            category=category,
            karat=karat,
            karat_factor=karat_to_factor(karat),
            weight_g=weight_g,
            price_per_g=price_per_g,
            total_paid=total_paid,
            vendor=vendor,
            location=location,
            notes=notes,
            zakat_exempt=zakat_exempt,
            purchase_date=purchase_date,
            hawl_date=hawl_date
        )
        
        
        db.session.add(item)
        db.session.commit()

        # ALWAYS initialize these lists before using them
        saved_imgs: list[str] = []
        saved_rcpts: list[str] = []

        # accept either "images" or "images[]" input names
        images_in = request.files.getlist("images") or request.files.getlist("images[]")
        rcpts_in  = request.files.getlist("receipts") or request.files.getlist("receipts[]")

        # images (input name must be "images")
        for i, f in enumerate(images_in, start=1):
            if not f or not f.filename:
                continue
            fn = save_file(
                f, ALLOWED_IMG_EXTENSIONS,
                kind="image", item_id=item.id,
                category=item.category, idx=i
            )
            if fn:
                saved_imgs.append(fn)

        # receipts (input name must be "receipts")
        for i, f in enumerate(rcpts_in, start=1):
            if not f or not f.filename:
                continue
            fn = save_file(
                f, ALLOWED_RCPT_EXTENSIONS,
                kind="receipt", item_id=item.id,
                category=item.category, idx=i
            )
            if fn:
                saved_rcpts.append(fn)


        
        # write CSVs back (lists may be empty; that's fine)
        item.image_path   = list_to_csv(saved_imgs) if saved_imgs else None
        item.receipt_path = list_to_csv(saved_rcpts) if saved_rcpts else None

        db.session.commit()

        flash("تمت إضافة القطعة وحفظ المرفقات بنجاح", "success")
        return redirect(url_for("inventory_list", item_id=item.id))

    return render_template("inventory_add.html")





@app.route("/inventory", methods=["GET"])
def inventory_list():
    items = GoldItem.query.order_by(GoldItem.purchase_date.desc()).all()
    return render_template("inventory_list.html", items=items)


@app.route("/inventory/<int:item_id>", methods=["GET"])
def inventory_detail(item_id):
    item = GoldItem.query.get_or_404(item_id)

    image_files   = csv_to_list(item.image_path)     
    receipt_files = csv_to_list(item.receipt_path)

    return render_template("inventory_detail.html",
                           item=item,
                           image_files=image_files,
                           receipt_files=receipt_files)



# make sure .pdf is registered correctly
mimetypes.add_type("application/pdf", ".pdf")
mimetypes.add_type("image/heic", ".heic")
mimetypes.add_type("image/heif", ".heif")


# (serve uploaded images/receipts)
@app.route("/uploads/<path:filename>")
def uploaded_file(filename):

        # --- Sanitize and locate file ---
    safe_name = secure_filename(os.path.basename(filename))
    full_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)

    if not os.path.isfile(full_path):
        current_app.logger.warning("Missing upload: %s", full_path)
        abort(404)

    # --- Detect mimetype (PDF, PNG, JPG, etc.) ---
    mime_type = mimetypes.guess_type(full_path)[0] or "application/octet-stream"

    # --- Send file inline (so browsers display it, not download) ---
    response = make_response(
        send_file(
            full_path,
            mimetype=mime_type,
            as_attachment=False,
            download_name=safe_name,
            conditional=True,  # adds proper headers (Content-Length, etc.)
        )
    )

    # --- Disable caching for PDFs (fixes iOS Safari blank on reopen) ---
    if mime_type == "application/pdf":
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response

# ---- Download (attachment) ----
@app.route("/uploads/download/<path:filename>")
def download_uploaded(filename):
    safe_name = secure_filename(os.path.basename(filename))
    full_path = os.path.join(app.config["UPLOAD_FOLDER"], safe_name)

    if not os.path.isfile(full_path):
        current_app.logger.warning("Missing upload: %s", full_path)
        abort(404)

    # Force download
    return send_file(
        full_path,
        as_attachment=True,
        download_name=safe_name,
        conditional=True,
        max_age=0,
    )


@app.route("/inventory/<int:item_id>/edit",
           methods=["GET", "POST"],
           endpoint="inventory_edit")
def inventory_edit(item_id):
    # 1) fetch the item
    item = GoldItem.query.get_or_404(item_id)

    # keep current paths around if you’re not re-uploading now
    existing_imgs  = csv_to_list(item.image_path)
    existing_rcpts = csv_to_list(item.receipt_path)


    if request.method == "POST":
        # --- safe getters (avoid ValueError) ---
        def f_str(name, default=None):
            v = request.form.get(name)
            return v.strip() if v and isinstance(v, str) else default

        def f_float(name, default=None):
            v = request.form.get(name, "")
            try:
                return float(v) if v not in ("", None) else default
            except ValueError:
                return default

        def f_int(name, default=None):
            v = request.form.get(name, "")
            try:
                return int(v) if v not in ("", None) else default
            except ValueError:
                return default

        def f_date(name):
            v = request.form.get(name, "")
            try:
                return dt.datetime.strptime(v, "%Y-%m-%d").date() if v else None
            except ValueError:
                return None

        # 2) update fields
        item.category     = f_str("category", item.category)
        item.karat        = f_int("karat", item.karat or 24)
        item.weight_g     = f_float("weight_g", item.weight_g or 0.0)
        item.price_per_g  = f_float("price_per_g", None)
        item.total_paid   = f_float("total_paid", None)

        item.purchase_date = f_date("purchase_date")
        item.hawl_date     = f_date("hawl_date")

        item.vendor   = f_str("vendor")
        item.location = f_str("location")
        item.notes    = f_str("notes")

        # append new images
        for i, f in enumerate(request.files.getlist("images"), start=1):
            fn = save_file(
                f, ALLOWED_IMG_EXTENSIONS,
                kind="image", item_id=item.id, category=item.category, idx=len(existing_imgs)+i
            )
            if fn: existing_imgs.append(fn)

        # append new receipts
        for i, f in enumerate(request.files.getlist("receipts"), start=1):
            fn = save_file(
                f, ALLOWED_RCPT_EXTENSIONS,
                kind="receipt", item_id=item.id, category=item.category, idx=len(existing_rcpts)+i
            )
            if fn: existing_rcpts.append(fn)

        # keep existing file lists (unless you handle uploads here)
        item.image_path   = list_to_csv(existing_imgs) or None
        item.receipt_path = list_to_csv(existing_rcpts) or None

        db.session.commit()
        flash("تم حفظ التعديلات بنجاح", "success")
        return redirect(url_for("inventory_detail", item_id=item.id))

    # 3) render edit form (reuse add form)
    return render_template("inventory_add.html", item=item, mode="edit")



@app.route(
    "/inventory/<int:item_id>/delete",
    methods=["POST"],
    endpoint="inventory_delete")
def delete_inventory(item_id):
    # Retrieve the record or return 404 if not found
    item = GoldItem.query.get_or_404(item_id)

    # Delete the record
    db.session.delete(item)
    db.session.commit()

    # Flash confirmation message
    flash("تم حذف القطعة بنجاح", "danger")

    # Redirect back to the inventory list
    return redirect(url_for("inventory_list"))

@app.route("/instruction")
def instructions():
    return render_template("instructions.html")

@app.get("/buy", endpoint="buy")
def buy():
    # TODO: ضع هنا منطق الدفع/التحويل لبوابة الدفع
    # مؤقتاً نعيد صفحة بسيطة/أو تحويل:
    return render_template("checkout.html") 
    # أو: return redirect("https://your-payment-gateway.com/...")

@app.route("/static/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")

@app.route("/static/sw.js")
def sw():
    return send_from_directory("static", "sw.js", mimetype="application/javascript")


from io import BytesIO
from flask import send_file

@app.route("/quote/pdf")
def quote_pdf():
    """Generate a simple PDF quote/receipt from query params.
    Note: PDF labels are English for better rendering reliability."""
    def to_f(x, d=0.0):
        try:
            return float(x) if x not in (None, "") else d
        except ValueError:
            return d

    mode = request.args.get("mode", "buy")
    w    = to_f(request.args.get("w"))
    p    = to_f(request.args.get("p"))
    k    = to_f(request.args.get("k"), 1.0)
    t    = to_f(request.args.get("t"))
    vat  = to_f(request.args.get("vat"), 0.0)
    mg   = to_f(request.args.get("mg"), 0.0)

    # compute again (server-trust)
    if mode == "sell":
        r = calc_sell_mode(w, p, t, k, mg)
    else:
        r = calc_buy_mode(w, p, t, k, vat)

    # Build PDF
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    y = height - 24*mm
    c.setFont("Helvetica-Bold", 16)
    c.drawString(20*mm, y, "Smart Gold Calculator - Quote")
    y -= 10*mm

    c.setFont("Helvetica", 10)
    c.drawString(20*mm, y, f"Mode: {'BUY' if r.get('mode')=='buy' else 'SELL'}")
    c.drawRightString(width-20*mm, y, f"Generated: {time.strftime('%Y-%m-%d %H:%M')}")
    y -= 8*mm

    def line(label, value):
        nonlocal y
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, y, label)
        c.setFont("Helvetica-Bold", 10)
        c.drawRightString(width-20*mm, y, value)
        y -= 7*mm

    line("Weight (g)", f"{r.get('weight', 0):.4f}")
    line("Price per gram", f"{r.get('price_per_g', 0):.4f}")
    line("Karat factor", f"{r.get('karat_factor', 1.0):.4f}")
    if r.get("mode") == "buy":
        line("VAT rate", f"{r.get('vat_rate', 0.0)*100:.2f}%")
    else:
        line("Dealer margin / g", f"{r.get('margin_per_g', 0.0):.2f}")

    y -= 4*mm
    c.line(20*mm, y, width-20*mm, y)
    y -= 8*mm

    line("Gold value", f"{r.get('gold_value', 0):.2f}")
    if r.get("mode") == "buy":
        line("VAT value", f"{r.get('vat_value', 0):.2f}")
    line("Fair price", f"{r.get('fair_price', 0):.2f}")
    line("Offered total", f"{r.get('offered_total', 0):.2f}")
    if r.get("mode") == "buy":
        line("Making fee / g", f"{r.get('making_fee_per_g', 0):.2f}")
        line("Seller profit", f"{r.get('seller_profit', 0):.1f} ({r.get('profit_pct', 0)*100:.2f}%)")
    else:
        line("Difference", f"{r.get('diff', 0):.2f}")

    y -= 8*mm
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(20*mm, y, "Disclaimer: This is an estimate for guidance only. Final pricing may vary by dealer & fees.")
    y -= 10*mm

    c.setFont("Helvetica", 9)
    c.drawString(20*mm, 15*mm, "Zakati App • Gold Industry Tools")
    c.showPage()
    c.save()

    buf.seek(0)
    filename = f"gold_quote_{mode}_{int(time.time())}.pdf"
    return send_file(buf, as_attachment=True, download_name=filename, mimetype="application/pdf")


if __name__ == "__main__":
    local_ip = get_local_ip()
    print(f"🚀 Flask app is running!\n"
          f"👉 On this computer: http://127.0.0.1:5000\n"
          f"📱 On your phone:    http://{local_ip}:5000\n")

    with app.app_context():
        db.create_all()

    app.run(host='0.0.0.0', port=5000, debug=True)