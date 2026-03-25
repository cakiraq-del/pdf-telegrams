# --- PY313 fix: provide imghdr stub before telegram imports ---
import sys, types
try:
    import imghdr  # Python 3.12'de var; 3.13'te yok.
except ModuleNotFoundError:
    m = types.ModuleType("imghdr")
    def what(file, h=None):  # PTB'nin ihtiyacı sadece import başarısı; fonk no-op
        return None
    m.what = what
    sys.modules["imghdr"] = m
# --- END PY313 fix ---
import os
import time
import tempfile
import logging
import requests
import threading  # ✅ YENİ: Sahte sunucu için
from http.server import BaseHTTPRequestHandler, HTTPServer  # ✅ YENİ: Sahte sunucu için
from dotenv import load_dotenv
from datetime import datetime, date, timedelta, timezone
import json
import pytz  # ✅ zoneinfo yerine pytz kullanıyoruz

TR_TZ = pytz.timezone("Europe/Istanbul")  # ✅ ZoneInfo yerine pytz

from telegram import Update, InputFile
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters,
    ConversationHandler, CallbackContext
)

# ⏰ Scheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ================== AYAR ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_KEY = os.getenv("BOT_KEY")  # 🔑 siteyle aynı olmalı

PDF_URL = "https://pdfffcngz.onrender.com/generate"  # Ücret formu endpoint'i
KART_PDF_URL = "https://pdfffcngz.onrender.com/generate2"
BURS_PDF_URL = "https://pdfffcngz.onrender.com/generate3"  # ✅ Burs endpoint'i (sablon3.pdf)
DIP_PDF_URL = "https://pdfffcngz.onrender.com/diploma"  # ✅ Dip endpoint'i (d.pdf)
PDF2_URL = "https://pdfffcngz.onrender.com/pdf2"  # ✅ YENİ: PDF2 endpoint'i (sablon4.pdf)

