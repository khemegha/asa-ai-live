#!/usr/bin/env python3
"""ASA AI - inscriptions a la demo live de l'AI SOC Analyst.

Service autonome (bibliotheque standard Python uniquement) :
  - sert la landing page et ses fichiers statiques ;
  - enregistre les inscriptions dans SQLite, 100 places par session ;
  - envoie la confirmation a l'inscrit (avec le visuel) et la notification a contact@asa-ai.fr.

Configuration : fichier live.env passe en argument (--env <chemin>) ou variables d'environnement.
Machine cible : poste Windows ASA-AI-Admin (tache planifiee silencieuse, pythonw.exe).
"""
import json
import os
import re
import smtplib
import sqlite3
import ssl
import threading
import time
import traceback
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import sys


def _load_env_file():
    """Charge le fichier de configuration indique par --env <chemin> ou LIVE_ENV_FILE."""
    path = os.environ.get("LIVE_ENV_FILE")
    if "--env" in sys.argv:
        path = sys.argv[sys.argv.index("--env") + 1]
    if not path:
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"')


_load_env_file()

import sessions  # noqa: E402
from texts import T, long_date  # noqa: E402

APP_DIR = Path(__file__).resolve().parent
STATIC = APP_DIR / "static"

CFG = {
    "HOST": os.environ.get("LIVE_HOST", "127.0.0.1"),
    "PORT": int(os.environ.get("LIVE_PORT", "8097")),
    "DB": os.environ.get("LIVE_DB", str(Path(__file__).resolve().parent.parent / "data" / "registrations.db")),
    "CAPACITY": int(os.environ.get("LIVE_CAPACITY", "100")),
    "DURATION": int(os.environ.get("LIVE_DURATION_MIN", "45")),
    "JOIN_URL": os.environ.get("LIVE_JOIN_URL", "https://meet.jit.si/ASA-AI-SOC-Analyst-Live"),
    "PUBLIC_URL": os.environ.get("LIVE_PUBLIC_URL", "https://khemegha.github.io/asa-ai-live/"),
    "ORIGINS": os.environ.get("LIVE_ALLOWED_ORIGINS", "https://khemegha.github.io"),
    "NOTIFY_TO": os.environ.get("LIVE_NOTIFY_TO", "contact@asa-ai.fr"),
    "SMTP_HOST": os.environ.get("SMTP_HOST", "mail.asa-ai.fr"),
    "SMTP_PORT": int(os.environ.get("SMTP_PORT", "587")),
    "SMTP_USER": os.environ.get("SMTP_USER", "contact@asa-ai.fr"),
    "SMTP_PASS": (Path(os.environ["SMTP_PASS_FILE"]).read_text(encoding="utf-8").rstrip("\r\n")
                  if os.environ.get("SMTP_PASS_FILE") else os.environ.get("SMTP_PASS", "")),
    "SMTP_FROM": os.environ.get("SMTP_FROM", "contact@asa-ai.fr"),
    "SMTP_FROM_NAME": os.environ.get("SMTP_FROM_NAME", "ASA AI"),
    "SMTP_CAFILE": os.environ.get("SMTP_CAFILE", ""),
    "SMTP_VERIFY": os.environ.get("SMTP_VERIFY", "1") == "1",
    "SMTP_SECURITY": os.environ.get("SMTP_SECURITY", "starttls"),  # starttls | ssl | none (tests locaux)
}

ALLOWED_ORIGINS = {o.strip() for o in CFG["ORIGINS"].split(",") if o.strip()}
EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$")
DB_LOCK = threading.Lock()
RATE = {}
LOG_PREFIX = "[asa-ai-live]"


LOG_FILE = os.environ.get("LIVE_LOG", "")
LOG_LOCK = threading.Lock()