HEADERS_BASE = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/pdf,application/octet-stream,*/*",
    "Referer": "https://pdfffcngz.onrender.com/",
    "X-Requested-With": "XMLHttpRequest",
}
def _headers():
    """Her istekte X-Bot-Key ekle (varsa)."""
    h = dict(HEADERS_BASE)
    if BOT_KEY:
        h["X-Bot-Key"] = BOT_KEY
    return h

# ✅ SADECE İZİN VERDİĞİN GRUPLAR
ALLOWED_CHAT_ID = {-1002955588714}

# ====== ADMIN KİLİDİ ======
ADMIN_ID = 6672759317  # 👈 sadece bu kullanıcı admin

def _is_admin(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id == ADMIN_ID)

def _require_admin(update: Update) -> bool:
    """Admin değilse kullanıcıyı uyarır, False döner."""
    if not _is_admin(update):
        try:
            update.message.reply_text("⛔ Bu komutu kullanma yetkin yok.@CengizzAtay")
        except Exception:
            pass
        return False
    return True

# ====== GEÇİCİ İZİN (SÜRELİ HAK) ======
PERMS_FILE = "temp_perms.json"  # geçici izinlerin saklandığı dosya

def _now_utc():
    return datetime.now(timezone.utc)

def _load_perms():
    try:
        with open(PERMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        return {}

def _save_perms(perms: dict):
    try:
        with open(PERMS_FILE, "w", encoding="utf-8") as f:
            json.dump(perms, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"temp_perms yazılamadı: {e}")

def _prune_expired(perms: dict) -> dict:
    changed = False
    now = _now_utc()
    out = {}
    for k, iso in perms.items():
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt > now:
                out[k] = iso
            else:
                changed = True
        except Exception:
            changed = True
    if changed:
        _save_perms(out)
    return out

TEMP_PERMS = _prune_expired(_load_perms())

def _add_temp(chat_id: int, until_dt_utc: datetime):
    global TEMP_PERMS
    TEMP_PERMS[str(chat_id)] = until_dt_utc.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    _save_perms(TEMP_PERMS)

def _is_temp_allowed(chat_id: int) -> bool:
    global TEMP_PERMS
    TEMP_PERMS = _prune_expired(TEMP_PERMS)
    iso = TEMP_PERMS.get(str(chat_id))
    if not iso:
        return False
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) > _now_utc()
    except Exception:
        return False

# ====== KARA LİSTE (ANINDA KAPAT /bitir) ======
DENY_FILE = "deny_groups.json"
def _load_deny():
    try:
        with open(DENY_FILE, "r", encoding="utf-8") as f:
            arr = json.load(f)
            return set(int(x) for x in arr)
    except Exception:
        return set()

def _save_deny(s: set):
    try:
        with open(DENY_FILE, "w", encoding="utf-8") as f:
            json.dump(list(s), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"deny_groups yazılamadı: {e}")

DENY_GROUPS = _load_deny()

# ====== HAK (ADET) SİSTEMİ ======
QUOTA_FILE = "quota_rights.json"

def _load_quota():
    try:
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # {chat_id_str: int}
            out = {}
            for k, v in data.items():
                try:
                    out[str(int(k))] = int(v)
                except Exception:
                    pass
            return out
    except Exception:
        return {}

def _save_quota(d: dict):
    try:
        with open(QUOTA_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"quota yazılamadı: {e}")

QUOTA = _load_quota()

def _get_quota(chat_id: int) -> int:
    return int(QUOTA.get(str(chat_id), 0))

def _set_quota(chat_id: int, amount: int):
    global QUOTA
    QUOTA[str(chat_id)] = max(0, int(amount))
    _save_quota(QUOTA)

def _dec_quota_if_applicable(chat_id: int):
    """
    Sadece ALLOWED veya TEMP izni YOKSA düş.
    (Süre izni varsa sınırsız, hak azaltılmaz.)
    """
    if chat_id in ALLOWED_CHAT_ID or _is_temp_allowed(chat_id):
        return
    rem = _get_quota(chat_id)
    if rem > 0:
        _set_quota(chat_id, rem - 1)

# ====== KONTENJAN (ÜYE SAYISI) SİSTEMİ ======
LIMIT_FILE = "group_limits.json"  # 👈 grup limitlerini saklarız
DEFAULT_LIMIT = 5  # 👈 Varsayılan maksimum üye sayısı (SİZİN İSTEĞİNİZ: 7)

def _load_limits():
    """Grup ID'si başına özel limiti yükler."""
    try:
        with open(LIMIT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # {chat_id_str: int}
            out = {}
            for k, v in data.items():
                try:
                    out[str(int(k))] = int(v)
                except Exception:
                    pass
            return out
    except Exception:
        return {}

def _save_limits(d: dict):
    """Grup limitlerini kaydeder."""
    try:
        with open(LIMIT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"group_limits yazılamadı: {e}")

GROUP_LIMITS = _load_limits()

def _get_max_members(chat_id: int) -> int:
    """Bir grup için tanımlanmış özel limiti veya varsayılan limiti döner."""
    return int(GROUP_LIMITS.get(str(chat_id), DEFAULT_LIMIT))

def _set_max_members(chat_id: int, amount: int):
    """Bir gruba özel limit tanımlar."""
    global GROUP_LIMITS
    GROUP_LIMITS[str(chat_id)] = max(0, int(amount))
    _save_limits(GROUP_LIMITS)

# ====== GÜNLÜK RAPOR (GRUP BAŞI SAYAC) ======
REPORT_FILE = "daily_report.json"
TITLES_FILE = "group_titles.json"  # 👈 grup adlarını saklarız

MONTHS_TR = ["Ocak","Şubat","Mart","Nisan","Mayıs","Haziran","Temmuz","Ağustos","Eylül","Ekim","Kasım","Aralık"]

def _today_tr_str():
    return datetime.now(TR_TZ).strftime("%Y-%m-%d")

def _today_tr_human():
    now = datetime.now(TR_TZ)
    return f"{now.day} {MONTHS_TR[now.month-1]}"

def _load_titles():
    try:
        with open(TITLES_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return {str(k): str(v) for k, v in d.items()}
    except Exception:
        return {}

def _save_titles(d: dict):
    try:
        with open(TITLES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"group_titles yazılamadı: {e}")

GROUP_TITLES = _load_titles()

def _load_report():
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "date" in data and "counts" in data and isinstance(data["counts"], dict):
                migrated = False
                for k, v in list(data["counts"].items()):
                    if isinstance(v, int):
                        data["counts"][k] = {"pdf": int(v), "kart": 0}
                        migrated = True
                    elif isinstance(v, dict):
                        v.setdefault("pdf", 0)
                        v.setdefault("kart", 0)
                if migrated:
                    _save_report(data)
                return data
    except Exception:
        pass
    return {"date": _today_tr_str(), "counts": {}}

def _save_report(rep: dict):
    try:
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"daily_report yazılamadı: {e}")

def _ensure_today_report():
    rep = _load_report()
    today = _today_tr_str()
    if rep.get("date") != today:
        rep = {"date": today, "counts": {}}
        _save_report(rep)
    return rep

def _inc_report(chat_id: int, kind: str, title: str = None):
    """Günlük sayaç artır. (title verilirse kaydederiz.)"""
    rep = _ensure_today_report()
    key = str(chat_id)
    node = rep["counts"].get(key) or {"pdf": 0, "kart": 0}
    if kind not in ("pdf", "kart"):
        kind = "pdf"
    node[kind] = int(node.get(kind, 0)) + 1
    rep["counts"][key] = node
    _save_report(rep)

    if title:
        GROUP_TITLES[key] = title
        _save_titles(GROUP_TITLES)

def _get_today_counts(chat_id: int):
    rep = _ensure_today_report()
    node = rep["counts"].get(str(chat_id)) or {"pdf": 0, "kart": 0}
    pdf_c = int(node.get("pdf", 0))
    kart_c = int(node.get("kart", 0))
    return pdf_c, kart_c, pdf_c + kart_c

# Konuşma durumları
TC, NAME, SURNAME, MIKTAR = range(4)
# /kart için durumlar
K_ADSOYAD, K_ADRES, K_ILILCE, K_TARIH = range(4)
# /burs için durumlar
B_TC, B_NAME, B_SURNAME, B_MIKTAR = range(4)
# /dip için durumlar
D_TC, D_NAME, D_SURNAME, D_MIKTAR = range(4)
# /pdf2 için durumlar
P2_ADSOYAD, P2_TARIH = range(2)

# ================== LOG ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("telegrampdf")

# ================== YARDIMCI ==================
def tr_upper(s: str) -> str:
    if not isinstance(s, str):
        return s
    s = s.strip()
    s = s.replace("i", "İ").replace("ı", "I")
    return s.upper()

def _has_time_or_whitelist(chat_id: int) -> bool:
    return (chat_id in ALLOWED_CHAT_ID) or _is_temp_allowed(chat_id)

def _check_group(update: Update, context: CallbackContext) -> bool: # 👈 context eklendi
    chat = update.effective_chat
    if not chat:
        return False
    chat_id = chat.id

    # 1. Kara listedeyse kapat
    if chat_id in DENY_GROUPS:
        try:
            update.message.reply_text("Hakkın kapalıdır. Destek için @CengizzAtay yaz.")
        except Exception:
            pass
        return False

    # 2. Üye Sayısı Kontrolü (YENİ KONTROL)
    try:
        if chat.type in ("group", "supergroup"):
            # Güncel üye sayısını alıyoruz
            member_count = context.bot.get_chat_member_count(chat_id) # 👈 API CALL
            max_limit = _get_max_members(chat_id)
            
            if member_count > max_limit:
                msg = f"⛔ Bu grup {max_limit} kişiyle sınırlıdır. Şu an: {member_count} kişi var."
                try:
                    update.message.reply_text(msg)
                except Exception:
                    pass
                return False # Kontenjan aşımı
    except Exception as e:
        log.warning(f"Üye sayısı kontrol edilemedi: {e}")
        # Hata olursa, botun çalışmaya devam etmesi için True dönebiliriz.

    # 3. Süre/whitelist ise serbest
    if _has_time_or_whitelist(chat_id):
        return True

    # 4. Değilse hak (adet) kontrolü
    if _get_quota(chat_id) > 0:
        return True

    # 5. Hiçbiri yoksa kapalı
    try:
        update.message.reply_text("Bu grubun hakkı yoktur. /yetkiver veya /hakver kullanın.")
    except Exception:
        pass
    return False

def _parse_inline_base(text: str, command: str):
    """/pdf, /burs, /dip için ortak parser"""
    if not text:
        return None
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return None
    
    first = lines[0]
    
    clean_first = first.lstrip().lstrip('\u200B').strip()
    
    if clean_first.lower().startswith("<code>") and clean_first.lower().endswith("</code>"):
        clean_first = clean_first[6:-7].strip()
        
    clean_first = clean_first.replace("<code>", "").replace("</code>", "")

    if not clean_first.lower().startswith(f'/{command}'):
        return None

    # Çok satırlı tercih
    rest = lines[1:]
    if len(rest) >= 4:
        tc = rest[0]
        ad = rest[1]
        soyad = rest[2]
        miktar = rest[3]
        return tc, ad, soyad, miktar

    # Tek satır varyantı
    parts = clean_first.split()
    if len(parts) >= 5:
        tc = parts[1]
        ad = parts[2]
        miktar = parts[-1]
        soyad = " ".join(parts[3:-1])
        return tc, ad, soyad, miktar

    return None

def parse_pdf_inline(text: str):
    return _parse_inline_base(text, "pdf")

def parse_burs_inline(text: str):
    return _parse_inline_base(text, "burs")

def parse_dip_inline(text: str):
    return _parse_inline_base(text, "dip")

# ================== KART PARSER (Farklı format) ==================
def parse_kart_inline(text: str):
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    first_line_end = raw.find("\n")
    first_line = raw if first_line_end == -1 else raw[:first_line_end]

    clean_first_line = first_line.lstrip().lstrip('\u200B').strip()
    if clean_first_line.lower().startswith("<code>") and clean_first_line.lower().endswith("</code>"):
        clean_first_line = clean_first_line[6:-7].strip()
    clean_first_line = clean_first_line.replace("<code>", "").replace("</code>", "")
    
    if not clean_first_line.lower().startswith("/kart"):
        return None
    
    rest_text = "" if first_line_end == -1 else raw[first_line_end+1:]
    rest_lines = [l.strip() for l in rest_text.splitlines() if l.strip()]
    if len(rest_lines) >= 4:
        adsoyad = rest_lines[0]
        adres = rest_lines[1]
        ililce = rest_lines[2]
        tarih = rest_lines[3]
        return adsoyad, adres, ililce, tarih
    return None

# ================== PDF2 PARSER (YENİ) ==================
def parse_pdf2_inline(text: str):
    """
    /pdf2
    AD SOYAD
    TARİH
    formatı için
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None
    
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        return None
    
    first = lines[0]
    clean_first = first.lstrip().lstrip('\u200B').strip()
    
    if clean_first.lower().startswith("<code>") and clean_first.lower().endswith("</code>"):
        clean_first = clean_first[6:-7].strip()
    clean_first = clean_first.replace("<code>", "").replace("</code>", "")

    if not clean_first.lower().startswith("/pdf2"):
        return None

    # Çok satırlı kullanım
    rest = lines[1:]
    if len(rest) >= 2:
        adsoyad = rest[0]
        tarih = rest[1]
        return adsoyad, tarih

    # Tek satır kullanım (/pdf2 AD SOYAD TARİH) denemesi
    parts = clean_first.split()
    if len(parts) >= 3:
        tarih = parts[-1]
        adsoyad = " ".join(parts[1:-1])
        return adsoyad, tarih

    return None

# ================== RENDER PORT FIX (SAHTE SUNUCU) ==================
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Telegram Bot is running on Render!")

def run_dummy_server():
    port = int(os.environ.get("PORT", 8080)) # Render'ın atadığı portu alıyoruz
    try:
        server = HTTPServer(("0.0.0.0", port), DummyHandler)
        log.info(f"Sahte web sunucusu port {port} üzerinde baslatildi.")
        server.serve_forever()
    except Exception as e:
        log.error(f"Sahte sunucu baslatilamadi: {e}")

# ================== HANDLER'lar ==================
def cmd_start(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return ConversationHandler.END
    # admin için bilgi mesajı (normal /start artık kilitli)
    update.message.reply_text("Admin panel komutları: /yetkiver, /hakver, /kalanhak, /bitir, /rapor")
    return ConversationHandler.END

def cmd_whereami(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    cid = update.effective_chat.id if update.effective_chat else None
    uid = update.effective_user.id if update.effective_user else None
    update.message.reply_text(f"Chat ID: {cid}\nUser ID: {uid}")

# 🚀 YENİ TEST KOMUTUMUZ
def cmd_ping(update: Update, context: CallbackContext):
    update.message.reply_text("Pong! Yeni kod devrede kanks!")

# Süre verme komutu — SADECE ADMIN
def cmd_yetkiver(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /yetkiver <gün>  (1–30 arası)")
        return
    days = int(digits)
    if days < 1 or days > 30:
        update.message.reply_text("Gün 1 ile 30 arasında olmalı.")
        return
    until_utc = _now_utc() + timedelta(days=days)
    _add_temp(chat_id, until_utc)

    # bitir ile kapatılmışsa kaldır
    global DENY_GROUPS
    if chat_id in DENY_GROUPS:
        DENY_GROUPS.remove(chat_id)
        _save_deny(DENY_GROUPS)

    update.message.reply_text(f"Bu gruba {days} günlük izin verildi.")

# Hak verme (adet) — SADECE ADMIN
def cmd_hakver(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        update.message.reply_text("Kullanım: /hakver <adet>  (örn: /hakver 20)")
        return
    amount = int(digits)
    if amount < 0:
        update.message.reply_text("Adet 0 veya üstü olmalı.")
        return
    _set_quota(chat_id, amount)

    # Eğer kara listedeyse aç (hak tanındıysa kullanabilsin)
    global DENY_GROUPS
    if chat_id in DENY_GROUPS:
        DENY_GROUPS.remove(chat_id)
        _save_deny(DENY_GROUPS)

    update.message.reply_text(f"✅ Bu gruba {amount} adet PDF hakkı tanımlandı.")

# Kalan hak — SADECE ADMIN
def cmd_hakdurum(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    rem = _get_quota(chat_id)
    msg = f"Kalan hak: {rem}"
    if _has_time_or_whitelist(chat_id):
        msg += "\n(Not: Süreli/whitelist izni olduğu için hak düşmez.)"
    update.message.reply_text(msg)

# Anında kapat — SADECE ADMIN
def cmd_bitir(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id

    global TEMP_PERMS
    if str(chat_id) in TEMP_PERMS:
        del TEMP_PERMS[str(chat_id)]
        _save_perms(TEMP_PERMS)

    global DENY_GROUPS
    DENY_GROUPS.add(chat_id)
    _save_deny(DENY_GROUPS)

    update.message.reply_text("⛔ Bu grubun hakkı kapatıldı.")

# Kontenjan (maksimum üye sayısı) belirleme — SADECE ADMIN
def cmd_kontenjan(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    raw = " ".join(context.args or [])
    digits = "".join(ch for ch in raw if ch.isdigit())

    if not digits:
        current = _get_max_members(chat_id)
        update.message.reply_text(f"Kullanım: /kontenjan <adet> (örn: /kontenjan 10). Varsayılan: {DEFAULT_LIMIT}. Bu grubun limiti: {current}")
        return

    amount = int(digits)
    if amount < 1:
        update.message.reply_text("Kontenjan en az 1 veya üstü olmalı.")
        return

    _set_max_members(chat_id, amount)
    update.message.reply_text(f"✅ Bu grubun maksimum üye kontenjanı {amount} olarak ayarlandı.")
    
# Günlük rapor — SADECE ADMIN (o anki grup için)
def cmd_rapor(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    chat = update.effective_chat
    if not chat:
        return
    chat_id = chat.id
    human_day = _today_tr_human()
    pdf_c, kart_c, _ = _get_today_counts(chat_id)
    update.message.reply_text(
        f"{human_day}\n\n"
        f"Üretilen PDF : {pdf_c}\n"
        f"Üretilen KART PDF : {kart_c}"
    )

# ✅ TÜM GÜNÜN GENEL RAPORU — SADECE ADMIN
def cmd_raporadmin(update: Update, context: CallbackContext):
    if not _require_admin(update):
        return
    # özelden yazılmasını tavsiye et
    try:
        if update.effective_chat and getattr(update.effective_chat, "type", "") != "private":
            update.message.reply_text("Bu komutu bana özelden yaz: /raporadmin")
            return
    except Exception:
        pass
    try:
        text = _build_daily_message(context.bot)
        update.message.reply_text(text)
    except Exception as e:
        log.exception(f"/raporadmin hata: {e}")
        update.message.reply_text("Rapor hazırlanırken bir sorun oluştu.")

# ================== PDF OLUŞTURMA (Genel) ==================
def _save_if_pdf_like(resp) -> str:
    """Sunucudan gelen yanıt PDF ise diske kaydeder, yolunu döner."""
    try:
        ct = (resp.headers.get("Content-Type") or "").lower()
        cd = (resp.headers.get("Content-Disposition") or "").lower()
        content = resp.content or b""
        looks_pdf = (b"%PDF" in content[:10]) or ("application/pdf" in ct) or ("filename=" in cd)
        
        if resp.status_code == 200 and looks_pdf and content:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(content)
            tmp.close()
            return tmp.name
        return ""
    except Exception as e:
        log.exception(f"_save_if_pdf_like hata: {e}")
        return ""

def _send_temp_pdf(update: Update, pdf_path: str, name_up: str, surname_up: str, suffix: str = "") -> bool:
    """
    Geçici PDF dosyasını göndermeyi dener (3 deneme), ardından dosyayı siler.
    Başarı durumunda True döner.
    """
    sent_ok = False
    for attempt in range(1, 4):
        try:
            filename = f"{name_up}_{surname_up}{suffix}.pdf".replace(" ", "_")
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
            break # Başarılı, döngüden çık
        except (NetworkError, TimedOut) as e:
            log.warning(f"send_document{suffix} timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"send_document{suffix} failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break # Başarısız, döngüden çık
    
    # PDF gönderilmiş olsun ya da olmasın, geçici dosyayı sil
    try:
        os.remove(pdf_path)
    except Exception:
        pass
        
    return sent_ok

def _generate_pdf_base(url: str, tc: str, name: str, surname: str, miktar: str, log_ctx: str) -> str:
    """PDF, Burs ve Dip için ortak PDF oluşturma mantığı (API isteği)"""
    data = {"tc": tc, "ad": name, "soyad": surname, "miktar": miktar}
    try:
        # Önce FORM data olarak dene
        r = requests.post(url, data=data, headers=_headers(), timeout=120)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"[{log_ctx} form] PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:300]}")
    except Exception as e:
        log.exception(f"[{log_ctx} form] _generate_pdf_base hata: {e}")
    
    try:
        # Sonra JSON olarak dene
        r2 = requests.post(url, json=data, headers=_headers(), timeout=120)
        path2 = _save_if_pdf_like(r2)
        if path2:
            return path2
        else:
            log.error(f"[{log_ctx} json] PDF alınamadı | status={r2.status_code} ct={(r2.headers.get('Content-Type') or '').lower()} body={r2.text[:300]}")
    except Exception as e:
        log.exception(f"[{log_ctx} json] _generate_pdf_base hata: {e}")
    return ""

def generate_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    return _generate_pdf_base(PDF_URL, tc, name, surname, miktar, "pdf")

def generate_burs_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    return _generate_pdf_base(BURS_PDF_URL, tc, name, surname, miktar, "burs")
    
def generate_dip_pdf(tc: str, name: str, surname: str, miktar: str) -> str:
    return _generate_pdf_base(DIP_PDF_URL, tc, name, surname, miktar, "dip")

def generate_kart_pdf(adsoyad: str, adres: str, ililce: str, tarih: str) -> str:
    """Kart PDF'i /generate2'den ister (farklı data yapısı)"""
    try:
        data = {"adsoyad": adsoyad, "adres": adres, "ililce": ililce, "tarih": tarih}
        r = requests.post(KART_PDF_URL, data=data, headers=_headers(), timeout=90)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"KART PDF alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:200]}")
            return ""
    except Exception as e:
        log.exception(f"generate_kart_pdf hata: {e}")
    return ""

def generate_pdf2_pdf(adsoyad: str, tarih: str) -> str:
    """PDF2'yi (sablon4.pdf) oluşturmak için istek atar"""
    try:
        data = {"adsoyad": adsoyad, "tarih": tarih}
        r = requests.post(PDF2_URL, data=data, headers=_headers(), timeout=90)
        path = _save_if_pdf_like(r)
        if path:
            return path
        else:
            log.error(f"PDF2 alınamadı | status={r.status_code} ct={(r.headers.get('Content-Type') or '').lower()} body={r.text[:200]}")
            return ""
    except Exception as e:
        log.exception(f"generate_pdf2_pdf hata: {e}")
    return ""


# ================== /pdf CONVERSATION ==================
def start_pdf(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    inline = parse_pdf_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ PDF hazırlanıyor")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = _send_temp_pdf(update, pdf_path, name_up, surname_up, "")
        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)
        return ConversationHandler.END

    update.message.reply_text("Müşterinin TC numarasını yaz:")
    return TC

def get_tc(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["tc"] = update.message.text.strip()
    update.message.reply_text("Müşterinin Adını yaz:")
    return NAME

def get_name(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["name"] = update.message.text
    update.message.reply_text("Müşterinin Soyadını yaz:")
    return SURNAME

def get_surname(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["surname"] = update.message.text
    update.message.reply_text("Miktarı yaz (örn: 5.000):")
    return MIKTAR

def get_miktar(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ PDF hazırlanıyor")
    name_up = tr_upper(context.user_data["name"])
    surname_up = tr_upper(context.user_data["surname"])
    pdf_path = generate_pdf(
        context.user_data["tc"],
        name_up,
        surname_up,
        context.user_data["miktar"]
    )
    if not pdf_path:
        update.message.reply_text("❌ PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = _send_temp_pdf(update, pdf_path, name_up, surname_up, "")
    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)
    return ConversationHandler.END


# ================== /kart CONVERSATION ==================
def start_kart(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    inline = parse_kart_inline(update.message.text or "")
    if inline:
        adsoyad, adres, ililce, tarih = inline
        update.message.reply_text("⏳ Kart durumu PDF hazırlanıyor...")
        pdf_path = generate_kart_pdf(adsoyad, adres, ililce, tarih)
        if not pdf_path:
            update.message.reply_text("❌ Kart PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "kart", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        # Kart için _send_temp_pdf kullanamayız, dosya adı farklı
        sent_ok = False
        for attempt in range(1, 4):
            try:
                base = (adsoyad or "KART").strip().replace(" ", "_").upper()
                filename = f"{base}_KART.pdf"
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"kart send timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"kart send failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break
        try:
            os.remove(pdf_path)
        except Exception:
            pass
        
        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)
        return ConversationHandler.END

    update.message.reply_text("Ad Soyad yaz:")
    return K_ADSOYAD

def get_k_adsoyad(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["k_adsoyad"] = update.message.text.strip()
    update.message.reply_text("Adres yaz:")
    return K_ADRES

def get_k_adres(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["k_adres"] = update.message.text.strip()
    update.message.reply_text("İl İlçe yaz:")
    return K_ILILCE

def get_k_ililce(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["k_ililce"] = update.message.text.strip()
    update.message.reply_text("Tarih yaz:")
    return K_TARIH

def get_k_tarih(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["k_tarih"] = update.message.text.strip()
    update.message.reply_text("⏳ Kart durumu PDF hazırlanıyor...")
    pdf_path = generate_kart_pdf(
        context.user_data["k_adsoyad"],
        context.user_data["k_adres"],
        context.user_data["k_ililce"],
        context.user_data["k_tarih"]
    )
    if not pdf_path:
        update.message.reply_text("❌ Kart PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "kart", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            base = (context.user_data.get("k_adsoyad") or "KART").strip().replace(" ", "_").upper()
            filename = f"{base}_KART.pdf"
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"kart send timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"kart send failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break
    try:
        os.remove(pdf_path)
    except Exception:
        pass

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)
    return ConversationHandler.END

# ================== /burs CONVERSATION ==================
def start_burs(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    inline = parse_burs_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ BURS PDF hazırlanıyor")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_burs_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ BURS PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = _send_temp_pdf(update, pdf_path, name_up, surname_up, "_BURS")
        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)
        return ConversationHandler.END

    update.message.reply_text("TC yaz:")
    return B_TC

def get_b_tc(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_tc"] = update.message.text.strip()
    update.message.reply_text("Ad yaz:")
    return B_NAME

def get_b_name(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_name"] = update.message.text
    update.message.reply_text("Soyad yaz:")
    return B_SURNAME

def get_b_surname(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_surname"] = update.message.text
    update.message.reply_text("Miktar yaz (örn: 5.000):")
    return B_MIKTAR

def get_b_miktar(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["b_miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ BURS PDF hazırlanıyor")
    name_up = tr_upper(context.user_data["b_name"])
    surname_up = tr_upper(context.user_data["b_surname"])
    pdf_path = generate_burs_pdf(
        context.user_data["b_tc"],
        name_up,
        surname_up,
        context.user_data["b_miktar"]
    )
    if not pdf_path:
        update.message.reply_text("❌ BURS PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = _send_temp_pdf(update, pdf_path, name_up, surname_up, "_BURS")
    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)
    return ConversationHandler.END

# ================== /dip CONVERSATION ==================
def start_dip(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    inline = parse_dip_inline(update.message.text or "")
    if inline:
        tc_raw, name_raw, surname_raw, miktar_raw = inline
        update.message.reply_text("⏳ DİP PDF hazırlanıyor")
        name_up = tr_upper(name_raw)
        surname_up = tr_upper(surname_raw)
        pdf_path = generate_dip_pdf(tc_raw.strip(), name_up, surname_up, miktar_raw.strip())
        if not pdf_path:
            update.message.reply_text("❌ DİP PDF oluşturulamadı.")
            return ConversationHandler.END

        try:
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = _send_temp_pdf(update, pdf_path, name_up, surname_up, "_DIP")
        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)
        return ConversationHandler.END

    update.message.reply_text("TC yaz:")
    return D_TC

def get_d_tc(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["d_tc"] = update.message.text.strip()
    update.message.reply_text("Ad yaz:")
    return D_NAME

def get_d_name(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["d_name"] = update.message.text
    update.message.reply_text("Soyad yaz:")
    return D_SURNAME

def get_d_surname(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["d_surname"] = update.message.text
    update.message.reply_text("Miktar yaz (örn: 5.000):")
    return D_MIKTAR

def get_d_miktar(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["d_miktar"] = update.message.text.strip()
    update.message.reply_text("⏳ DİP PDF hazırlanıyor")
    name_up = tr_upper(context.user_data["d_name"])
    surname_up = tr_upper(context.user_data["d_surname"])
    pdf_path = generate_dip_pdf(
        context.user_data["d_tc"],
        name_up,
        surname_up,
        context.user_data["d_miktar"]
    )
    if not pdf_path:
        update.message.reply_text("❌ DİP PDF oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = _send_temp_pdf(update, pdf_path, name_up, surname_up, "_DIP")
    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)
    return ConversationHandler.END

# ================== YENİ: /pdf2 CONVERSATION ==================
def start_pdf2(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    inline = parse_pdf2_inline(update.message.text or "")
    if inline:
        adsoyad, tarih = inline
        update.message.reply_text("⏳ PDF2 hazırlanıyor...")
        pdf_path = generate_pdf2_pdf(adsoyad, tarih)
        if not pdf_path:
            update.message.reply_text("❌ PDF2 oluşturulamadı.")
            return ConversationHandler.END

        try:
            # Raporlama için pdf sayacını artırıyorum
            _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
        except Exception:
            pass

        sent_ok = False
        for attempt in range(1, 4):
            try:
                base = (adsoyad or "DOSYA").strip().replace(" ", "_").upper()
                filename = f"{base}_PDF2.pdf"
                with open(pdf_path, "rb") as f:
                    update.message.reply_document(
                        document=InputFile(f, filename=filename),
                        timeout=180
                    )
                sent_ok = True
                break
            except (NetworkError, TimedOut) as e:
                log.warning(f"pdf2 send timeout/network (attempt {attempt}): {e}")
                if attempt == 3:
                    update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
                else:
                    time.sleep(2 * attempt)
            except Exception as e:
                log.exception(f"pdf2 send failed: {e}")
                update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
                break
        try:
            os.remove(pdf_path)
        except Exception:
            pass

        if sent_ok:
            _dec_quota_if_applicable(update.effective_chat.id)
        return ConversationHandler.END

    update.message.reply_text("Ad Soyad yaz:")
    return P2_ADSOYAD

def get_p2_adsoyad(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["p2_adsoyad"] = update.message.text.strip()
    update.message.reply_text("Tarih yaz:")
    return P2_TARIH

def get_p2_tarih(update: Update, context: CallbackContext):
    if not _check_group(update, context):
        return ConversationHandler.END
    context.user_data["p2_tarih"] = update.message.text.strip()
    update.message.reply_text("⏳ PDF2 hazırlanıyor...")
    
    pdf_path = generate_pdf2_pdf(
        context.user_data["p2_adsoyad"],
        context.user_data["p2_tarih"]
    )
    if not pdf_path:
        update.message.reply_text("❌ PDF2 oluşturulamadı.")
        return ConversationHandler.END

    try:
        _inc_report(update.effective_chat.id, "pdf", getattr(update.effective_chat, "title", None))
    except Exception:
        pass

    sent_ok = False
    for attempt in range(1, 4):
        try:
            base = (context.user_data.get("p2_adsoyad") or "DOSYA").strip().replace(" ", "_").upper()
            filename = f"{base}_PDF2.pdf"
            with open(pdf_path, "rb") as f:
                update.message.reply_document(
                    document=InputFile(f, filename=filename),
                    timeout=180
                )
            sent_ok = True
            break
        except (NetworkError, TimedOut) as e:
            log.warning(f"pdf2 send timeout/network (attempt {attempt}): {e}")
            if attempt == 3:
                update.message.reply_text("⚠️ Yükleme zaman aşımına uğradı. Tekrar dene.")
            else:
                time.sleep(2 * attempt)
        except Exception as e:
            log.exception(f"pdf2 send failed: {e}")
            update.message.reply_text("❌ Dosya gönderirken hata oluştu.")
            break
    try:
        os.remove(pdf_path)
    except Exception:
        pass

    if sent_ok:
        _dec_quota_if_applicable(update.effective_chat.id)
    return ConversationHandler.END


# ================== CANCEL (Tüm konuşmalar için) ==================
def cmd_cancel(update: Update, context: CallbackContext):
    update.message.reply_text("İptal edildi.")
    return ConversationHandler.END

# ================== GÜNLÜK DM RAPORU (Scheduler) ==================
def _build_daily_message(bot: "telegram.Bot") -> str:
    rep = _ensure_today_report()
    counts = rep.get("counts", {})
    if not counts:
        return (
            "ÜRETİLEN TOPLAM PDF  : 0\n"
            "ÜRETİLEN BURS ve PDF : 0\n"
            "ÜRETİLEN KART PDF : 0\n\n"
            "Bugün üretim yok."
        )

    total_pdf = 0
    total_kart = 0
    lines = []
    
    # Raporu ID'ye göre değil, başlığa göre sırala (daha okunaklı)
    sorted_items = []
    for chat_id_str, node in counts.items():
        title = GROUP_TITLES.get(chat_id_str, f"Grup {chat_id_str}")
        sorted_items.append((title, chat_id_str, node))
    
    # Başlığa göre alfabetik sırala
    sorted_items.sort(key=lambda x: x[0].lower())

    for title, chat_id_str, node in sorted_items:
        pdf_c = int(node.get("pdf", 0))
        kart_c = int(node.get("kart", 0))
        total_pdf += pdf_c
        total_kart += kart_c

        # Başlık hala ID ise, çekmeyi dene (sonradan eklenen gruplar için)
        if title == f"Grup {chat_id_str}":
            try:
                ch = bot.get_chat(int(chat_id_str))
                title = getattr(ch, "title", None) or f"Grup {chat_id_str}"
                GROUP_TITLES[chat_id_str] = title # Hafızaya al
            except Exception:
                title = f"Grup {chat_id_str} (Erişim Yok)"

        lines.append(f"- {title} ({chat_id_str}) → PDF: {pdf_c} | KART: {kart_c}")

    # Başlıkları kaydet (yeni çekilenler varsa)
    _save_titles(GROUP_TITLES)
    
    msg = (
        f"ÜRETİLEN TOPLAM PDF  : {total_pdf}\n"
        f"ÜRETİLEN BURS ve PDF : {total_pdf}\n"
        f"ÜRETİLEN KART PDF : {total_kart}\n\n"
        + "\n".join(lines)
    )
    return msg

def send_daily_dm(bot: "telegram.Bot"):
    try:
        text = _build_daily_message(bot)
        bot.send_message(chat_id=ADMIN_ID, text=text)
    except Exception as e:
        log.exception(f"Günlük DM raporu gönderilemedi: {e}")

# ================== ERROR HANDLER ==================
def on_error(update: object, context: CallbackContext):
    log.exception("Unhandled error", exc_info=context.error)

# ================== MAIN ==================
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env'de yok!")

    request_kwargs = {
        "con_pool_size": 8,
        "connect_timeout": 30,
        "read_timeout": 180
    }

    updater = Updater(BOT_TOKEN, use_context=True, request_kwargs=request_kwargs)

    try:
        updater.bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook uyarı: {e}")

    dp = updater.dispatcher
    dp.add_error_handler(on_error)

    # --- Konuşma Yöneticileri ---
    
    # /pdf
    conv = ConversationHandler(
        entry_points=[CommandHandler("pdf", start_pdf)],
        states={
            TC: [MessageHandler(Filters.text & ~Filters.command, get_tc)],
            NAME: [MessageHandler(Filters.text & ~Filters.command, get_name)],
            SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_surname)],
            MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    # /kart
    conv_kart = ConversationHandler(
        entry_points=[CommandHandler("kart", start_kart)],
        states={
            K_ADSOYAD: [MessageHandler(Filters.text & ~Filters.command, get_k_adsoyad)],
            K_ADRES: [MessageHandler(Filters.text & ~Filters.command, get_k_adres)],
            K_ILILCE: [MessageHandler(Filters.text & ~Filters.command, get_k_ililce)],
            K_TARIH: [MessageHandler(Filters.text & ~Filters.command, get_k_tarih)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    # /burs
    conv_burs = ConversationHandler(
        entry_points=[CommandHandler("burs", start_burs)],
        states={
            B_TC: [MessageHandler(Filters.text & ~Filters.command, get_b_tc)],
            B_NAME: [MessageHandler(Filters.text & ~Filters.command, get_b_name)],
            B_SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_b_surname)],
            B_MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_b_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )
    
    # /dip
    conv_dip = ConversationHandler(
        entry_points=[CommandHandler("diploma", start_dip)],
        states={
            D_TC: [MessageHandler(Filters.text & ~Filters.command, get_d_tc)],
            D_NAME: [MessageHandler(Filters.text & ~Filters.command, get_d_name)],
            D_SURNAME: [MessageHandler(Filters.text & ~Filters.command, get_d_surname)],
            D_MIKTAR: [MessageHandler(Filters.text & ~Filters.command, get_d_miktar)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    # /pdf2 (YENİ)
    conv_pdf2 = ConversationHandler(
        entry_points=[CommandHandler("pdf2", start_pdf2)],
        states={
            P2_ADSOYAD: [MessageHandler(Filters.text & ~Filters.command, get_p2_adsoyad)],
            P2_TARIH: [MessageHandler(Filters.text & ~Filters.command, get_p2_tarih)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        conversation_timeout=180,
        allow_reentry=True
    )

    # --- Komutları Ekleme ---

    # Admin-only komutlar
    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("whereami", cmd_whereami))
    dp.add_handler(CommandHandler("ping", cmd_ping)) # ✅ YENİ TEST KOMUTU EKLENDİ
    dp.add_handler(CommandHandler("yetkiver", cmd_yetkiver, pass_args=True))
    dp.add_handler(CommandHandler("hakver", cmd_hakver))
    dp.add_handler(CommandHandler("kalanhak", cmd_hakdurum))
    dp.add_handler(CommandHandler("bitir", cmd_bitir))
    dp.add_handler(CommandHandler("rapor", cmd_rapor))
    dp.add_handler(CommandHandler("raporadmin", cmd_raporadmin))
    dp.add_handler(CommandHandler("kontenjan", cmd_kontenjan))
    dp.add_handler(CommandHandler("ekle", cmd_kontenjan)) # Takma ad
    
    # Normal akışlar
    dp.add_handler(conv)
    dp.add_handler(conv_kart)
    dp.add_handler(conv_burs)
    dp.add_handler(conv_dip)
    dp.add_handler(conv_pdf2) # ✅ YENİ eklendi

    # ⏰ Günlük 23:55'te ADMIN_ID'ye DM rapor
    scheduler = BackgroundScheduler(timezone=TR_TZ)
    scheduler.add_job(
        send_daily_dm,
        CronTrigger(hour=23, minute=55, timezone=TR_TZ),
        args=[updater.bot],
        id="daily_dm_2355",
        replace_existing=True,
    )
    scheduler.start()

    # ✅ Render'in port bekleyisini kirmak icin sahte sunucuyu ayri bir is parcaciginda calistir
    threading.Thread(target=run_dummy_server, daemon=True).start()

    log.info("Bot açılıyor...")
    updater.start_polling(drop_pending_updates=True)
    updater.idle()

if __name__ == "__main__":
    main()