def log(msg):
    line = f"{LOG_PREFIX} {datetime.now().isoformat(timespec='seconds')} {msg}"
    if LOG_FILE:  # pythonw.exe n'a pas de console : journal dans un fichier (tourne a 5 Mo)
        with LOG_LOCK:
            try:
                if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 5_000_000:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
                with open(LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
    elif sys.stdout:
        print(line, flush=True)


# ---------------------------------------------------------------- base de donnees
def db():
    con = sqlite3.connect(CFG["DB"], timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    Path(CFG["DB"]).parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_date TEXT NOT NULL,
            first_name TEXT NOT NULL, last_name TEXT NOT NULL,
            email TEXT NOT NULL, company TEXT NOT NULL, job_title TEXT NOT NULL,
            lang TEXT NOT NULL DEFAULT 'en',
            created_at TEXT NOT NULL, ip TEXT,
            confirm_sent INTEGER NOT NULL DEFAULT 0,
            notify_sent INTEGER NOT NULL DEFAULT 0,
            mail_attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            UNIQUE(session_date, email)
        );
        CREATE INDEX IF NOT EXISTS idx_session ON registrations(session_date);
        """)


def counts():
    with db() as con:
        rows = con.execute("SELECT session_date, COUNT(*) n FROM registrations GROUP BY session_date").fetchall()
    return {r["session_date"]: r["n"] for r in rows}


def session_list(n=8):
    c = counts()
    cap = CFG["CAPACITY"]
    out = []
    for d in sessions.upcoming_sessions(count=n):
        k = d.isoformat()
        taken = c.get(k, 0)
        out.append({"date": k, "taken": taken, "left": max(cap - taken, 0), "full": taken >= cap})
    return out


def register(data, ip):
    """Retourne (status_http, dict). Attribue la session demandee si elle a de la place."""
    fields = {k: str(data.get(k, "")).strip()[:120] for k in ("first", "last", "email", "company", "role", "session", "lang")}
    lang = fields["lang"] if fields["lang"] in T else "en"
    if data.get("website"):  # champ piege pour robots
        return 200, {"ok": True, "session": fields["session"], "lang": lang}
    if not all(fields[k] for k in ("first", "last", "email", "company", "role", "session")) \
            or not EMAIL_RE.match(fields["email"]) or data.get("consent") is not True:
        return 400, {"ok": False, "error": "invalid"}
    email = fields["email"].lower()
    valid = {d.isoformat() for d in sessions.upcoming_sessions(count=30)}
    if fields["session"] not in valid:
        return 409, {"ok": False, "error": "full", "sessions": session_list()}
    with DB_LOCK:
        con = db()
        try:
            con.execute("BEGIN IMMEDIATE")
            taken = con.execute("SELECT COUNT(*) FROM registrations WHERE session_date=?", (fields["session"],)).fetchone()[0]
            if taken >= CFG["CAPACITY"]:
                con.rollback()
                return 409, {"ok": False, "error": "full", "sessions": session_list()}
            dup = con.execute("SELECT 1 FROM registrations WHERE session_date=? AND email=?", (fields["session"], email)).fetchone()
            if dup:
                con.rollback()
                return 409, {"ok": False, "error": "dup"}
            con.execute(
                "INSERT INTO registrations(session_date,first_name,last_name,email,company,job_title,lang,created_at,ip) VALUES(?,?,?,?,?,?,?,?,?)",
                (fields["session"], fields["first"], fields["last"], email, fields["company"], fields["role"], lang,
                 datetime.now(sessions.PARIS).isoformat(timespec="seconds"), ip))
            con.commit()
        finally:
            con.close()
    log(f"inscription {email} -> {fields['session']}")
    MAIL_EVENT.set()
    return 200, {"ok": True, "session": fields["session"], "lang": lang, "sessions": session_list()}


def rate_ok(ip):
    now = time.time()
    hits = [t for t in RATE.get(ip, []) if now - t < 600]
    if len(hits) >= 8:
        RATE[ip] = hits
        return False
    hits.append(now)
    RATE[ip] = hits
    return True


# ---------------------------------------------------------------- e-mails
MAIL_EVENT = threading.Event()
IMAGE = STATIC / "asa-ai-soc-analyst-live.jpg"


def smtp_send(msg):
    ctx = ssl.create_default_context(cafile=CFG["SMTP_CAFILE"] or None)
    if CFG["SMTP_CAFILE"]:
        # certificat du serveur Mailcow epingle (CA interne) : on fait confiance a ce certificat precis
        ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
        ctx.check_hostname = False
    if not CFG["SMTP_VERIFY"]:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    port = CFG["SMTP_PORT"]
    if CFG["SMTP_SECURITY"] == "ssl":
        s = smtplib.SMTP_SSL(CFG["SMTP_HOST"], port, context=ctx, timeout=30)
    else:
        s = smtplib.SMTP(CFG["SMTP_HOST"], port, timeout=30)
        s.ehlo()
        if CFG["SMTP_SECURITY"] == "starttls":
            s.starttls(context=ctx)
            s.ehlo()
    try:
        if CFG["SMTP_PASS"]:
            s.login(CFG["SMTP_USER"], CFG["SMTP_PASS"])
        s.send_message(msg)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def confirmation_mail(r):
    t = T[r["lang"]]
    d = date.fromisoformat(r["session_date"])
    when = f"{long_date(d, r['lang'])} · {t['time_label']}"
    msg = EmailMessage()
    msg["Subject"] = t["mail_subject"].format(date=long_date(d, r["lang"]))
    msg["From"] = formataddr((CFG["SMTP_FROM_NAME"], CFG["SMTP_FROM"]))
    msg["To"] = r["email"]
    msg["Reply-To"] = CFG["NOTIFY_TO"]
    join = CFG["JOIN_URL"]
    dur = t["mail_dur_v"].format(dur=CFG["DURATION"])
    text = "\n".join([
        t["mail_hello"].format(first=r["first_name"]), "", t["mail_intro"], "",
        f"{t['mail_when']}: {when}", f"{t['mail_dur']}: {dur}", f"{t['mail_where']}: {join}", t["mail_join_note"], "",
        t["mail_body"], "", t["mail_bring"], "", t["mail_cancel"], "", t["mail_bye"], t["mail_team"], "contact@asa-ai.fr · www.asa-ai.fr",
    ])
    msg.set_content(text)
    cid = make_msgid(domain="asa-ai.fr")
    e = escape
    row = lambda k, v: (f'<tr><td style="padding:8px 0;color:#5C5C72;font:500 13px Arial,sans-serif;width:150px;vertical-align:top">{e(k)}</td>'
                        f'<td style="padding:8px 0;color:#1A1A2E;font:600 15px Arial,sans-serif">{v}</td></tr>')
    html = f"""<!doctype html><html lang="{t['html_lang']}"><body style="margin:0;background:#F2F2F2">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F2F2F2"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#FFFFFF;border:1px solid #D3D3DE;border-radius:10px;overflow:hidden">
<tr><td style="height:6px;background:#D6247A;background-image:linear-gradient(90deg,#6B2FA0,#D6247A)"></td></tr>
<tr><td style="padding:28px 32px 8px;font:400 15px/1.55 Arial,sans-serif;color:#1A1A2E">
<p style="margin:0 0 14px">{e(t['mail_hello'].format(first=r['first_name']))}</p>
<p style="margin:0 0 18px;font-weight:700;font-size:18px">{e(t['mail_intro'])}</p>
<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-top:1px solid #D3D3DE;border-bottom:1px solid #D3D3DE;margin-bottom:18px">
{row(t['mail_when'], e(when))}{row(t['mail_dur'], e(dur))}
{row(t['mail_where'], f'<a href="{e(join)}" style="color:#D6247A">{e(join)}</a><br><span style="font-weight:400;color:#5C5C72;font-size:13px">{e(t["mail_join_note"])}</span>')}
</table></td></tr>
<tr><td style="padding:0 32px"><img src="cid:{cid[1:-1]}" width="536" alt="ASA AI · AI SOC Analyst" style="display:block;width:100%;height:auto;border:1px solid #D3D3DE;border-radius:8px"></td></tr>
<tr><td style="padding:18px 32px 28px;font:400 15px/1.55 Arial,sans-serif;color:#1A1A2E">
<p style="margin:0 0 12px">{e(t['mail_body'])}</p>
<p style="margin:0 0 12px">{e(t['mail_bring'])}</p>
<p style="margin:0 0 18px;color:#5C5C72;font-size:13px">{e(t['mail_cancel'])}</p>
<p style="margin:0">{e(t['mail_bye'])}<br><b>{e(t['mail_team'])}</b><br><span style="color:#5C5C72;font-size:13px">contact@asa-ai.fr · <a href="https://www.asa-ai.fr" style="color:#6B2FA0">www.asa-ai.fr</a></span></p>
</td></tr></table></td></tr></table></body></html>"""
    msg.add_alternative(html, subtype="html")
    msg.get_payload()[1].add_related(IMAGE.read_bytes(), "image", "jpeg", cid=cid, filename="asa-ai-soc-analyst-live.jpg")
    return msg


def notification_mail(r, taken):
    d = date.fromisoformat(r["session_date"])
    msg = EmailMessage()
    msg["Subject"] = f"[Inscription live] {r['first_name']} {r['last_name']} ({r['company']}) — {long_date(d, 'fr')} — {taken}/{CFG['CAPACITY']}"
    msg["From"] = formataddr((CFG["SMTP_FROM_NAME"], CFG["SMTP_FROM"]))
    msg["To"] = CFG["NOTIFY_TO"]
    msg["Reply-To"] = r["email"]
    msg.set_content("\n".join([
        "Nouvelle inscription à la démo live AI SOC Analyst.", "",
        f"Session    : {long_date(d, 'fr')} à 15h00",
        f"Nom        : {r['first_name']} {r['last_name']}",
        f"E-mail     : {r['email']}",
        f"Entreprise : {r['company']}",
        f"Fonction   : {r['job_title']}",
        f"Langue     : {r['lang']}",
        f"Inscrit le : {r['created_at']}", "",
        f"Inscrits sur cette session : {taken} / {CFG['CAPACITY']}",
        f"Page : {CFG['PUBLIC_URL']}",
    ]))
    return msg


def mail_worker():
    while True:
        MAIL_EVENT.wait(timeout=120)
        MAIL_EVENT.clear()
        try:
            with db() as con:
                rows = con.execute("SELECT * FROM registrations WHERE (confirm_sent=0 OR notify_sent=0) AND mail_attempts<100 ORDER BY id").fetchall()
            for r in rows:
                err = None
                try:
                    if not r["confirm_sent"]:
                        smtp_send(confirmation_mail(r))
                        with db() as con:
                            con.execute("UPDATE registrations SET confirm_sent=1 WHERE id=?", (r["id"],))
                    if not r["notify_sent"]:
                        with db() as con:
                            taken = con.execute("SELECT COUNT(*) FROM registrations WHERE session_date=?", (r["session_date"],)).fetchone()[0]
                        smtp_send(notification_mail(r, taken))
                        with db() as con:
                            con.execute("UPDATE registrations SET notify_sent=1 WHERE id=?", (r["id"],))
                    log(f"e-mails envoyes pour {r['email']}")
                except Exception as ex:  # nouvel essai toutes les 2 minutes (100 essais max, environ 3 h)
                    err = f"{type(ex).__name__}: {ex}"
                    log(f"ECHEC e-mail {r['email']} : {err}")
                    with db() as con:
                        con.execute("UPDATE registrations SET mail_attempts=mail_attempts+1, last_error=? WHERE id=?", (err[:500], r["id"]))
        except Exception:
            log("erreur worker : " + traceback.format_exc())


# ---------------------------------------------------------------- HTTP
TYPES = {".woff2": "font/woff2", ".jpg": "image/jpeg", ".png": "image/png", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    server_version = "asa-ai-live"

    def log_message(self, fmt, *args):
        pass

    def client_ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def path_rel(self):
        p = urlparse(self.path).path
        if p.startswith("/live-api"):
            p = p[len("/live-api"):]
        return p or "/"

    def cors(self):
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.cors()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self.path_rel()
        if p == "/api/sessions":
            return self.send(200, json.dumps({"sessions": session_list(), "capacity": CFG["CAPACITY"], "duration": CFG["DURATION"]}), "application/json")
        if p == "/health":
            return self.send(200, "ok", "text/plain")
        if p.startswith("/static/"):
            f = (STATIC / p[len("/static/"):]).resolve()
            if STATIC in f.parents and f.is_file():
                return self.send(200, f.read_bytes(), TYPES.get(f.suffix, "application/octet-stream"), "public, max-age=86400")
        self.send(404, "Not found", "text/plain")

    def do_POST(self):
        if self.path_rel() != "/api/register":
            return self.send(404, "Not found", "text/plain")
        ip = self.client_ip()
        if not rate_ok(ip):
            return self.send(429, json.dumps({"ok": False, "error": "rate"}), "application/json")
        try:
            n = min(int(self.headers.get("Content-Length", "0")), 8192)
            data = json.loads(self.rfile.read(n) or b"{}")
            code, out = register(data, ip)
        except Exception:
            log("erreur inscription : " + traceback.format_exc())
            code, out = 500, {"ok": False, "error": "generic"}
        self.send(code, json.dumps(out), "application/json")


def main():
    init_db()
    threading.Thread(target=mail_worker, daemon=True).start()
    MAIL_EVENT.set()
    srv = ThreadingHTTPServer((CFG["HOST"], CFG["PORT"]), Handler)
    log(f"en ecoute sur {CFG['HOST']}:{CFG['PORT']} (base {CFG['DB']})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
