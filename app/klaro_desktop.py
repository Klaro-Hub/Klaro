# -*- coding: utf-8 -*-
"""
KLARO DESKTOP - Biblioteca de vídeo local (sin cuentas, sin login, sin internet)
================================================================================
- Un solo archivo. Solo necesita Python 3.8+ (sin instalar nada) y Edge o Chrome (ya vienen en Windows).
- Se abre en su propia ventana de aplicación (WebView2 incrustado, si "pywebview" está instalado).
  Si no, usa Edge/Chrome en modo "app" y, como último recurso, tu navegador.
- Para crear el instalador de Windows (Klaro Launcher): ejecuta build_installer.bat.

Funciones: añadir vídeos (varios a la vez, arrastrar y soltar), miniaturas automáticas o a tu gusto,
buscador, orden (recientes, antiguos, título, más vistos, más pesados, visto recientemente), etiquetas,
favoritos, ver más tarde, colecciones, reproductor con salto de posición, velocidad, repetir,
atajos de teclado, subtítulos (.vtt/.srt), marcadores/capítulos, continuar donde lo dejaste,
editar, borrar, guardar copia, estadísticas.

Los datos se guardan en la carpeta de datos de tu usuario (se muestra en Configuración):
  Windows: %APPDATA%\\Klaro      macOS: ~/Library/Application Support/Klaro      Linux: ~/.local/share/Klaro
Si algo falla, el motivo queda escrito en el archivo error.log de esa carpeta.
"""
import os, sys, re, json, csv, io, glob, hashlib, hmac, sqlite3, secrets, time, threading, webbrowser, subprocess, traceback, shutil, tempfile, urllib.request, urllib.error
from contextlib import closing
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

# ------------------------------------------------------------------ configuración
APP_NAME = "Klaro"
VERSION = "1.2.0"
REPO = "Klaro-Hub/Klaro"  # donde se publican las versiones (GitHub Releases)
DOWNLOAD_URL = "https://github.com/%s/releases/latest/download/KlaroLauncherSetup.exe" % REPO
MAX_UPLOAD = 50 * 1024 ** 3  # 50 GB por archivo
ALLOWED = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
           ".ogv": "video/ogg", ".mov": "video/quicktime"}
SORTS = {"recent": "created DESC", "old": "created ASC", "title": "title COLLATE NOCASE ASC",
         "views": "views DESC, created DESC", "size": "size DESC", "played": "played DESC, created DESC"}


def data_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


DATA = os.environ.get("KLARO_DATA") or data_dir()
VIDEOS = os.path.join(DATA, "videos")
THUMBS = os.path.join(DATA, "thumbs")
SUBS = os.path.join(DATA, "subs")
DB_PATH = os.path.join(DATA, "klaro.db")
LOGO = os.path.join(DATA, "logo.png")
PORT_IN_USE = 0
for d in (DATA, VIDEOS, THUMBS, SUBS):
    os.makedirs(d, exist_ok=True)


# ------------------------------------------------------------------ base de datos
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS videos(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            file TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
            views INTEGER NOT NULL DEFAULT 0, fav INTEGER NOT NULL DEFAULT 0, later INTEGER NOT NULL DEFAULT 0,
            has_thumb INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS colls(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS coll_items(
            coll_id INTEGER NOT NULL REFERENCES colls(id) ON DELETE CASCADE,
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE, PRIMARY KEY(coll_id,video_id));
        CREATE TABLE IF NOT EXISTS vviews(
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE, day TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(video_id,day));
        CREATE TABLE IF NOT EXISTS markers(
            id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            t REAL NOT NULL, label TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS subs(
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE, lang TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '', PRIMARY KEY(video_id,lang));
        CREATE TABLE IF NOT EXISTS ctas(
            id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            t_start REAL NOT NULL, t_end REAL NOT NULL, text TEXT NOT NULL, label TEXT NOT NULL, url TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS watch(
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE, day TEXT NOT NULL,
            seconds REAL NOT NULL DEFAULT 0, PRIMARY KEY(video_id,day));
        CREATE INDEX IF NOT EXISTS idx_videos_created ON videos(created DESC);
        """)
        cols = [r["name"] for r in c.execute("PRAGMA table_info(videos)")]  # migración de versiones anteriores
        for name, ddl in (("tags", "TEXT NOT NULL DEFAULT ''"), ("pos", "REAL NOT NULL DEFAULT 0"),
                          ("dur", "REAL NOT NULL DEFAULT 0"), ("played", "INTEGER NOT NULL DEFAULT 0"),
                          ("has_subs", "INTEGER NOT NULL DEFAULT 0"), ("hidden", "INTEGER NOT NULL DEFAULT 0"),
                          ("pw_salt", "TEXT NOT NULL DEFAULT ''"), ("pw_hash", "TEXT NOT NULL DEFAULT ''"),
                          ("w", "INTEGER NOT NULL DEFAULT 0"), ("h", "INTEGER NOT NULL DEFAULT 0")):
            if name not in cols:
                c.execute("ALTER TABLE videos ADD COLUMN %s %s" % (name, ddl))
        mcols = [r["name"] for r in c.execute("PRAGMA table_info(markers)")]
        for name, ddl in (("kind", "TEXT NOT NULL DEFAULT 'chapter'"), ("done", "INTEGER NOT NULL DEFAULT 0")):
            if name not in mcols:
                c.execute("ALTER TABLE markers ADD COLUMN %s %s" % (name, ddl))
        for r in c.execute("SELECT id FROM videos WHERE has_subs=1").fetchall():  # subtítulos de la versión 1.0
            old = os.path.join(SUBS, r["id"] + ".vtt")
            if os.path.isfile(old):
                os.replace(old, os.path.join(SUBS, r["id"] + ".es.vtt"))
                c.execute("INSERT OR IGNORE INTO subs(video_id,lang,label) VALUES(?,?,?)", (r["id"], "es", "Español"))
            c.execute("UPDATE videos SET has_subs=0 WHERE id=?", (r["id"],))
        c.commit()


def open_path(path):
    try:
        os.startfile(path)  # Windows
    except AttributeError:
        webbrowser.open("file://" + path)


def jv(r):
    return {"id": r["id"], "title": r["title"], "description": r["description"], "size": r["size"],
            "views": r["views"], "fav": bool(r["fav"]), "later": bool(r["later"]),
            "thumb": bool(r["has_thumb"]), "created": r["created"], "pos": r["pos"], "dur": r["dur"],
            "tags": [t for t in r["tags"].split(",") if t], "hidden": bool(r["hidden"]),
            "locked": bool(r["pw_hash"]), "w": r["w"], "h": r["h"]}


def clean_tags(txt):
    out = []
    for t in str(txt).split(","):
        t = re.sub(r"[%_\\]", "", t.strip().lower())[:30]
        if t and t not in out:
            out.append(t)
    return ",".join(out[:20])


def to_vtt(raw):
    try:
        t = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        t = raw.decode("latin-1")
    t = t.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not t.startswith("WEBVTT"):
        t = "WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d{3})", r"\1.\2", t)
    return t + "\n"


def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000).hex()


UNLOCKED = {}  # token de sesión -> vídeos con contraseña ya desbloqueados (solo en memoria)
UPDATE = {}    # se rellena si hay una versión nueva publicada
SETTINGS_DEFAULTS = {"accent": "#2ee6a6", "logo_on": "0", "logo_pos": "br", "logo_opacity": "80", "controls": "1",
                     "autoplay": "0", "loop": "0", "speed": "1", "updates": "1", "updates_auto": "1", "prompter_text": ""}


def get_settings(c):
    d = dict(SETTINGS_DEFAULTS)
    d.update({r["key"]: r["value"] for r in c.execute("SELECT key,value FROM settings")})
    d["logo"] = os.path.isfile(LOGO)
    return d


def vtuple(txt):
    return tuple(int(x) for x in re.findall(r"\d+", txt)[:3])


RELEASES_BASE = "https://github.com/%s/releases" % REPO
API_LATEST = "https://api.github.com/repos/%s/releases/latest" % REPO
UA = {"User-Agent": "Klaro/" + VERSION}
UPD = {"state": "idle", "done": 0, "total": 0, "error": ""}  # idle | downloading | installing | error


def can_install():
    """La actualización automática solo funciona en la app instalada de Windows (no al ejecutar el .py)."""
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def latest_tag():
    """Última versión publicada: primero la API de GitHub; si falla (red, límites), la redirección de /releases/latest."""
    try:
        req = urllib.request.Request(API_LATEST, headers=dict(UA, Accept="application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=6) as r:
            tag = json.load(r).get("tag_name", "")
        if tag:
            return tag
    except Exception:
        pass
    try:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            urllib.request.build_opener(NoRedirect).open(urllib.request.Request(RELEASES_BASE + "/latest", headers=UA), timeout=6)
        except urllib.error.HTTPError as e:
            m = re.search(r"/tag/([^/?#]+)$", e.headers.get("Location", ""))
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


def check_update():
    """Único uso de internet: preguntar cuál es la última versión publicada."""
    tag = latest_tag()
    if re.fullmatch(r"[vV]?\d+(\.\d+){0,3}", tag or "") and vtuple(tag) > vtuple(VERSION):
        UPDATE["info"] = {"version": tag.lstrip("vV"), "tag": tag}
    else:
        UPDATE.pop("info", None)


def update_loop():
    while True:
        try:
            with closing(db()) as c:
                if get_settings(c).get("updates") == "1":
                    check_update()
        except Exception:
            pass
        time.sleep(6 * 3600)


def launch_installer(path):
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], creationflags=flags, close_fds=True)


def exit_soon():
    threading.Timer(1.5, lambda: os._exit(0)).start()


def run_update():
    """Descarga el instalador de la versión nueva, comprueba su huella SHA-256 y lo ejecuta en silencio."""
    try:
        info = UPDATE.get("info")
        if not info:
            raise RuntimeError("No hay ninguna versión nueva.")
        UPD.update(state="downloading", done=0, total=0, error="")
        base = "%s/download/%s" % (RELEASES_BASE, info["tag"])
        try:
            with urllib.request.urlopen(urllib.request.Request(base + "/KlaroLauncherSetup.exe.sha256", headers=UA), timeout=20) as r:
                m = re.search(r"\b[0-9a-fA-F]{64}\b", r.read(4096).decode("ascii", "ignore"))
        except Exception:
            m = None
        if not m:
            raise RuntimeError("La versión nueva no incluye su huella de seguridad (.sha256), así que no se instala sola. Descárgala desde la web.")
        want = m.group(0).lower()
        path = os.path.join(tempfile.gettempdir(), "KlaroLauncherSetup-%s.exe" % info["version"])
        h = hashlib.sha256()
        with urllib.request.urlopen(urllib.request.Request(base + "/KlaroLauncherSetup.exe", headers=UA), timeout=30) as r, open(path, "wb") as out:
            UPD["total"] = int(r.headers.get("Content-Length") or 0)
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                h.update(chunk)
                UPD["done"] += len(chunk)
        if h.hexdigest() != want:
            os.remove(path)
            raise RuntimeError("La descarga llegó dañada (la huella no coincide). Inténtalo de nuevo más tarde.")
        UPD["state"] = "installing"
        launch_installer(path)
        exit_soon()
    except Exception as e:  # noqa
        UPD.update(state="error", error=str(e) or "Error desconocido")


# ------------------------------------------------------------------ servidor HTTP local
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = APP_NAME + "/Desktop"

    def log_message(self, *a):
        pass

    def _headers(self, status, ctype, length, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def send_bytes(self, data, ctype, status=200, extra=None):
        self._headers(status, ctype, len(data), extra)
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json; charset=utf-8", status)

    def err(self, status, msg):
        self.send_json({"error": msg}, status)

    def body(self, limit=1_000_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Cuerpo demasiado grande")
        return self.rfile.read(n) if n else b""

    def json_body(self):
        try:
            d = json.loads(self.body() or b"{}")
            return d if isinstance(d, dict) else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    def guard(self, mutating):
        """Sin login, así que se protege la API: solo acepta el host local y exige una cabecera propia
        en las operaciones que modifican datos (otras webs abiertas en tu navegador no pueden usarla)."""
        host = (self.headers.get("Host") or "").lower()
        if host not in ("127.0.0.1:%d" % PORT_IN_USE, "localhost:%d" % PORT_IN_USE):
            return False
        return not mutating or self.headers.get("X-Klaro") == "1"

    def _safe(self, fn, mutating=False):
        try:
            if not self.guard(mutating):
                self.close_connection = True
                return self.err(403, "Acceso no permitido")
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception as e:  # noqa
            self.close_connection = True
            try:
                self.err(500, "Error interno: %s" % e)
            except Exception:
                pass

    def do_GET(self):
        self._safe(self.route_get)

    def do_HEAD(self):
        self._safe(self.route_get)

    def do_POST(self):
        self._safe(self.route_post, True)

    def do_PUT(self):
        self._safe(self.route_put, True)

    def do_DELETE(self):
        self._safe(self.route_delete, True)

    def get_video(self, vid):
        with closing(db()) as c:
            return c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()

    # ---- rutas ampliadas (v1.1): se atienden primero; si no coinciden, sigue el flujo normal
    def unlocked(self, v):
        if not v["pw_hash"]:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, val = part.strip().partition("=")
            if k == "kt" and v["id"] in UNLOCKED.get(val, ()):
                return True
        return False

    def J(self, obj, status=200):
        self.send_json(obj, status)
        return True

    def xget(self, p, q):
        J = self.J
        if p == "/api/version":
            return J({"version": VERSION, "update": UPDATE.get("info"), "download": DOWNLOAD_URL, "can_install": can_install()})
        if p == "/api/update/status":
            return J(dict(UPD, can_install=can_install()))
        if p == "/api/settings":
            with closing(db()) as c:
                return J(get_settings(c))
        if p == "/logo":
            if not os.path.isfile(LOGO):
                return J({"error": "Sin logo"}, 404)
            with open(LOGO, "rb") as fh:
                self.send_bytes(fh.read(), "image/png", extra={"Cache-Control": "no-cache"})
            return True
        if p == "/api/videos":
            term = q.get("q", [""])[0].strip()[:80]
            where, args = [], []
            f, coll = q.get("f", [""])[0], q.get("coll", [""])[0]
            if f == "hidden":
                where.append("hidden=1")
            elif not coll:
                where.append("hidden=0")
            if term:
                where.append("(title LIKE ? OR description LIKE ? OR tags LIKE ?)")
                args += ["%" + term + "%"] * 3
            if f in ("fav", "later"):
                where.append(f + "=1")
            tag = clean_tags(q.get("tag", [""])[0])
            if tag:
                where.append("(',' || tags || ',') LIKE ?")
                args.append("%," + tag + ",%")
            if coll.isdigit():
                where.append("id IN (SELECT video_id FROM coll_items WHERE coll_id=?)")
                args.append(int(coll))
            order = SORTS.get(q.get("sort", ["recent"])[0], SORTS["recent"])
            sql = "SELECT * FROM videos" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY " + order + " LIMIT 1000"
            with closing(db()) as c:
                return J({"videos": [jv(r) for r in c.execute(sql, args)]})
        m = re.fullmatch(r"/api/video/([\w-]+)", p)
        if m:
            v = self.get_video(m.group(1))
            if not v:
                return J({"error": "Vídeo no encontrado"}, 404)
            ok = self.unlocked(v)
            d = {"video": jv(v), "unlocked": ok, "markers": [], "subs": [], "ctas": []}
            if ok:
                with closing(db()) as c:
                    d["markers"] = [{"id": r["id"], "t": r["t"], "label": r["label"], "kind": r["kind"], "done": bool(r["done"])}
                                    for r in c.execute("SELECT * FROM markers WHERE video_id=? ORDER BY t", (v["id"],))]
                    d["subs"] = [{"lang": r["lang"], "label": r["label"]} for r in
                                 c.execute("SELECT * FROM subs WHERE video_id=? ORDER BY label", (v["id"],))]
                    d["ctas"] = [{"id": r["id"], "t_start": r["t_start"], "t_end": r["t_end"], "text": r["text"],
                                  "label": r["label"], "url": r["url"]} for r in
                                 c.execute("SELECT * FROM ctas WHERE video_id=? ORDER BY t_start", (v["id"],))]
            return J(d)
        m = re.fullmatch(r"/media/([\w-]+)", p)
        if m:
            v = self.get_video(m.group(1))
            if not v:
                return J({"error": "Vídeo no encontrado"}, 404)
            if not self.unlocked(v):
                return J({"error": "Vídeo protegido con contraseña"}, 403)
            self.serve_media(v, q.get("dl") == ["1"])
            return True
        m = re.fullmatch(r"/subs/([\w-]+)/([A-Za-z-]{2,8})", p)
        if m:
            v = self.get_video(m.group(1))
            f = os.path.join(SUBS, "%s.%s.vtt" % (m.group(1), m.group(2).lower()))
            if not v or not self.unlocked(v) or not os.path.isfile(f):
                return J({"error": "Sin subtítulos"}, 404)
            with open(f, "rb") as fh:
                self.send_bytes(fh.read(), "text/vtt; charset=utf-8", extra={"Cache-Control": "no-cache"})
            return True
        if p == "/api/stats":
            days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * d)) for d in range(13, -1, -1)]
            with closing(db()) as c:
                vrows = {r[0]: r[1] for r in c.execute("SELECT day,SUM(n) FROM vviews GROUP BY day")}
                wrows = {r[0]: r[1] for r in c.execute("SELECT day,SUM(seconds) FROM watch GROUP BY day")}
                t = c.execute("SELECT COUNT(*),COALESCE(SUM(views),0),COALESCE(SUM(size),0),COALESCE(SUM(fav),0) FROM videos").fetchone()
                secs = c.execute("SELECT COALESCE(SUM(seconds),0) FROM watch").fetchone()[0]
                top = [jv(r) for r in c.execute("SELECT * FROM videos WHERE hidden=0 ORDER BY views DESC, created DESC LIMIT 6")]
                table = [{"id": r["id"], "title": r["title"], "views": r["views"], "minutes": round((r["sec"] or 0) / 60, 1),
                          "w": r["w"], "h": r["h"], "size": r["size"], "locked": bool(r["pw_hash"])} for r in c.execute(
                    "SELECT v.*,(SELECT SUM(seconds) FROM watch WHERE video_id=v.id) sec FROM videos v ORDER BY v.views DESC, v.created DESC LIMIT 100")]
            return J({"videos": t[0], "views": t[1], "size": t[2], "favs": t[3], "minutes": round(secs / 60, 1), "data": DATA,
                      "days": [[d[5:], vrows.get(d, 0), round(wrows.get(d, 0) / 60, 1)] for d in days], "top": top, "table": table})
        if p == "/api/report.csv":
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(["Título", "Fecha", "Tamaño (MB)", "Resolución", "Vistas", "Minutos vistos", "Favorito", "Etiquetas"])
            with closing(db()) as c:
                for r in c.execute("SELECT v.*,(SELECT SUM(seconds) FROM watch WHERE video_id=v.id) sec FROM videos v ORDER BY v.views DESC"):
                    w.writerow([r["title"], time.strftime("%Y-%m-%d", time.localtime(r["created"])), round(r["size"] / 1e6, 1),
                                ("%dx%d" % (r["w"], r["h"])) if r["w"] else "", r["views"], round((r["sec"] or 0) / 60, 1),
                                "sí" if r["fav"] else "no", r["tags"].replace(",", ", ")])
            self.send_bytes(("\ufeff" + buf.getvalue()).encode("utf-8"), "text/csv; charset=utf-8",
                            extra={"Content-Disposition": "attachment; filename=klaro-informe.csv"})
            return True
        return False

    def xpost(self, p, q):
        J = self.J
        m = re.fullmatch(r"/api/video/([\w-]+)/(unlock|pos|markers|subs|ctas)", p)
        if m:
            vid, act = m.groups()
            v = self.get_video(vid)
            if not v:
                return J({"error": "Vídeo no encontrado"}, 404)
            if act == "unlock":
                pw = str(self.json_body().get("password", ""))
                if not v["pw_hash"] or hmac.compare_digest(v["pw_hash"], hash_pw(pw, v["pw_salt"])):
                    cur = ""
                    for part in (self.headers.get("Cookie") or "").split(";"):
                        k, _, val = part.strip().partition("=")
                        if k == "kt":
                            cur = val
                    tok = cur if cur in UNLOCKED else secrets.token_urlsafe(24)
                    UNLOCKED.setdefault(tok, set()).add(vid)
                    self.send_bytes(b'{"ok": true}', "application/json", 200,
                                    {"Set-Cookie": "kt=%s; Path=/; HttpOnly; SameSite=Strict" % tok})
                    return True
                return J({"error": "Contraseña incorrecta"}, 403)
            if not self.unlocked(v):
                return J({"error": "Vídeo protegido con contraseña"}, 403)
            with closing(db()) as c:
                if act == "pos":
                    d = self.json_body()
                    try:
                        pos, dur = max(0.0, float(d.get("pos", 0))), max(0.0, float(d.get("dur", 0)))
                        wid, hei = int(d.get("w", 0) or 0), int(d.get("h", 0) or 0)
                        watched = min(120.0, max(0.0, float(d.get("watched", 0) or 0)))
                    except (TypeError, ValueError):
                        return J({"error": "Datos no válidos"}, 400)
                    c.execute("UPDATE videos SET pos=?, dur=?, played=? WHERE id=?", (pos, dur, int(time.time()), vid))
                    if wid > 0 and hei > 0:
                        c.execute("UPDATE videos SET w=?, h=? WHERE id=?", (wid, hei, vid))
                    if watched > 0:
                        c.execute("INSERT INTO watch(video_id,day,seconds) VALUES(?,?,?) ON CONFLICT(video_id,day) DO UPDATE SET seconds=seconds+?",
                                  (vid, time.strftime("%Y-%m-%d"), watched, watched))
                elif act == "markers":
                    d = self.json_body()
                    try:
                        t = max(0.0, float(d.get("t", 0)))
                    except (TypeError, ValueError):
                        return J({"error": "Datos no válidos"}, 400)
                    kind = "note" if d.get("kind") == "note" else "chapter"
                    c.execute("INSERT INTO markers(video_id,t,label,kind) VALUES(?,?,?,?)", (vid, t, str(d.get("label", "")).strip()[:120], kind))
                elif act == "subs":
                    lang = q.get("lang", [""])[0].lower()
                    if not re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,4})?", lang):
                        return J({"error": "Idioma no válido"}, 400)
                    try:
                        raw = self.body(2_000_000)
                    except ValueError:
                        return J({"error": "Archivo de subtítulos demasiado grande"}, 413)
                    if not raw:
                        return J({"error": "Archivo vacío"}, 400)
                    with open(os.path.join(SUBS, "%s.%s.vtt" % (vid, lang)), "w", encoding="utf-8") as fh:
                        fh.write(to_vtt(raw))
                    c.execute("INSERT INTO subs(video_id,lang,label) VALUES(?,?,?) ON CONFLICT(video_id,lang) DO UPDATE SET label=?",
                              (vid, lang, q.get("label", [lang])[0][:30], q.get("label", [lang])[0][:30]))
                elif act == "ctas":
                    d = self.json_body()
                    url = str(d.get("url", "")).strip()
                    text, label = str(d.get("text", "")).strip()[:160], str(d.get("label", "")).strip()[:40]
                    try:
                        t0, t1 = max(0.0, float(d.get("t_start", 0))), max(0.0, float(d.get("t_end", 0)))
                    except (TypeError, ValueError):
                        return J({"error": "Datos no válidos"}, 400)
                    if not re.match(r"https?://", url) or not text or not label or t1 <= t0:
                        return J({"error": "Revisa el texto, el botón, el enlace (http…) y los tiempos"}, 400)
                    c.execute("INSERT INTO ctas(video_id,t_start,t_end,text,label,url) VALUES(?,?,?,?,?,?)", (vid, t0, t1, text, label, url[:500]))
                c.commit()
            return J({"ok": True})
        m = re.fullmatch(r"/api/markers/(\d+)/done", p)
        if m:
            with closing(db()) as c:
                c.execute("UPDATE markers SET done=1-done WHERE id=?", (int(m.group(1)),))
                c.commit()
            return J({"ok": True})
        if p == "/api/settings":
            d = self.json_body()
            with closing(db()) as c:
                for k, val in d.items():
                    if k in SETTINGS_DEFAULTS:
                        c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=?",
                                  (k, str(val)[:20000], str(val)[:20000]))
                c.commit()
            return J({"ok": True})
        if p == "/api/settings/logo":
            data = self.body(2_000_000)
            if data[:4] != b"\x89PNG":
                return J({"error": "El logo debe ser PNG"}, 400)
            with open(LOGO, "wb") as fh:
                fh.write(data)
            return J({"ok": True})
        if p == "/api/update/check":
            check_update()
            return J({"ok": True, "update": UPDATE.get("info")})
        if p == "/api/update/install":
            if not can_install():
                return J({"error": "La actualización automática solo funciona en la app instalada. Descarga el instalador desde la web."}, 400)
            if not UPDATE.get("info"):
                return J({"error": "No hay ninguna versión nueva."}, 400)
            if UPD["state"] not in ("downloading", "installing"):
                threading.Thread(target=run_update, daemon=True).start()
            return J({"ok": True})
        if p == "/api/open-url":
            url = str(self.json_body().get("url", ""))
            if re.match(r"https?://", url):
                webbrowser.open(url)
                return J({"ok": True})
            return J({"error": "Enlace no válido"}, 400)
        return False

    def xput(self, p):
        J = self.J
        m = re.fullmatch(r"/api/video/([\w-]+)", p)
        v = self.get_video(m.group(1)) if m else None
        if not v:
            return False
        if not self.unlocked(v):
            return J({"error": "Vídeo protegido con contraseña"}, 403)
        d = self.json_body()
        title = str(d.get("title", v["title"])).strip()[:120]
        desc = str(d.get("description", v["description"])).strip()[:2000]
        tags = clean_tags(d["tags"]) if "tags" in d else v["tags"]
        hidden = 1 if d.get("hidden", bool(v["hidden"])) else 0
        salt, ph = v["pw_salt"], v["pw_hash"]
        if "password" in d:
            pw = str(d["password"])
            if pw == "":
                salt, ph = "", ""
            elif len(pw) < 4:
                return J({"error": "La contraseña debe tener al menos 4 caracteres"}, 400)
            else:
                salt = secrets.token_hex(16)
                ph = hash_pw(pw, salt)
        if not title:
            return J({"error": "El título no puede estar vacío"}, 400)
        with closing(db()) as c:
            c.execute("UPDATE videos SET title=?,description=?,tags=?,hidden=?,pw_salt=?,pw_hash=? WHERE id=?",
                      (title, desc, tags, hidden, salt, ph, v["id"]))
            c.commit()
        return J({"ok": True})

    def xdelete(self, p, q):
        J = self.J
        m = re.fullmatch(r"/api/video/([\w-]+)", p)
        if m:
            v = self.get_video(m.group(1))
            if not v:
                return J({"error": "No encontrado"}, 404)
            if not self.unlocked(v):
                return J({"error": "Vídeo protegido con contraseña"}, 403)
            files = [os.path.join(VIDEOS, v["file"]), os.path.join(THUMBS, v["id"] + ".jpg")] + glob.glob(os.path.join(SUBS, v["id"] + ".*.vtt"))
            for f in files:
                if os.path.isfile(f):
                    os.remove(f)
            with closing(db()) as c:
                c.execute("DELETE FROM videos WHERE id=?", (v["id"],))
                c.commit()
            return J({"ok": True})
        m = re.fullmatch(r"/api/video/([\w-]+)/subs", p)
        if m:
            v = self.get_video(m.group(1))
            lang = q.get("lang", [""])[0].lower()
            if not v or not self.unlocked(v) or not re.fullmatch(r"[a-z]{2,3}(-[a-z]{2,4})?", lang):
                return J({"error": "No permitido"}, 403)
            f = os.path.join(SUBS, "%s.%s.vtt" % (v["id"], lang))
            if os.path.isfile(f):
                os.remove(f)
            with closing(db()) as c:
                c.execute("DELETE FROM subs WHERE video_id=? AND lang=?", (v["id"], lang))
                c.commit()
            return J({"ok": True})
        m = re.fullmatch(r"/api/ctas/(\d+)", p)
        if m:
            with closing(db()) as c:
                c.execute("DELETE FROM ctas WHERE id=?", (int(m.group(1)),))
                c.commit()
            return J({"ok": True})
        if p == "/api/settings/logo":
            if os.path.isfile(LOGO):
                os.remove(LOGO)
            return J({"ok": True})
        return False

    # ---- GET
    def route_get(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if self.xget(p, q):
            return
        if p == "/":
            return self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8", extra={"Cache-Control": "no-store"})
        if p == "/api/videos":
            term = q.get("q", [""])[0].strip()[:80]
            where, args = [], []
            if term:
                where.append("(title LIKE ? OR description LIKE ? OR tags LIKE ?)")
                args += ["%" + term + "%"] * 3
            f = q.get("f", [""])[0]
            if f in ("fav", "later"):
                where.append(f + "=1")
            tag = clean_tags(q.get("tag", [""])[0])
            if tag:
                where.append("(',' || tags || ',') LIKE ?")
                args.append("%," + tag + ",%")
            if q.get("coll", [""])[0].isdigit():
                where.append("id IN (SELECT video_id FROM coll_items WHERE coll_id=?)")
                args.append(int(q["coll"][0]))
            order = SORTS.get(q.get("sort", ["recent"])[0], SORTS["recent"])
            sql = "SELECT * FROM videos" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY " + order + " LIMIT 1000"
            with closing(db()) as c:
                return self.send_json({"videos": [jv(r) for r in c.execute(sql, args)]})
        if p == "/api/tags":
            counts = {}
            with closing(db()) as c:
                for r in c.execute("SELECT tags FROM videos WHERE tags!=''"):
                    for t in r["tags"].split(","):
                        counts[t] = counts.get(t, 0) + 1
            return self.send_json({"tags": sorted(counts.items(), key=lambda x: (-x[1], x[0]))})
        if p == "/api/colls":
            with closing(db()) as c:
                return self.send_json({"colls": [{"id": r["id"], "name": r["name"], "n": r["n"]} for r in c.execute(
                    "SELECT k.id,k.name,(SELECT COUNT(*) FROM coll_items WHERE coll_id=k.id) n FROM colls k ORDER BY k.name")]})
        if p == "/api/stats":
            days = [time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400 * d)) for d in range(13, -1, -1)]
            with closing(db()) as c:
                rows = {r[0]: r[1] for r in c.execute("SELECT day,SUM(n) FROM vviews GROUP BY day")}
                t = c.execute("SELECT COUNT(*),COALESCE(SUM(views),0),COALESCE(SUM(size),0),COALESCE(SUM(fav),0) FROM videos").fetchone()
                top = [jv(r) for r in c.execute("SELECT * FROM videos ORDER BY views DESC, created DESC LIMIT 6")]
            return self.send_json({"videos": t[0], "views": t[1], "size": t[2], "favs": t[3], "data": DATA,
                                   "days": [[d[5:], rows.get(d, 0)] for d in days], "top": top})
        m = re.fullmatch(r"/api/video/([\w-]+)", p)
        if m:
            v = self.get_video(m.group(1))
            if not v:
                return self.err(404, "Vídeo no encontrado")
            with closing(db()) as c:
                mk = [{"id": r["id"], "t": r["t"], "label": r["label"]} for r in
                      c.execute("SELECT * FROM markers WHERE video_id=? ORDER BY t", (v["id"],))]
            return self.send_json({"video": jv(v), "markers": mk})
        m = re.fullmatch(r"/thumb/([\w-]+)", p)
        if m:
            f = os.path.join(THUMBS, m.group(1) + ".jpg")
            if not os.path.isfile(f):
                return self.err(404, "Sin miniatura")
            with open(f, "rb") as fh:
                return self.send_bytes(fh.read(), "image/jpeg", extra={"Cache-Control": "no-cache"})
        m = re.fullmatch(r"/subs/([\w-]+)", p)
        if m:
            f = os.path.join(SUBS, m.group(1) + ".vtt")
            if not os.path.isfile(f):
                return self.err(404, "Sin subtítulos")
            with open(f, "rb") as fh:
                return self.send_bytes(fh.read(), "text/vtt; charset=utf-8", extra={"Cache-Control": "no-cache"})
        m = re.fullmatch(r"/media/([\w-]+)", p)
        if m:
            v = self.get_video(m.group(1))
            return self.serve_media(v, q.get("dl") == ["1"]) if v else self.err(404, "Vídeo no encontrado")
        self.err(404, "No encontrado")

    def serve_media(self, v, download=False):
        path = os.path.join(VIDEOS, v["file"])
        if not os.path.isfile(path):
            return self.err(404, "Archivo no encontrado")
        total = os.path.getsize(path)
        start, end, status = 0, total - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end = int(m.group(2)) if m.group(2) else total - 1
                else:
                    start = max(0, total - int(m.group(2)))
                end = min(end, total - 1)
                if start > end or start >= total:
                    return self._headers(416, "text/plain", 0, {"Content-Range": "bytes */%d" % total})
                status = 206
        length = end - start + 1
        extra = {"Accept-Ranges": "bytes", "Cache-Control": "max-age=3600"}
        if status == 206:
            extra["Content-Range"] = "bytes %d-%d/%d" % (start, end, total)
        if download:
            name = re.sub(r'[\\/:*?"<>|]', "_", v["title"]) + os.path.splitext(v["file"])[1]
            extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name)
        self._headers(status, v["mime"], length, extra)
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1024 * 512, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    # ---- POST
    def route_post(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        if self.xpost(p, q):
            return

        if p == "/api/upload":
            fname = os.path.basename(q.get("filename", [""])[0])[:200]
            ext = os.path.splitext(fname)[1].lower()
            if ext not in ALLOWED:
                self.close_connection = True
                return self.err(400, "Formato no admitido. Usa: " + ", ".join(ALLOWED))
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_UPLOAD:
                self.close_connection = True
                return self.err(413, "Archivo vacío o demasiado grande")
            vid = secrets.token_urlsafe(7)
            fpath = os.path.join(VIDEOS, vid + ext)
            left = n
            try:
                with open(fpath, "wb") as out:
                    while left > 0:
                        chunk = self.rfile.read(min(1024 * 1024, left))
                        if not chunk:
                            raise ConnectionError("Subida interrumpida")
                        out.write(chunk)
                        left -= len(chunk)
            except Exception:
                if os.path.exists(fpath):
                    os.remove(fpath)
                self.close_connection = True
                raise
            title = os.path.splitext(fname)[0][:120] or "Sin título"
            with closing(db()) as c:
                c.execute("INSERT INTO videos(id,title,file,mime,size,created) VALUES(?,?,?,?,?,?)",
                          (vid, title, vid + ext, ALLOWED[ext], n, int(time.time())))
                c.commit()
            return self.send_json({"id": vid})

        m = re.fullmatch(r"/api/video/([\w-]+)/(view|fav|later|pos|subs|markers|thumb)", p)
        if m:
            vid, act = m.groups()
            if not self.get_video(vid):
                return self.err(404, "Vídeo no encontrado")
            with closing(db()) as c:
                if act == "view":
                    c.execute("UPDATE videos SET views=views+1, played=? WHERE id=?", (int(time.time()), vid))
                    c.execute("INSERT INTO vviews(video_id,day,n) VALUES(?,?,1) ON CONFLICT(video_id,day) DO UPDATE SET n=n+1",
                              (vid, time.strftime("%Y-%m-%d")))
                elif act in ("fav", "later"):
                    c.execute("UPDATE videos SET %s=1-%s WHERE id=?" % (act, act), (vid,))
                elif act == "pos":
                    d = self.json_body()
                    try:
                        pos, dur = max(0.0, float(d.get("pos", 0))), max(0.0, float(d.get("dur", 0)))
                    except (TypeError, ValueError):
                        return self.err(400, "Datos no válidos")
                    c.execute("UPDATE videos SET pos=?, dur=?, played=? WHERE id=?", (pos, dur, int(time.time()), vid))
                elif act == "markers":
                    d = self.json_body()
                    try:
                        t = max(0.0, float(d.get("t", 0)))
                    except (TypeError, ValueError):
                        return self.err(400, "Datos no válidos")
                    c.execute("INSERT INTO markers(video_id,t,label) VALUES(?,?,?)", (vid, t, str(d.get("label", "")).strip()[:80]))
                elif act == "subs":
                    try:
                        raw = self.body(2_000_000)
                    except ValueError:
                        return self.err(413, "Archivo de subtítulos demasiado grande")
                    if not raw:
                        return self.err(400, "Archivo vacío")
                    with open(os.path.join(SUBS, vid + ".vtt"), "w", encoding="utf-8") as fh:
                        fh.write(to_vtt(raw))
                    c.execute("UPDATE videos SET has_subs=1 WHERE id=?", (vid,))
                elif act == "thumb":
                    try:
                        data = self.body(2_000_000)
                    except ValueError:
                        return self.err(413, "Miniatura demasiado grande")
                    if data[:2] != b"\xff\xd8":
                        return self.err(400, "La miniatura debe ser JPEG")
                    with open(os.path.join(THUMBS, vid + ".jpg"), "wb") as fh:
                        fh.write(data)
                    c.execute("UPDATE videos SET has_thumb=1 WHERE id=?", (vid,))
                c.commit()
            return self.send_json({"ok": True})

        if p == "/api/colls":
            name = str(self.json_body().get("name", "")).strip()[:60]
            if not name:
                return self.err(400, "Escribe un nombre")
            with closing(db()) as c:
                c.execute("INSERT INTO colls(name) VALUES(?)", (name,))
                c.commit()
            return self.send_json({"ok": True})

        m = re.fullmatch(r"/api/colls/(\d+)/add/([\w-]+)", p)
        if m:
            with closing(db()) as c:
                if not c.execute("SELECT 1 FROM colls WHERE id=?", (int(m.group(1)),)).fetchone() or not self.get_video(m.group(2)):
                    return self.err(404, "No encontrado")
                c.execute("INSERT OR IGNORE INTO coll_items(coll_id,video_id) VALUES(?,?)", (int(m.group(1)), m.group(2)))
                c.commit()
            return self.send_json({"ok": True})

        if p == "/api/open-data":
            open_path(DATA)
            return self.send_json({"ok": True})

        self.err(404, "No encontrado")

    # ---- PUT / DELETE
    def route_put(self):
        if self.xput(urlparse(self.path).path):
            return
        m = re.fullmatch(r"/api/video/([\w-]+)", urlparse(self.path).path)
        v = self.get_video(m.group(1)) if m else None
        if not v:
            return self.err(404, "No encontrado")
        d = self.json_body()
        title = str(d.get("title", v["title"])).strip()[:120]
        desc = str(d.get("description", v["description"])).strip()[:2000]
        tags = clean_tags(d["tags"]) if "tags" in d else v["tags"]
        if not title:
            return self.err(400, "El título no puede estar vacío")
        with closing(db()) as c:
            c.execute("UPDATE videos SET title=?,description=?,tags=? WHERE id=?", (title, desc, tags, v["id"]))
            c.commit()
        self.send_json({"ok": True})

    def route_delete(self):
        u = urlparse(self.path)
        p = u.path
        if self.xdelete(p, parse_qs(u.query)):
            return
        m = re.fullmatch(r"/api/video/([\w-]+)", p)
        if m:
            v = self.get_video(m.group(1))
            if not v:
                return self.err(404, "No encontrado")
            for f in (os.path.join(VIDEOS, v["file"]), os.path.join(THUMBS, v["id"] + ".jpg"), os.path.join(SUBS, v["id"] + ".vtt")):
                if os.path.isfile(f):
                    os.remove(f)
            with closing(db()) as c:
                c.execute("DELETE FROM videos WHERE id=?", (v["id"],))
                c.commit()
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/video/([\w-]+)/subs", p)
        if m:
            f = os.path.join(SUBS, m.group(1) + ".vtt")
            if os.path.isfile(f):
                os.remove(f)
            with closing(db()) as c:
                c.execute("UPDATE videos SET has_subs=0 WHERE id=?", (m.group(1),))
                c.commit()
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/markers/(\d+)", p)
        if m:
            with closing(db()) as c:
                c.execute("DELETE FROM markers WHERE id=?", (int(m.group(1)),))
                c.commit()
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/colls/(\d+)", p)
        if m:
            with closing(db()) as c:
                c.execute("DELETE FROM colls WHERE id=?", (int(m.group(1)),))
                c.commit()
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/colls/(\d+)/remove/([\w-]+)", p)
        if m:
            with closing(db()) as c:
                c.execute("DELETE FROM coll_items WHERE coll_id=? AND video_id=?", (int(m.group(1)), m.group(2)))
                c.commit()
            return self.send_json({"ok": True})
        self.err(404, "No encontrado")


# ------------------------------------------------------------------ interfaz
INDEX_HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512' xmlns:c2pa='http://c2pa.org/manifest'%3E%3Cdefs%3E%3ClinearGradient id='bg' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%231d2735'/%3E%3Cstop offset='1' stop-color='%23080b10'/%3E%3C/linearGradient%3E%3ClinearGradient id='rim' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23ffffff' stop-opacity='.38'/%3E%3Cstop offset='.55' stop-color='%23ffffff' stop-opacity='0'/%3E%3C/linearGradient%3E%3CradialGradient id='glow' cx='.5' cy='.5' r='.5'%3E%3Cstop offset='0' stop-color='%232ee6a6' stop-opacity='.30'/%3E%3Cstop offset='1' stop-color='%232ee6a6' stop-opacity='0'/%3E%3C/radialGradient%3E%3ClinearGradient id='k' gradientUnits='userSpaceOnUse' x1='140' y1='110' x2='390' y2='410'%3E%3Cstop offset='0' stop-color='%238dffd9'/%3E%3Cstop offset='.5' stop-color='%232ee6a6'/%3E%3Cstop offset='1' stop-color='%2313b6cc'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='8' y='8' width='496' height='496' rx='120' fill='url(%23bg)'/%3E%3Ccircle cx='256' cy='262' r='200' fill='url(%23glow)'/%3E%3Crect x='9.5' y='9.5' width='493' height='493' rx='118.5' fill='none' stroke='url(%23rim)' stroke-width='3'/%3E%3Cg fill='none' stroke='url(%23k)' stroke-width='58' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M176 142V370'/%3E%3Cpath d='M176 270 338 142'/%3E%3Cpath d='M237 222 342 370'/%3E%3C/g%3E%3Cmetadata%3E%3Cc2pa:manifest%3EAAAWgmp1bWIAAAAeanVtZGMycGEAEQAQgAAAqgA4m3EDYzJwYQAAABZcanVtYgAAAEdqdW1kYzJtYQARABCAAACqADibcQN1cm46YzJwYTpmZDY3ZTk2NC1kMGY2LTQwMzEtYjcyMi1mZGU2YjY2YWZjNGUAAAADl2p1bWIAAAApanVtZGMyYXMAEQAQgAAAqgA4m3EDYzJwYS5hc3NlcnRpb25zAAAAALxqdW1iAAAARGp1bWRjYm9yABEAEIAAAKoAOJtxE2MycGEuaW5ncmVkaWVudC52MwAAAAAYYzJzaBLACGL26AC4Gf%2BVRwWCa9kAAABwY2JvcqNpZGM6Zm9ybWF0bWltYWdlL3N2Zyt4bWxqaW5zdGFuY2VJRHgseG1wOmlpZDowNjE5NmE3Yy00MjY2LTQ4NDItYmNlNy01NzkxNjc4Yjc5ZTlscmVsYXRpb25zaGlwaHBhcmVudE9mAAAB4mp1bWIAAABBanVtZGNib3IAEQAQgAAAqgA4m3ETYzJwYS5hY3Rpb25zLnYyAAAAABhjMnNog6QtQshsmz/SRn6kQvuj0gAAAZljYm9yomdhY3Rpb25zgqJmYWN0aW9ua2MycGEub3BlbmVkanBhcmFtZXRlcnOha2luZ3JlZGllbnRzgaJjdXJseC1zZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmluZ3JlZGllbnQudjNkaGFzaFgg2tDd5gdpk1GfJ74LzBoYmc3mqK4WqGS7BhRUbU41fMKkZmFjdGlvbngdY29tLmFudGhyb3BpYy5jbGF1ZGUucHJvdmlkZWRqcGFyYW1ldGVyc6F4H2NvbS5hbnRocm9waWMub3JpZ2luLWNvbmZpZGVuY2VndW5rbm93bmtkZXNjcmlwdGlvbnhmQ2xhdWRlIHByb3ZpZGVkIHRoaXMgZmlsZSBhdCB0aGUgcmVxdWVzdCBvZiBhIHVzZXIgYW5kIG1heSBoYXZlIGNyZWF0ZWQgb3IgbW9kaWZpZWQgdGhlIGZpbGUgY29udGVudHMubXNvZnR3YXJlQWdlbnShZG5hbWVmQ2xhdWRlcmFsbEFjdGlvbnNJbmNsdWRlZPUAAADIanVtYgAAAEBqdW1kY2JvcgARABCAAACqADibcRNjMnBhLmhhc2guZGF0YQAAAAAYYzJzaDtkn/L6GgT6kKQ0TiFmplgAAACAY2JvcqVjYWxnZnNoYTI1NmNwYWRMAAAAAAAAAAAAAAAAZGhhc2hYINsRWBITNHaw2j//Gy1GQGg3cSWfxZR2kYc8fwRzCbsQZG5hbWVuanVtYmYgbWFuaWZlc3RqZXhjbHVzaW9uc4GiZXN0YXJ0GQUVZmxlbmd0aBkeBAAAAj5qdW1iAAAAJ2p1bWRjMmNsABEAEIAAAKoAOJtxA2MycGEuY2xhaW0udjIAAAACD2Nib3KlY2FsZ2ZzaGEyNTZpc2lnbmF0dXJleE1zZWxmI2p1bWJmPS9jMnBhL3VybjpjMnBhOmZkNjdlOTY0LWQwZjYtNDAzMS1iNzIyLWZkZTZiNjZhZmM0ZS9jMnBhLnNpZ25hdHVyZWppbnN0YW5jZUlEeCx4bXA6aWlkOjQ4MWQzM2RhLTNjOWItNDVjMC05ZTdkLWFmMWYxOTdkY2MyZHJjcmVhdGVkX2Fzc2VydGlvbnODomN1cmx4LXNlbGYjanVtYmY9YzJwYS5hc3NlcnRpb25zL2MycGEuaW5ncmVkaWVudC52M2RoYXNoWCDa0N3mB2mTUZ8nvgvMGhiZzeaorhaoZLsGFFRtTjV8wqJjdXJseCpzZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmFjdGlvbnMudjJkaGFzaFggnmNYLNOUKLIHQ7EVi2suAOrlrbgms%2BZOmghwYW1Q4WKiY3VybHgpc2VsZiNqdW1iZj1jMnBhLmFzc2VydGlvbnMvYzJwYS5oYXNoLmRhdGFkaGFzaFggTG6xQN5AwLIigQhXy7GcxAjDsxLuHsBy3oWwywpJAe50Y2xhaW1fZ2VuZXJhdG9yX2luZm%2BjZG5hbWVvQW50aHJvcGljIEZpbGVzZ3ZlcnNpb25lMS4wLjBrc3BlY1ZlcnNpb25lMi40LjAAABA4anVtYgAAAChqdW1kYzJjcwARABCAAACqADibcQNjMnBhLnNpZ25hdHVyZQAAABAIY2JvctKEWQISogEmGCFZAgowggIGMIIBjaADAgECAhRA5aAK7sI50L64g/oGQgU9Z1UTADAKBggqhkjOPQQDAzBJMRcwFQYDVQQKEw5BbnRocm9waWMsIFBCQzEuMCwGA1UEAxMlQW50aHJvcGljIENvbnRlbnQgQ3JlZGVudGlhbHMgUm9vdCBDQTAeFw0yNjA4MDcxODQzNTZaFw0yODA4MDYxOTQzNTZaMEQxFzAVBgNVBAoTDkFudGhyb3BpYywgUEJDMSkwJwYDVQQDEyBBbnRocm9waWMgQ2xhdWRlIENvbnRlbnQgU2lnbmluZzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABJh6CmvLUBgFFNU0vUKlOVtE6djd17L5SuwX0LemFisBM3dkd/3cyjxFA3Qo5S46fX0/ihY0VZ7mfb9KF703t5OjWDBWMA4GA1UdDwEB/wQEAwIHgDAVBgNVHSUEDjAMBgorBgEEAYPoXgIBMAwGA1UdEwEB/wQCMAAwHwYDVR0jBBgwFoAUzlHiBIFOZFsj%2BOPEz5o%2BnMHXXMIwCgYIKoZIzj0EAwMDZwAwZAIwMXMdFJ4BetLLVY7ORuE9noqbbAZOZn/aArXyTwFAZfKrPzxF2vPoJNf1%2BUCdg1XGAjBwX1zd9WGqYkqmL5SFqw1QySjr1zJfpJM9%2B1rdDwSPLMOPOjKuiXjoU/pUUeG9RwmhY3BhZFkNngAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPZYQKCccUto3kd/w4F2KlO/ZQv66aKp0USLh8lqLcVovfB8b07NbMSMYyCyau4wB3IoJpPRG4uMnaDQJGcsq50lr9s=%3C/c2pa:manifest%3E%3C/metadata%3E%3C/svg%3E">
<title>Klaro</title>
<style>
:root{--bg:#0e1116;--panel:#171b22;--panel2:#1f2530;--line:#2a3140;--text:#e8ecf3;--mute:#8d97a8;--acc:#2ee6a6;--acc2:#ff6b5e}
*{box-sizing:border-box}body{margin:0;display:flex;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}
a{color:inherit;text-decoration:none}
#sb{width:264px;flex:none;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;padding:16px 12px;background:#0b0e13;border-right:1px solid var(--line);z-index:20}
.bh{display:flex;align-items:center;gap:12px;padding:0 8px 14px;font-size:19px}
.lg{width:38px;height:38px;display:block}
.pill{font-size:10px;font-weight:700;letter-spacing:.6px;color:var(--acc);border:1px solid var(--acc);border-radius:99px;padding:0 7px;line-height:17px}
nav{display:flex;flex-direction:column;flex:1;overflow:auto;gap:1px;margin-right:-6px;padding-right:6px}
nav .sec{font-size:11px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#5c667a;padding:14px 14px 4px}
nav a{display:flex;align-items:center;gap:14px;padding:8px 14px;border-radius:12px;color:var(--mute)}
nav a.on{background:var(--panel2);color:var(--text)}nav a:hover{color:var(--text)}nav a.soon{opacity:.6}.ic{width:20px;text-align:center}
.bt{margin-left:auto;font-size:9px;font-weight:700;letter-spacing:.6px;color:var(--mute);border:1px solid var(--line);border-radius:99px;padding:0 6px;line-height:15px}
.up{width:100%;background:#fff;color:#0b0e13;border-radius:14px;padding:12px;margin:10px 0 6px}
.foot{padding:4px 8px;font-size:12px;color:var(--mute)}
#wrap{flex:1;min-width:0}
header{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;padding:12px 24px;background:rgba(14,17,22,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
input,textarea,select{width:100%;padding:10px 12px;background:var(--panel2);border:1px solid var(--line);border-radius:10px;color:var(--text);font:inherit}
input[type=checkbox]{width:auto}input[type=color]{padding:2px;height:40px;width:64px}input[type=range]{padding:0}
input:focus,textarea:focus,select:focus{outline:2px solid var(--acc);border-color:transparent}
.search{max-width:360px}
button,.btn{cursor:pointer;border:0;border-radius:10px;padding:10px 16px;font:inherit;font-weight:600;background:var(--acc);color:#04130d;display:inline-block}
button.sec,.btn.sec{background:var(--panel2);color:var(--text);border:1px solid var(--line)}button.del{background:var(--acc2);color:#fff}
button.beta{opacity:.55;cursor:not-allowed}button.beta .bt{margin-left:8px}
button:disabled{cursor:not-allowed}
main{max-width:1200px;margin:0 auto;padding:28px 24px}
h1{font-size:26px;margin:0 0 18px;letter-spacing:-.4px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:20px}
.card{display:block;background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden;transition:.15s}.card:hover{transform:translateY(-3px);border-color:var(--acc)}
.th{position:relative;aspect-ratio:16/9;background:linear-gradient(135deg,#1f2530,#12161c);display:flex;align-items:center;justify-content:center;color:var(--mute);font-size:34px}
.th img{width:100%;height:100%;object-fit:cover;display:block}.th .hb{position:absolute;top:8px;right:10px;color:var(--acc2);font-size:18px}
.th .lk{position:absolute;top:8px;left:10px;font-size:15px;background:rgba(0,0,0,.55);border-radius:8px;padding:0 6px}
.pg{position:absolute;left:0;bottom:0;height:4px;background:var(--acc)}
.ci{padding:12px 14px}.ci h3{margin:0 0 4px;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{color:var(--mute);font-size:13px}
.tag{display:inline-block;padding:1px 10px;border-radius:99px;font-size:12px;background:var(--panel2);border:1px solid var(--line);color:var(--mute)}a.tag:hover{border-color:var(--acc);color:var(--text)}
.tag.res{color:var(--acc);border-color:var(--acc)}
.err{color:var(--acc2);margin-top:12px;min-height:20px}
.drop{border:2px dashed var(--line);border-radius:16px;padding:48px;text-align:center;color:var(--mute);cursor:pointer}.drop.hv{border-color:var(--acc);color:var(--text)}
.bar{height:10px;background:var(--panel2);border-radius:9px;overflow:hidden;margin-top:14px}.bar i{display:block;height:100%;width:0;background:var(--acc)}
.watch{display:grid;gap:20px}
.pw{position:relative;background:#000;border-radius:14px;overflow:hidden}
video.pl{width:100%;max-height:72vh;background:#000;display:block}
.wm{position:absolute;max-width:18%;max-height:18%;pointer-events:none}.wm-tl{top:12px;left:12px}.wm-tr{top:12px;right:12px}.wm-bl{bottom:56px;left:12px}.wm-br{bottom:56px;right:12px}
.cta{position:absolute;left:16px;bottom:64px;max-width:70%;background:rgba(15,19,26,.92);border:1px solid var(--acc);border-radius:14px;padding:12px 16px;display:flex;gap:14px;align-items:center}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.box{max-width:420px;margin:40px auto;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:28px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.row select{width:auto}
.lb{display:block;margin:12px 0 4px;color:var(--mute);font-size:13px}
.empty{text-align:center;color:var(--mute);padding:60px 0}
.two{display:grid;grid-template-columns:1fr 1fr;gap:20px}
.chart{display:flex;align-items:flex-end;gap:6px;height:110px;margin-top:14px}.chart div{flex:1;background:var(--acc);border-radius:4px 4px 0 0}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}th{color:var(--mute);font-weight:600;font-size:12px}
.mod{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:50}.mod .panel{max-width:380px}
.mk{display:flex;gap:10px;align-items:center;margin-top:8px}.mk a.t{color:var(--acc);cursor:pointer;min-width:52px}.mk.cur{background:var(--panel2);border-radius:8px;padding:2px 6px}
.tp{position:relative;height:300px;overflow:hidden;background:#000;border-radius:14px;padding:0 24px}
.tpi{white-space:pre-wrap;text-align:center;line-height:1.5;padding:130px 0 300px;color:#fff;font-weight:600}
.tpfull{position:fixed;inset:0;background:#000;z-index:80;display:flex;flex-direction:column}
.tpfull .tp{flex:1;height:auto;border-radius:0;padding:0 8vw}.tpfull .tpi{padding:42vh 0 70vh}
.tpbar{display:flex;gap:10px;align-items:center;padding:10px 16px;background:#0b0e13;flex-wrap:wrap}
.rec{display:inline-block;width:12px;height:12px;border-radius:50%;background:var(--acc2);margin-right:8px;animation:bl 1s infinite}@keyframes bl{50%{opacity:.2}}
.soonbox{max-width:560px;margin:30px auto;text-align:center}.soonbox ul{text-align:left;color:var(--mute);margin:18px 0 0;padding-left:22px}
#updbar{display:none;gap:12px;align-items:center;padding:10px 24px;background:#0f2a22;border-bottom:1px solid var(--acc);font-size:14px}
#fatal{display:none;background:var(--acc2);color:#fff;padding:10px 16px;font-size:13px;position:fixed;left:0;right:0;bottom:0;z-index:99}
@media(max-width:900px){#sb{width:210px}main{padding:20px 14px}.two{grid-template-columns:1fr}}
</style></head><body>
<aside id="sb">
 <div class="bh"><img class="lg" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512' xmlns:c2pa='http://c2pa.org/manifest'%3E%3Cdefs%3E%3ClinearGradient id='bg' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%231d2735'/%3E%3Cstop offset='1' stop-color='%23080b10'/%3E%3C/linearGradient%3E%3ClinearGradient id='rim' x1='0' y1='0' x2='0' y2='1'%3E%3Cstop offset='0' stop-color='%23ffffff' stop-opacity='.38'/%3E%3Cstop offset='.55' stop-color='%23ffffff' stop-opacity='0'/%3E%3C/linearGradient%3E%3CradialGradient id='glow' cx='.5' cy='.5' r='.5'%3E%3Cstop offset='0' stop-color='%232ee6a6' stop-opacity='.30'/%3E%3Cstop offset='1' stop-color='%232ee6a6' stop-opacity='0'/%3E%3C/radialGradient%3E%3ClinearGradient id='k' gradientUnits='userSpaceOnUse' x1='140' y1='110' x2='390' y2='410'%3E%3Cstop offset='0' stop-color='%238dffd9'/%3E%3Cstop offset='.5' stop-color='%232ee6a6'/%3E%3Cstop offset='1' stop-color='%2313b6cc'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect x='8' y='8' width='496' height='496' rx='120' fill='url(%23bg)'/%3E%3Ccircle cx='256' cy='262' r='200' fill='url(%23glow)'/%3E%3Crect x='9.5' y='9.5' width='493' height='493' rx='118.5' fill='none' stroke='url(%23rim)' stroke-width='3'/%3E%3Cg fill='none' stroke='url(%23k)' stroke-width='58' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M176 142V370'/%3E%3Cpath d='M176 270 338 142'/%3E%3Cpath d='M237 222 342 370'/%3E%3C/g%3E%3Cmetadata%3E%3Cc2pa:manifest%3EAAAWgmp1bWIAAAAeanVtZGMycGEAEQAQgAAAqgA4m3EDYzJwYQAAABZcanVtYgAAAEdqdW1kYzJtYQARABCAAACqADibcQN1cm46YzJwYTpmZDY3ZTk2NC1kMGY2LTQwMzEtYjcyMi1mZGU2YjY2YWZjNGUAAAADl2p1bWIAAAApanVtZGMyYXMAEQAQgAAAqgA4m3EDYzJwYS5hc3NlcnRpb25zAAAAALxqdW1iAAAARGp1bWRjYm9yABEAEIAAAKoAOJtxE2MycGEuaW5ncmVkaWVudC52MwAAAAAYYzJzaBLACGL26AC4Gf%2BVRwWCa9kAAABwY2JvcqNpZGM6Zm9ybWF0bWltYWdlL3N2Zyt4bWxqaW5zdGFuY2VJRHgseG1wOmlpZDowNjE5NmE3Yy00MjY2LTQ4NDItYmNlNy01NzkxNjc4Yjc5ZTlscmVsYXRpb25zaGlwaHBhcmVudE9mAAAB4mp1bWIAAABBanVtZGNib3IAEQAQgAAAqgA4m3ETYzJwYS5hY3Rpb25zLnYyAAAAABhjMnNog6QtQshsmz/SRn6kQvuj0gAAAZljYm9yomdhY3Rpb25zgqJmYWN0aW9ua2MycGEub3BlbmVkanBhcmFtZXRlcnOha2luZ3JlZGllbnRzgaJjdXJseC1zZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmluZ3JlZGllbnQudjNkaGFzaFgg2tDd5gdpk1GfJ74LzBoYmc3mqK4WqGS7BhRUbU41fMKkZmFjdGlvbngdY29tLmFudGhyb3BpYy5jbGF1ZGUucHJvdmlkZWRqcGFyYW1ldGVyc6F4H2NvbS5hbnRocm9waWMub3JpZ2luLWNvbmZpZGVuY2VndW5rbm93bmtkZXNjcmlwdGlvbnhmQ2xhdWRlIHByb3ZpZGVkIHRoaXMgZmlsZSBhdCB0aGUgcmVxdWVzdCBvZiBhIHVzZXIgYW5kIG1heSBoYXZlIGNyZWF0ZWQgb3IgbW9kaWZpZWQgdGhlIGZpbGUgY29udGVudHMubXNvZnR3YXJlQWdlbnShZG5hbWVmQ2xhdWRlcmFsbEFjdGlvbnNJbmNsdWRlZPUAAADIanVtYgAAAEBqdW1kY2JvcgARABCAAACqADibcRNjMnBhLmhhc2guZGF0YQAAAAAYYzJzaDtkn/L6GgT6kKQ0TiFmplgAAACAY2JvcqVjYWxnZnNoYTI1NmNwYWRMAAAAAAAAAAAAAAAAZGhhc2hYINsRWBITNHaw2j//Gy1GQGg3cSWfxZR2kYc8fwRzCbsQZG5hbWVuanVtYmYgbWFuaWZlc3RqZXhjbHVzaW9uc4GiZXN0YXJ0GQUVZmxlbmd0aBkeBAAAAj5qdW1iAAAAJ2p1bWRjMmNsABEAEIAAAKoAOJtxA2MycGEuY2xhaW0udjIAAAACD2Nib3KlY2FsZ2ZzaGEyNTZpc2lnbmF0dXJleE1zZWxmI2p1bWJmPS9jMnBhL3VybjpjMnBhOmZkNjdlOTY0LWQwZjYtNDAzMS1iNzIyLWZkZTZiNjZhZmM0ZS9jMnBhLnNpZ25hdHVyZWppbnN0YW5jZUlEeCx4bXA6aWlkOjQ4MWQzM2RhLTNjOWItNDVjMC05ZTdkLWFmMWYxOTdkY2MyZHJjcmVhdGVkX2Fzc2VydGlvbnODomN1cmx4LXNlbGYjanVtYmY9YzJwYS5hc3NlcnRpb25zL2MycGEuaW5ncmVkaWVudC52M2RoYXNoWCDa0N3mB2mTUZ8nvgvMGhiZzeaorhaoZLsGFFRtTjV8wqJjdXJseCpzZWxmI2p1bWJmPWMycGEuYXNzZXJ0aW9ucy9jMnBhLmFjdGlvbnMudjJkaGFzaFggnmNYLNOUKLIHQ7EVi2suAOrlrbgms%2BZOmghwYW1Q4WKiY3VybHgpc2VsZiNqdW1iZj1jMnBhLmFzc2VydGlvbnMvYzJwYS5oYXNoLmRhdGFkaGFzaFggTG6xQN5AwLIigQhXy7GcxAjDsxLuHsBy3oWwywpJAe50Y2xhaW1fZ2VuZXJhdG9yX2luZm%2BjZG5hbWVvQW50aHJvcGljIEZpbGVzZ3ZlcnNpb25lMS4wLjBrc3BlY1ZlcnNpb25lMi40LjAAABA4anVtYgAAAChqdW1kYzJjcwARABCAAACqADibcQNjMnBhLnNpZ25hdHVyZQAAABAIY2JvctKEWQISogEmGCFZAgowggIGMIIBjaADAgECAhRA5aAK7sI50L64g/oGQgU9Z1UTADAKBggqhkjOPQQDAzBJMRcwFQYDVQQKEw5BbnRocm9waWMsIFBCQzEuMCwGA1UEAxMlQW50aHJvcGljIENvbnRlbnQgQ3JlZGVudGlhbHMgUm9vdCBDQTAeFw0yNjA4MDcxODQzNTZaFw0yODA4MDYxOTQzNTZaMEQxFzAVBgNVBAoTDkFudGhyb3BpYywgUEJDMSkwJwYDVQQDEyBBbnRocm9waWMgQ2xhdWRlIENvbnRlbnQgU2lnbmluZzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABJh6CmvLUBgFFNU0vUKlOVtE6djd17L5SuwX0LemFisBM3dkd/3cyjxFA3Qo5S46fX0/ihY0VZ7mfb9KF703t5OjWDBWMA4GA1UdDwEB/wQEAwIHgDAVBgNVHSUEDjAMBgorBgEEAYPoXgIBMAwGA1UdEwEB/wQCMAAwHwYDVR0jBBgwFoAUzlHiBIFOZFsj%2BOPEz5o%2BnMHXXMIwCgYIKoZIzj0EAwMDZwAwZAIwMXMdFJ4BetLLVY7ORuE9noqbbAZOZn/aArXyTwFAZfKrPzxF2vPoJNf1%2BUCdg1XGAjBwX1zd9WGqYkqmL5SFqw1QySjr1zJfpJM9%2B1rdDwSPLMOPOjKuiXjoU/pUUeG9RwmhY3BhZFkNngAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPZYQKCccUto3kd/w4F2KlO/ZQv66aKp0USLh8lqLcVovfB8b07NbMSMYyCyau4wB3IoJpPRG4uMnaDQJGcsq50lr9s=%3C/c2pa:manifest%3E%3C/metadata%3E%3C/svg%3E" alt="Klaro"><b>Klaro</b><span class="pill">BETA</span></div>
 <nav id="nav"></nav>
 <button class="up" onclick="location.hash='#/upload'">⬆ Añadir vídeos</button>
 <div class="foot">Biblioteca local · tus vídeos solo están en este equipo</div>
</aside>
<div id="wrap">
 <header><input class="search" id="q" placeholder="Buscar por título, descripción o etiqueta…"></header>
 <div id="updbar"></div>
 <main id="app"></main>
</div>
<div id="fatal"></div>
<script>
window.addEventListener('error',e=>{const f=document.getElementById('fatal');f.style.display='block';f.textContent='Error en la página: '+e.message+' (línea '+e.lineno+')'});
window.addEventListener('unhandledrejection',e=>{const f=document.getElementById('fatal');f.style.display='block';f.textContent='Error: '+(e.reason&&e.reason.message||e.reason)});
const $=s=>document.querySelector(s);
const el=(t,a={},...k)=>{const e=document.createElement(t);for(const x in a){if(x==='class')e.className=a[x];else if(x.startsWith('on'))e.addEventListener(x.slice(2),a[x]);else if(x==='value')e.value=a[x];else e.setAttribute(x,a[x])}for(const c of k.flat())e.append(c&&c.nodeType?c:document.createTextNode(c??''));return e};
async function api(p,o={}){const h={'X-Klaro':'1',...(o.headers||{})};let b=o.body;if(b&&typeof b==='object'&&!(b instanceof Blob)){h['Content-Type']='application/json';b=JSON.stringify(b)}
 const r=await fetch(p,{...o,headers:h,body:b});let d={};try{d=await r.json()}catch(e){}if(!r.ok)throw new Error(d.error||('Error '+r.status));return d}
const fmt=n=>n>1e9?(n/1e9).toFixed(2)+' GB':n>1e6?(n/1e6).toFixed(1)+' MB':Math.ceil(n/1e3)+' KB';
const date=t=>new Date(t*1000).toLocaleDateString();
const clock=s=>{s=Math.floor(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+':'+String(m).padStart(2,'0'):m)+':'+String(x).padStart(2,'0')};
const resTag=(w,h)=>!w?'':(w>=3800?'4K UHD':w>=2500?'2K':w>=1900?'Full HD':w>=1250?'HD':'SD')+' · '+w+'×'+h;
const enc=encodeURIComponent;
const H2=t=>el('h1',{style:'margin:22px 0 12px;font-size:20px'},t);
const LANGS=[['es','Español'],['en','English'],['ca','Català'],['eu','Euskara'],['gl','Galego'],['fr','Français'],['de','Deutsch'],['it','Italiano'],['pt','Português'],['nl','Nederlands'],['ru','Русский'],['zh','中文'],['ja','日本語'],['ar','العربية']];
const SORTS=[['recent','Más recientes'],['old','Más antiguos'],['title','Título (A-Z)'],['views','Más vistos'],['size','Más pesados'],['played','Visto recientemente']];
const NAV=[['sec','Biblioteca'],['#/','⌂','Inicio'],['#/top','◎','Más vistos'],['#/fav','♥','Favoritos'],['#/later','⏱','Ver más tarde'],['#/colls','☰','Colecciones'],['#/hidden','🔒','Ocultos'],
 ['sec','Crear'],['#/record','⏺','Grabar pantalla o cámara'],['#/prompter','▤','Teleprompter'],
 ['sec','Análisis'],['#/stats','▥','Estadísticas'],
 ['sec','Requieren internet'],['#/beta/ai','✦','Inteligencia artificial',1],['#/beta/live','◉','En vivo y eventos',1],['#/beta/web','⊕','Publicar en internet',1],['#/beta/team','☺','Equipos',1],
 ['sec','Aplicación'],['#/settings','⚙','Configuración']];
const BETA={ai:['Inteligencia artificial','✦',['Traducción multilingüe de vídeos','Generación y traducción de guiones','Edición basada en texto','Respuestas automáticas para espectadores','Transcripciones automáticas y subtítulos generados']],
 live:['En vivo y eventos','◉',['Transmisión en directo','Emisión simultánea a varios destinos','Chat en directo','Webinarios privados']],
 web:['Publicar en internet','⊕',['Código de inserción para sitios web','Restricción por dominios','Formularios para recopilar correos','Análisis de espectadores externos']],
 team:['Equipos','☺',['Revisión compartida con marcas de tiempo','Comentarios de varias personas','Cumplimiento empresarial (RGPD, SOC 2, HIPAA)']]};
let ST={},VER={},SORT='recent';
const openUrl=u=>api('/api/open-url',{method:'POST',body:{url:u}}).catch(()=>{});
function applyAccent(){document.documentElement.style.setProperty('--acc',ST.accent||'#2ee6a6')}
function ask(t){return new Promise(r=>{const m=el('div',{class:'mod'},el('div',{class:'panel'},el('p',{style:'margin-top:0'},t),el('div',{class:'row'},el('button',{class:'del',onclick:()=>{m.remove();r(true)}},'Aceptar'),el('button',{class:'sec',onclick:()=>{m.remove();r(false)}},'Cancelar'))));document.body.append(m)})}
function head(){const r=location.hash||'#/';$('#nav').replaceChildren(...NAV.map(n=>n[0]==='sec'?el('div',{class:'sec'},n[1]):el('a',{href:n[0],class:(r===n[0]?'on':'')+(n[3]?' soon':'')},el('span',{class:'ic'},n[1]),n[2],n[3]?el('span',{class:'bt'},'BETA'):'')))}
function card(v){return el('a',{class:'card',href:'#/v/'+v.id},el('div',{class:'th'},v.thumb?el('img',{src:'/thumb/'+v.id,loading:'lazy'}):'▶',v.fav?el('span',{class:'hb'},'♥'):'',v.locked?el('span',{class:'lk'},'🔒'):'',v.pos>5&&v.dur?el('span',{class:'pg',style:'width:'+Math.min(100,100*v.pos/v.dur)+'%'}):''),
 el('div',{class:'ci'},el('h3',{},v.title),el('div',{class:'meta'},v.views+' vistas · '+fmt(v.size)+' · '+date(v.created)+(v.w>=1250?' · '+(v.w>=3800?'4K':v.w>=2500?'2K':v.w>=1900?'FHD':'HD'):''))))}
async function vlist(base,title,empty,fixed){const app=$('#app');app.replaceChildren(el('h1',{},title));
 const tools=el('div',{class:'row',style:'margin-bottom:18px'});
 if(!fixed){const s=el('select',{onchange:()=>{SORT=s.value;route()}},SORTS.map(([k,n])=>el('option',{value:k,...(k===SORT?{selected:''}:{})},n)));tools.append(s)}
 try{(await api('/api/tags')).tags.slice(0,12).forEach(t=>tools.append(el('a',{class:'tag',href:'#/tag/'+enc(t[0])},'#'+t[0]+' · '+t[1])))}catch(e){}
 if(tools.children.length)app.append(tools);
 try{const d=await api(base+(base.includes('?')?'&':'?')+'sort='+(fixed||SORT)+'&q='+enc($('#q').value.trim()));app.append(d.videos.length?el('div',{class:'grid'},d.videos.map(card)):el('div',{class:'empty'},empty))}catch(e){app.append(el('div',{class:'err'},e.message))}}
function thumbOf(file){return new Promise(res=>{const v=document.createElement('video'),url=URL.createObjectURL(file);let done=false;
 const fin=b=>{if(done)return;done=true;URL.revokeObjectURL(url);res(b)};setTimeout(()=>fin(null),10000);
 v.muted=true;v.preload='metadata';v.src=url;v.onerror=()=>fin(null);
 v.onloadedmetadata=()=>{v.currentTime=Math.min(1,(isFinite(v.duration)?v.duration:2)/10)};
 v.onseeked=()=>{const c=document.createElement('canvas'),w=640;c.width=w;c.height=Math.round(w*(v.videoHeight||360)/(v.videoWidth||640));c.getContext('2d').drawImage(v,0,0,c.width,c.height);c.toBlob(b=>fin(b),'image/jpeg',.82)}})}
async function imgTo(f,type,w){const bm=await createImageBitmap(f),c=document.createElement('canvas');c.width=Math.min(w,bm.width);c.height=Math.round(c.width*bm.height/bm.width);c.getContext('2d').drawImage(bm,0,0,c.width,c.height);return new Promise(r=>c.toBlob(r,type,.85))}
function sendFile(f,bar){return new Promise((ok,ko)=>{const x=new XMLHttpRequest();x.open('POST','/api/upload?filename='+enc(f.name));x.setRequestHeader('X-Klaro','1');
 x.upload.onprogress=e=>{if(bar&&e.lengthComputable)bar.style.width=(100*e.loaded/e.total)+'%'};
 x.onload=()=>{let d={};try{d=JSON.parse(x.responseText)}catch(e){}x.status===200?ok(d.id):ko(new Error(d.error||'Error al añadir'))};
 x.onerror=()=>ko(new Error('Fallo de conexión'));x.send(f)})}
async function saveBlob(blob,title,bar){const id=await sendFile(new File([blob],title.replace(/[\\/:*?"<>|]/g,'_')+'.webm',{type:'video/webm'}),bar);const t=await thumbOf(blob);if(t)try{await api('/api/video/'+id+'/thumb',{method:'POST',body:t})}catch(e){}return id}
async function fixWebm(blob,dur){try{const b=new Uint8Array(await blob.arrayBuffer());
 const find=(a,from,to)=>{for(let k=from;k<Math.min(b.length-4,to);k++)if(b[k]===a[0]&&b[k+1]===a[1]&&b[k+2]===a[2]&&b[k+3]===a[3])return k;return -1};
 const sg=find([0x18,0x53,0x80,0x67],0,128);if(sg<0||b[sg+4]!==0x01)return blob;for(let k=5;k<12;k++)if(b[sg+k]!==0xFF)return blob;
 const i=find([0x15,0x49,0xA9,0x66],0,4096);if(i<0)return blob;const sz=b[i+4];if(!(sz&0x80))return blob;
 const old=sz&0x7F,nw=old+11;if(nw>126)return blob;for(let k=i+5;k<i+4+old;k++)if(b[k]===0x44&&b[k+1]===0x89)return blob;
 const d=new Uint8Array(11);d[0]=0x44;d[1]=0x89;d[2]=0x88;new DataView(d.buffer).setFloat64(3,dur*1000);
 const out=new Uint8Array(b.length+11);out.set(b.subarray(0,i+5),0);out[i+4]=0x80|nw;out.set(d,i+5);out.set(b.subarray(i+5),i+16);return new Blob([out],{type:'video/webm'})}catch(e){return blob}}
function upload(){
 const msg=el('div',{class:'err'}),bar=el('i'),file=el('input',{type:'file',multiple:'',accept:'.mp4,.m4v,.webm,.ogv,.mov,video/*',style:'display:none'});
 const drop=el('div',{class:'drop',onclick:()=>file.click()},'Arrastra vídeos aquí o haz clic para elegirlos',el('br'),el('span',{class:'meta'},'MP4, WebM, OGV, MOV · puedes elegir varios · se reproducen en su calidad original (HD, Full HD, 4K…)'));
 const start=async fs=>{fs=[...fs];if(!fs.length)return;let last=null;
  for(const [i,f] of fs.entries()){msg.style.color='var(--mute)';msg.textContent='Añadiendo '+(i+1)+' de '+fs.length+': '+f.name+'…';bar.style.width='0';
   try{last=await sendFile(f,bar);const b=await thumbOf(f);if(b)try{await api('/api/video/'+last+'/thumb',{method:'POST',body:b})}catch(e){}}
   catch(e){msg.style.color='var(--acc2)';msg.textContent=f.name+': '+e.message;return}}
  location.hash=fs.length===1?'#/v/'+last:'#/'};
 file.onchange=()=>start(file.files);
 ['dragover','dragenter'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.add('hv')}));
 ['dragleave','drop'].forEach(n=>drop.addEventListener(n,e=>{e.preventDefault();drop.classList.remove('hv')}));
 drop.addEventListener('drop',e=>start(e.dataTransfer.files));
 $('#app').replaceChildren(el('h1',{},'Añadir vídeos'),drop,file,el('div',{class:'bar'},bar),msg)}
function betaBtn(t){return el('button',{class:'sec beta',disabled:'',title:'No disponible en la beta: necesita internet'},t,el('span',{class:'bt'},'BETA'))}
async function watch(id){const app=$('#app');app.replaceChildren();
 try{const D=await api('/api/video/'+id),v=D.video;document.title=v.title+' · Klaro';
  if(!D.unlocked){const pw=el('input',{type:'password',placeholder:'Contraseña'}),m=el('div',{class:'err'});
   const go=async()=>{try{await api('/api/video/'+id+'/unlock',{method:'POST',body:{password:pw.value}});watch(id)}catch(e){m.textContent=e.message}};pw.onkeydown=e=>{if(e.key==='Enter')go()};
   app.append(el('div',{class:'box'},el('h1',{},'🔒 '+v.title),el('p',{class:'meta'},'Este vídeo está protegido con contraseña.'),pw,el('div',{style:'margin-top:12px'},el('button',{onclick:go},'Desbloquear')),m));return}
  const ks=(await api('/api/colls')).colls,ivs=[];window.cleanup=()=>ivs.forEach(clearInterval);
  const pl=el('video',{class:'pl',playsinline:'',src:'/media/'+id,poster:v.thumb?'/thumb/'+id+'?t='+Date.now():''});
  if(ST.controls!=='0')pl.setAttribute('controls','');pl.loop=ST.loop==='1';pl.autoplay=ST.autoplay==='1';pl.defaultPlaybackRate=+ST.speed||1;pl.playbackRate=+ST.speed||1;
  D.subs.forEach((s,i)=>pl.append(el('track',{kind:'subtitles',src:'/subs/'+id+'/'+s.lang,srclang:s.lang,label:s.label,...(i===0?{default:''}:{})})));
  const cta=el('div',{class:'cta',style:'display:none'});
  const wrap=el('div',{class:'pw'},pl,ST.logo&&ST.logo_on==='1'?el('img',{class:'wm wm-'+ST.logo_pos,src:'/logo?t='+Date.now(),style:'opacity:'+(+ST.logo_opacity/100)}):'',cta);
  let counted=false,last=0,acc=0,shown=-1;
  const save=z=>api('/api/video/'+id+'/pos',{method:'POST',body:{pos:z?0:pl.currentTime,dur:isFinite(pl.duration)?pl.duration:0,w:pl.videoWidth||0,h:pl.videoHeight||0,watched:(()=>{const a=acc;acc=0;return a})()},keepalive:true}).catch(()=>{});
  pl.onplay=()=>{if(!counted){counted=true;api('/api/video/'+id+'/view',{method:'POST'}).catch(()=>{})}};
  pl.onloadedmetadata=()=>{if(v.pos>5&&pl.duration&&v.pos<pl.duration-5)pl.currentTime=v.pos;if(!v.w&&pl.videoWidth)save()};
  pl.ontimeupdate=()=>{const n=Date.now();if(n-last>5000){last=n;save()}};pl.onpause=()=>save();pl.onended=()=>save(true);
  ivs.push(setInterval(()=>{if(!pl.paused&&!pl.ended)acc++},1000));
  const mkRows=[];
  ivs.push(setInterval(()=>{const t=pl.currentTime,c=D.ctas.find(x=>t>=x.t_start&&t<=x.t_end);
   if(c&&shown!==c.id){shown=c.id;cta.replaceChildren(el('span',{},c.text),el('button',{onclick:()=>openUrl(c.url)},c.label));cta.style.display='flex'}else if(!c&&shown!==-1){shown=-1;cta.style.display='none'}
   let cur=null;D.markers.forEach(m=>{if(m.kind==='chapter'&&m.t<=t)cur=m.id});mkRows.forEach(([r,mid])=>r.classList.toggle('cur',mid===cur))},300));
  window.onkeydown=e=>{if(/INPUT|TEXTAREA|SELECT/.test(e.target.tagName))return;const k=e.key;
   if(k===' '){e.preventDefault();pl.paused?pl.play():pl.pause()}else if(k==='ArrowRight')pl.currentTime+=5;else if(k==='ArrowLeft')pl.currentTime-=5;else if(k==='l')pl.currentTime+=10;else if(k==='j')pl.currentTime-=10;
   else if(k==='f')document.fullscreenElement?document.exitFullscreen():wrap.requestFullscreen();else if(k==='m')pl.muted=!pl.muted};
  const msg=el('span',{class:'meta'}),again=async()=>{await save();watch(id)};
  const tog=(k,btn,on,off)=>async()=>{await api('/api/video/'+id+'/'+k,{method:'POST'});v[k]=!v[k];btn.className=v[k]?'':'sec';btn.textContent=v[k]?on:off};
  const fav=el('button',{class:v.fav?'':'sec'},v.fav?'♥ Favorito':'♡ Favorito'),later=el('button',{class:v.later?'':'sec'},v.later?'✓ Ver más tarde':'⏱ Ver más tarde');
  fav.onclick=tog('fav',fav,'♥ Favorito','♡ Favorito');later.onclick=tog('later',later,'✓ Ver más tarde','⏱ Ver más tarde');
  const lp=el('button',{class:pl.loop?'':'sec',onclick:()=>{pl.loop=!pl.loop;lp.className=pl.loop?'':'sec'}},'⟲ Repetir');
  const sp=el('select',{onchange:()=>{pl.playbackRate=+sp.value}},[0.5,0.75,1,1.25,1.5,2].map(x=>el('option',{value:x,...(x===(+ST.speed||1)?{selected:''}:{})},x+'x')));
  const col=el('select',{onchange:async()=>{if(col.value){await api('/api/colls/'+col.value+'/add/'+id,{method:'POST'});col.options[0].textContent='Añadido ✓';col.value=''}}},el('option',{value:''},'+ Colección'),ks.map(k=>el('option',{value:k.id},k.name)));
  // capítulos y notas de revisión
  const lab=el('input',{placeholder:'Título del capítulo o texto de la nota'}),kind=el('select',{},el('option',{value:'chapter'},'Capítulo'),el('option',{value:'note'},'Nota de revisión'));
  const mk=el('div',{class:'panel'},el('b',{},'Capítulos y notas de revisión'),el('p',{class:'meta',style:'margin:4px 0 10px'},'Marca momentos del vídeo con su tiempo. Los capítulos se resaltan mientras se reproduce.'),
   el('div',{class:'row',style:'flex-wrap:nowrap'},lab,kind,el('button',{style:'white-space:nowrap',onclick:async()=>{if(!lab.value.trim())return;await api('/api/video/'+id+'/markers',{method:'POST',body:{t:pl.currentTime,label:lab.value,kind:kind.value}});again()}},'+ Aquí')),
   D.markers.map(k=>{const r=el('div',{class:'mk'},el('a',{class:'t',onclick:()=>{pl.currentTime=k.t;pl.play()}},clock(k.t)),
     k.kind==='note'?el('input',{type:'checkbox',...(k.done?{checked:''}:{}),title:'Resuelta',onchange:()=>api('/api/markers/'+k.id+'/done',{method:'POST'})}):el('span',{class:'tag'},'cap.'),
     el('span',{style:'flex:1'},k.label||'Marcador'),el('a',{class:'meta',style:'cursor:pointer',onclick:async()=>{await api('/api/markers/'+k.id,{method:'DELETE'});again()}},'Quitar'));mkRows.push([r,k.kind==='chapter'?k.id:-2]);return r}));
  // tarjetas con llamada a la acción
  const ct=el('input',{placeholder:'Texto de la tarjeta'}),cl=el('input',{placeholder:'Texto del botón'}),cu=el('input',{placeholder:'https://…'}),c0=el('input',{type:'number',step:'0.5',min:'0',style:'width:90px'}),c1=el('input',{type:'number',step:'0.5',min:'0',style:'width:90px'});
  const ctaP=el('div',{class:'panel'},el('b',{},'Tarjetas con llamada a la acción'),el('p',{class:'meta',style:'margin:4px 0 10px'},'Muestra una tarjeta con un botón (abre un enlace) durante el tramo que elijas.'),
   ct,el('div',{class:'row',style:'margin-top:8px;flex-wrap:nowrap'},cl,cu),
   el('div',{class:'row',style:'margin-top:8px'},el('span',{class:'meta'},'Desde (s)'),c0,el('button',{class:'sec',onclick:()=>{c0.value=pl.currentTime.toFixed(1)}},'Ahora'),el('span',{class:'meta'},'hasta (s)'),c1,el('button',{class:'sec',onclick:()=>{c1.value=pl.currentTime.toFixed(1)}},'Ahora'),
    el('button',{onclick:async()=>{try{await api('/api/video/'+id+'/ctas',{method:'POST',body:{text:ct.value,label:cl.value,url:cu.value,t_start:c0.value,t_end:c1.value}});again()}catch(e){msg.textContent=e.message}}},'Añadir')),
   D.ctas.map(c=>el('div',{class:'mk'},el('a',{class:'t',onclick:()=>{pl.currentTime=c.t_start;pl.play()}},clock(c.t_start)+'–'+clock(c.t_end)),el('span',{style:'flex:1'},c.text+' → '+c.label),el('a',{class:'meta',style:'cursor:pointer',onclick:async()=>{await api('/api/ctas/'+c.id,{method:'DELETE'});again()}},'Quitar'))));
  // subtítulos en varios idiomas
  const lang=el('select',{},LANGS.map(([k,n])=>el('option',{value:k},n))),sf=el('input',{type:'file',accept:'.vtt,.srt',style:'display:none',onchange:async()=>{const f=sf.files[0];if(!f)return;const n=LANGS.find(x=>x[0]===lang.value)[1];await api('/api/video/'+id+'/subs?lang='+lang.value+'&label='+enc(n),{method:'POST',body:f});again()}});
  const subP=el('div',{class:'panel'},el('b',{},'Subtítulos'),el('p',{class:'meta',style:'margin:4px 0 10px'},'Añade archivos .srt o .vtt en cada idioma. Actívalos desde el botón CC del reproductor.'),
   el('div',{class:'row'},lang,el('button',{class:'sec',onclick:()=>sf.click()},'💬 Elegir archivo…'),sf,betaBtn('✦ Generar con IA')),
   D.subs.map(s=>el('div',{class:'mk'},el('span',{class:'tag'},s.lang),el('span',{style:'flex:1'},s.label),el('a',{class:'meta',style:'cursor:pointer',onclick:async()=>{await api('/api/video/'+id+'/subs?lang='+s.lang,{method:'DELETE'});again()}},'Quitar'))));
  // herramientas
  const imf=el('input',{type:'file',accept:'image/*',style:'display:none',onchange:async()=>{const f=imf.files[0];if(!f)return;await api('/api/video/'+id+'/thumb',{method:'POST',body:await imgTo(f,'image/jpeg',640)});msg.textContent='Miniatura actualizada ✓'}});
  const grab=async()=>{const c=document.createElement('canvas');c.width=640;c.height=Math.round(640*(pl.videoHeight||360)/(pl.videoWidth||640));c.getContext('2d').drawImage(pl,0,0,c.width,c.height);
   const b=await new Promise(r=>c.toBlob(r,'image/jpeg',.85));await api('/api/video/'+id+'/thumb',{method:'POST',body:b});pl.poster=c.toDataURL('image/jpeg');msg.textContent='Miniatura actualizada ✓'};
  const tools=el('div',{class:'panel'},el('b',{},'Herramientas'),el('div',{class:'row',style:'margin-top:10px'},
   el('a',{class:'btn',href:'#/edit/'+id},'✂ Recortar y editar'),el('button',{class:'sec',onclick:grab},'🖼 Fotograma como miniatura'),el('button',{class:'sec',onclick:()=>imf.click()},'🖼 Miniatura desde imagen'),
   el('a',{class:'btn sec',href:'/media/'+id+'?dl=1',download:''},'⬇ Guardar copia'),imf),el('div',{style:'margin-top:8px'},msg),
   el('p',{class:'meta',style:'margin-bottom:0'},'Atajos: espacio = pausa · ← → = ±5 s · J / L = ±10 s · F = pantalla completa · M = silencio'));
  // edición, privacidad
  const t=el('input',{value:v.title}),d=el('textarea',{rows:'3'},v.description),tg=el('input',{value:v.tags.join(', '),placeholder:'viajes, familia, tutorial…'}),hid=el('input',{type:'checkbox',...(v.hidden?{checked:''}:{})}),pw=el('input',{type:'password',placeholder:v.locked?'Nueva contraseña (vacío = no cambiar)':'Poner contraseña (mín. 4)'}),m2=el('span',{class:'meta'});
  const saveEdit=async()=>{try{const b={title:t.value,description:d.value,tags:tg.value,hidden:hid.checked};if(pw.value)b.password=pw.value;await save();await api('/api/video/'+id,{method:'PUT',body:b});watch(id)}catch(e){m2.textContent=e.message}};
  app.append(el('div',{class:'watch'},wrap,
   el('div',{class:'panel'},el('h1',{style:'margin-bottom:6px'},(v.locked?'🔒 ':'')+v.title),el('div',{class:'meta'},v.views+' vistas · '+fmt(v.size)+' · '+date(v.created)+(v.dur?' · '+clock(v.dur):'')),
    el('div',{class:'row',style:'margin-top:8px'},v.w?el('span',{class:'tag res'},resTag(v.w,v.h)):'',v.hidden?el('span',{class:'tag'},'Oculto'):'',v.tags.map(x=>el('a',{class:'tag',href:'#/tag/'+enc(x)},'#'+x))),v.description?el('p',{style:'white-space:pre-wrap'},v.description):''),
   el('div',{class:'panel'},el('div',{class:'row'},fav,later,lp,col,sp,betaBtn('⊕ Código para insertar en web'))),mk,ctaP,subP,tools,
   el('div',{class:'panel'},el('b',{},'Editar y privacidad'),el('label',{class:'lb'},'Título'),t,el('label',{class:'lb'},'Descripción'),d,el('label',{class:'lb'},'Etiquetas (separadas por comas)'),tg,
    el('label',{class:'lb',style:'color:var(--text)'},hid,' Ocultar de la biblioteca (solo aparece en «Ocultos»)'),el('label',{class:'lb'},'Contraseña del vídeo'),pw,
    el('div',{class:'row',style:'margin-top:14px'},el('button',{onclick:saveEdit},'Guardar'),
     v.locked?el('button',{class:'sec',onclick:async()=>{await api('/api/video/'+id,{method:'PUT',body:{password:''}});watch(id)}},'Quitar contraseña'):'',
     el('button',{class:'del',onclick:async()=>{if(await ask('¿Eliminar este vídeo para siempre? Se borrará el archivo de tu biblioteca.')){pl.pause();window.onkeydown=null;await api('/api/video/'+id,{method:'DELETE'});location.hash='#/'}}},'Eliminar'),m2))))
 }catch(e){app.append(el('div',{class:'empty'},'Vídeo no disponible. ',el('a',{href:'#/',style:'color:var(--acc)'},'Volver')))}}
async function editView(id){const D=await api('/api/video/'+id);if(!D.unlocked){location.hash='#/v/'+id;return}const v=D.video,app=$('#app');app.replaceChildren();
 const pl=el('video',{class:'pl',controls:'',src:'/media/'+id,style:'border-radius:14px'}),num=(val,w)=>el('input',{type:'number',step:'0.1',min:'0',value:val,style:'width:'+(w||100)+'px'});
 const s0=num(0),s1=num('');const now=i=>()=>{i.value=pl.currentTime.toFixed(1)};
 const tx=el('input',{placeholder:'Texto que aparecerá sobre el vídeo (opcional)'}),t0=num(0,80),t1=num(5,80),tp=el('select',{},[['bottom','Abajo'],['center','Centro'],['top','Arriba']].map(([k,n])=>el('option',{value:k},n))),
  ts=el('select',{},[['0.06','Pequeño'],['0.09','Mediano'],['0.13','Grande']].map(([k,n],i)=>el('option',{value:k,...(i===1?{selected:''}:{})},n))),tc=el('input',{type:'color',value:'#ffffff'});
 const mf=el('input',{type:'file',accept:'audio/*'}),mv=el('input',{type:'range',min:'0',max:'100',value:'60'}),ov=el('input',{type:'range',min:'0',max:'100',value:'100'}),mx=el('select',{},[['0','Calidad original'],['720','Máximo 720p (más rápido)']].map(([k,n])=>el('option',{value:k},n)));
 const bar=el('i'),msg=el('div',{class:'meta',style:'margin-top:10px'}),title=el('input',{value:v.title+' (editado)'});let job=null;
 const go=el('button',{onclick:async()=>{go.disabled=true;cancel.style.display='inline-block';job={};msg.style.color='var(--mute)';bar.style.width='0';msg.textContent='Exportando en tiempo real: mantén esta ventana visible hasta que termine…';
  try{const r=await exportEdit(job,{id,start:+s0.value||0,end:+s1.value||0,text:tx.value.trim(),ts:+t0.value,te:+t1.value,pos:tp.value,size:+ts.value,color:tc.value,music:mf.files[0]||null,mv:mv.value/100,ov:ov.value/100,max:+mx.value},bar);
   if(!r){msg.textContent='Exportación cancelada.'}else{msg.textContent='Guardando en tu biblioteca…';const nid=await saveBlob(r.blob,title.value||'Editado',null);location.hash='#/v/'+nid;return}}
  catch(e){msg.style.color='var(--acc2)';msg.textContent=e.message}go.disabled=false;cancel.style.display='none'}},'Exportar como vídeo nuevo'),cancel=el('button',{class:'sec',style:'display:none',onclick:()=>{if(job&&job.cancel)job.cancel()}},'Cancelar');
 const L=(t,c)=>el('label',{class:'lb'},t,c);
 app.append(el('h1',{},'Recortar y editar'),el('p',{class:'meta',style:'margin-top:-8px'},v.title+' · el original no se modifica: se crea un vídeo nuevo.'),pl,
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'1. Recorte'),el('div',{class:'row',style:'margin-top:10px'},el('span',{class:'meta'},'Inicio (s)'),s0,el('button',{class:'sec',onclick:now(s0)},'Usar posición actual'),el('span',{class:'meta'},'Fin (s)'),s1,el('button',{class:'sec',onclick:now(s1)},'Usar posición actual'),el('span',{class:'meta'},'(fin vacío = hasta el final)'))),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'2. Texto sobre el vídeo'),el('div',{style:'margin-top:10px'},tx),el('div',{class:'row',style:'margin-top:8px'},el('span',{class:'meta'},'Desde (s)'),t0,el('span',{class:'meta'},'hasta (s)'),t1,tp,ts,tc)),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'3. Música'),el('div',{style:'margin-top:10px'},mf),el('div',{class:'row',style:'margin-top:8px'},el('span',{class:'meta'},'Volumen música'),mv,el('span',{class:'meta'},'Volumen original'),ov)),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'4. Exportar'),L('Título del nuevo vídeo',title),L('Calidad',mx),el('div',{class:'row',style:'margin-top:14px'},go,cancel),el('div',{class:'bar'},bar),msg),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'Más edición'),el('div',{class:'row',style:'margin-top:10px'},betaBtn('✦ Edición basada en texto (IA)'),betaBtn('✦ Transcripción automática'),betaBtn('✦ Traducción de vídeo'))))}
async function exportEdit(job,o,bar){
 const v=document.createElement('video');v.src='/media/'+o.id;v.preload='auto';v.playsInline=true;
 await new Promise((ok,ko)=>{v.onloadedmetadata=ok;v.onerror=()=>ko(new Error('No se pudo abrir el vídeo'))});
 const end=Math.min(o.end>0?o.end:v.duration,v.duration),start=Math.max(0,o.start);if(!(end>start))throw new Error('El fin debe ser mayor que el inicio');
 let W=v.videoWidth,H=v.videoHeight;if(o.max&&H>o.max){W=Math.round(W*o.max/H);H=o.max}W-=W%2;H-=H%2;
 const cv=document.createElement('canvas');cv.width=W;cv.height=H;const g=cv.getContext('2d');
 const ac=new AudioContext(),dest=ac.createMediaStreamDestination(),g1=ac.createGain();g1.gain.value=o.ov;ac.createMediaElementSource(v).connect(g1);g1.connect(dest);
 let mus=null;if(o.music){mus=new Audio(URL.createObjectURL(o.music));mus.loop=true;const g2=ac.createGain();g2.gain.value=o.mv;ac.createMediaElementSource(mus).connect(g2);g2.connect(dest)}
 const cs=cv.captureStream(30);dest.stream.getAudioTracks().forEach(t=>cs.addTrack(t));
 const mime=['video/webm;codecs=vp9,opus','video/webm;codecs=vp8,opus','video/webm'].find(m=>MediaRecorder.isTypeSupported(m));
 const rec=new MediaRecorder(cs,{mimeType:mime,videoBitsPerSecond:8e6}),chunks=[];rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
 v.currentTime=start;await new Promise(r=>{v.onseeked=r});await ac.resume();
 let stop=false;job.cancel=()=>{stop=true};const done=new Promise(r=>{rec.onstop=r});
 rec.start(500);await v.play();if(mus)mus.play();
 await new Promise(res=>{const tick=()=>{if(stop||v.ended||v.currentTime>=end){res();return}
  g.drawImage(v,0,0,W,H);
  if(o.text&&v.currentTime>=o.ts&&v.currentTime<=o.te){g.font='bold '+Math.round(H*o.size)+'px system-ui,sans-serif';g.textAlign='center';g.fillStyle=o.color;g.shadowColor='rgba(0,0,0,.75)';g.shadowBlur=8;g.fillText(o.text,W/2,o.pos==='top'?H*0.13:o.pos==='center'?H*0.52:H*0.92,W*0.92);g.shadowBlur=0}
  bar.style.width=(100*(v.currentTime-start)/(end-start))+'%';setTimeout(tick,33)};tick()});
 v.pause();if(mus)mus.pause();rec.stop();await done;ac.close();
 if(stop)return null;return{blob:await fixWebm(new Blob(chunks,{type:'video/webm'}),end-start)}}
function prompterBox(text,size,speed,mirror){const box=el('div',{class:'tp'}),inner=el('div',{class:'tpi',style:'font-size:'+size+'px'+(mirror?';transform:scaleX(-1)':'')},text||'Escribe tu guion en el cuadro de texto…');box.append(inner);
 let iv=null,pos=0,run=false;const o={el:box,get running(){return run},start(){if(run)return;run=true;iv=setInterval(()=>{pos+=o.speed*0.04;box.scrollTop=pos},40)},stop(){run=false;clearInterval(iv)},reset(){pos=0;box.scrollTop=0},speed,setSize(s){inner.style.fontSize=s+'px'}};return o}
function prompterView(){const app=$('#app');
 const txt=el('textarea',{rows:'12',placeholder:'Pega aquí tu guion…'},ST.prompter_text||''),size=el('input',{type:'range',min:'24',max:'120',value:'56'}),speed=el('input',{type:'range',min:'10',max:'200',value:'50'}),mir=el('input',{type:'checkbox'});
 txt.onchange=()=>api('/api/settings',{method:'POST',body:{prompter_text:txt.value}}).then(()=>{ST.prompter_text=txt.value});
 const open=()=>{const p=prompterBox(txt.value,+size.value,+speed.value,mir.checked),cnt=el('span',{class:'meta'},'Velocidad '+speed.value);
  const bar=el('div',{class:'tpbar'},el('button',{onclick:()=>{p.running?p.stop():p.start();pb.textContent=p.running?'⏸ Pausa':'▶ Seguir'}},'⏸ Pausa'),el('button',{class:'sec',onclick:()=>{p.speed=Math.max(5,p.speed-10);cnt.textContent='Velocidad '+p.speed}},'− Más lento'),el('button',{class:'sec',onclick:()=>{p.speed+=10;cnt.textContent='Velocidad '+p.speed}},'+ Más rápido'),
   el('button',{class:'sec',onclick:()=>p.reset()},'↺ Inicio'),cnt,el('span',{style:'flex:1'}),el('button',{class:'del',onclick:close},'✕ Cerrar (Esc)')),pb=bar.firstChild;
  const full=el('div',{class:'tpfull'},bar,p.el);document.body.append(full);setTimeout(()=>p.start(),1200);
  const key=e=>{if(e.key==='Escape')close();else if(e.key===' '){e.preventDefault();pb.click()}else if(e.key==='ArrowUp')p.speed+=10;else if(e.key==='ArrowDown')p.speed=Math.max(5,p.speed-10)};
  function close(){p.stop();full.remove();removeEventListener('keydown',key)}addEventListener('keydown',key)};
 app.replaceChildren(el('h1',{},'Teleprompter'),el('div',{class:'panel'},txt,el('div',{class:'row',style:'margin-top:12px'},el('span',{class:'meta'},'Tamaño'),size,el('span',{class:'meta'},'Velocidad'),speed,el('label',{class:'meta'},mir,' Espejo')),
  el('div',{style:'margin-top:14px'},el('button',{onclick:open},'▶ Iniciar teleprompter')),el('p',{class:'meta',style:'margin-bottom:0'},'El texto se desplaza solo. Espacio = pausa · ↑ ↓ = velocidad · Esc = cerrar. Tu guion se guarda automáticamente.')))}
function recordView(){const app=$('#app');let stream=null,rec=null,chunks=[],t0=0,tick=null,prom=null;
 const msg=el('div',{class:'err'}),mode=el('select',{},el('option',{value:'screen'},'Pantalla'),el('option',{value:'cam'},'Cámara web')),mic=el('input',{type:'checkbox',checked:''}),useTp=el('input',{type:'checkbox'}),
  txt=el('textarea',{rows:'5',placeholder:'Guion del teleprompter (opcional)'},ST.prompter_text||''),spd=el('input',{type:'range',min:'10',max:'200',value:'45'}),
  prev=el('video',{class:'pl',muted:'',playsinline:'',style:'border-radius:14px;min-height:200px'}),stage=el('div',{class:'pw'},prev),timer=el('span',{class:'meta'});
 const bAct=el('button',{onclick:activate},'1. Activar'),bRec=el('button',{style:'display:none',onclick:startRec},'● Grabar'),bStop=el('button',{class:'del',style:'display:none',onclick:stopRec},'■ Detener');
 async function activate(){msg.textContent='';try{
   if(!navigator.mediaDevices)throw new Error('Esta ventana no permite acceder a la cámara o la pantalla.');
   if(mode.value==='screen'){stream=await navigator.mediaDevices.getDisplayMedia({video:true,audio:true});
    if(mic.checked)try{const m=await navigator.mediaDevices.getUserMedia({audio:true}),ac=new AudioContext(),d=ac.createMediaStreamDestination();[stream,m].forEach(s=>{if(s.getAudioTracks().length)ac.createMediaStreamSource(s).connect(d)});stream=new MediaStream([...stream.getVideoTracks(),...d.stream.getAudioTracks()])}catch(e){}}
   else stream=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1920},height:{ideal:1080}},audio:mic.checked});
   prev.srcObject=stream;prev.play();bAct.style.display='none';bRec.style.display='inline-block';
  }catch(e){msg.textContent=(e&&e.name==='NotAllowedError'?'Permiso denegado. Permite el acceso cuando Windows o la ventana te lo pidan.':'No se pudo activar: '+(e&&e.message||e))}}
 function startRec(){chunks=[];const mime=['video/webm;codecs=vp9,opus','video/webm;codecs=vp8,opus','video/webm'].find(m=>MediaRecorder.isTypeSupported(m));
  rec=new MediaRecorder(stream,{mimeType:mime,videoBitsPerSecond:8e6});rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};rec.onstop=finish;rec.start(1000);t0=Date.now();
  if(useTp.checked&&txt.value.trim()){prom=prompterBox(txt.value,40,+spd.value,false);prom.el.style.cssText+=';position:absolute;left:0;right:0;top:0;height:42%;background:rgba(0,0,0,.55)';stage.append(prom.el);setTimeout(()=>prom&&prom.start(),1500)}
  bRec.style.display='none';bStop.style.display='inline-block';tick=setInterval(()=>{timer.replaceChildren(el('span',{class:'rec'}),clock((Date.now()-t0)/1000))},500)}
 function stopRec(){clearInterval(tick);if(prom){prom.stop();prom.el.remove();prom=null}rec.stop();stream.getTracks().forEach(t=>t.stop());prev.srcObject=null;bStop.style.display='none';timer.textContent='Procesando…'}
 async function finish(){const dur=(Date.now()-t0)/1000,blob=await fixWebm(new Blob(chunks,{type:'video/webm'}),dur),url=URL.createObjectURL(blob);
  const title=el('input',{value:'Grabación '+new Date().toLocaleString()}),bar=el('i'),m=el('div',{class:'meta',style:'margin-top:10px'});
  stage.replaceChildren(el('video',{class:'pl',controls:'',src:url,style:'border-radius:14px'}));timer.textContent='';
  res.replaceChildren(el('div',{class:'panel'},el('b',{},'Grabación lista · '+clock(dur)),el('label',{class:'lb'},'Título'),title,el('div',{class:'row',style:'margin-top:14px'},
   el('button',{onclick:async()=>{m.textContent='Guardando…';try{const id=await saveBlob(blob,title.value||'Grabación',bar);location.hash='#/v/'+id}catch(e){m.textContent=e.message}}},'Guardar en mi biblioteca'),
   el('button',{class:'sec',onclick:()=>{URL.revokeObjectURL(url);route()}},'Descartar')),el('div',{class:'bar'},bar),m))}
 const res=el('div',{style:'margin-top:16px'});window.cleanup=()=>{clearInterval(tick);if(stream)stream.getTracks().forEach(t=>t.stop())};
 app.replaceChildren(el('h1',{},'Grabar pantalla o cámara'),el('div',{class:'panel',style:'margin-bottom:16px'},el('div',{class:'row'},mode,el('label',{class:'meta'},mic,' Micrófono'),el('label',{class:'meta'},useTp,' Mostrar teleprompter al grabar'),el('span',{class:'meta'},'Velocidad'),spd),
  el('div',{style:'margin-top:10px'},txt)),stage,el('div',{class:'row',style:'margin-top:14px'},bAct,bRec,bStop,timer),msg,res)}
async function colls(){const app=$('#app'),n=el('input',{placeholder:'Nueva colección…'});
 app.replaceChildren(el('h1',{},'Colecciones'),el('div',{class:'row',style:'margin-bottom:18px;flex-wrap:nowrap'},n,el('button',{onclick:async()=>{if(n.value.trim()){await api('/api/colls',{method:'POST',body:{name:n.value}});colls()}}},'Crear')));
 const d=await api('/api/colls');app.append(d.colls.length?el('div',{class:'grid'},d.colls.map(k=>el('div',{class:'card ci'},el('a',{href:'#/coll/'+k.id},el('h3',{},k.name)),el('div',{class:'meta'},k.n+' vídeos'),
  el('button',{class:'del',style:'margin-top:8px',onclick:async()=>{if(await ask('¿Borrar la colección? Los vídeos no se borran.')){await api('/api/colls/'+k.id,{method:'DELETE'});colls()}}},'Borrar')))):el('div',{class:'empty'},'No tienes colecciones.'))}
async function collView(id){const k=(await api('/api/colls')).colls.find(x=>x.id==id);await vlist('/api/videos?coll='+id,k?k.name:'Colección','Colección vacía. Añade vídeos desde su página.')}
const chart=(days,i,unit)=>{const mx=Math.max(1,...days.map(x=>x[i]));return el('div',{class:'chart'},days.map(x=>el('div',{title:x[0]+': '+x[i]+unit,style:'height:'+(4+96*x[i]/mx)+'px'})))};
async function stats(){const d=await api('/api/stats');
 $('#app').replaceChildren(el('h1',{},'Estadísticas y análisis'),el('div',{class:'grid',style:'margin-bottom:20px'},[['Vídeos',d.videos],['Reproducciones',d.views],['Tiempo visto',d.minutes>=60?(d.minutes/60).toFixed(1)+' h':d.minutes+' min'],['Favoritos',d.favs],['Espacio usado',fmt(d.size)]].map(([t,n])=>el('div',{class:'panel'},el('div',{class:'meta'},t),el('h1',{style:'margin:0'},n)))),
  el('div',{class:'two'},el('div',{class:'panel'},el('b',{},'Reproducciones · 14 días'),chart(d.days,1,' vistas')),el('div',{class:'panel'},el('b',{},'Minutos vistos · 14 días'),chart(d.days,2,' min'))),
  el('div',{class:'row',style:'margin:22px 0 12px'},el('h1',{style:'margin:0;font-size:20px;flex:1'},'Rendimiento por vídeo'),el('a',{class:'btn sec',href:'/api/report.csv',download:'klaro-informe.csv'},'⬇ Exportar informe (CSV)')),
  d.table.length?el('div',{class:'panel',style:'overflow:auto'},el('table',{},el('tr',{},['Vídeo','Vistas','Minutos vistos','Resolución','Tamaño'].map(h=>el('th',{},h))),d.table.map(r=>el('tr',{},el('td',{},el('a',{href:'#/v/'+r.id,style:'color:var(--acc)'},(r.locked?'🔒 ':'')+r.title)),el('td',{},r.views),el('td',{},r.minutes),el('td',{},r.w?r.w+'×'+r.h:'—'),el('td',{},fmt(r.size)))))):el('div',{class:'empty'},'Aún no hay datos.'))}
async function settings(){const d=ST,app=$('#app');
 const put=(k,v)=>{ST[k]=String(v);return api('/api/settings',{method:'POST',body:{[k]:String(v)}})};
 const chk=(k,label)=>{const c=el('input',{type:'checkbox',...(ST[k]==='1'?{checked:''}:{}),onchange:()=>put(k,c.checked?'1':'0')});return el('label',{class:'lb',style:'color:var(--text)'},c,' '+label)};
 const acc=el('input',{type:'color',value:ST.accent,oninput:()=>{ST.accent=acc.value;applyAccent()},onchange:()=>put('accent',acc.value)});
 const spd=el('select',{onchange:()=>put('speed',spd.value)},[0.5,0.75,1,1.25,1.5,2].map(x=>el('option',{value:x,...(x===(+ST.speed||1)?{selected:''}:{})},x+'x')));
 const pos=el('select',{onchange:()=>put('logo_pos',pos.value)},[['tl','Arriba izquierda'],['tr','Arriba derecha'],['bl','Abajo izquierda'],['br','Abajo derecha']].map(([k,n])=>el('option',{value:k,...(k===ST.logo_pos?{selected:''}:{})},n)));
 const op=el('input',{type:'range',min:'20',max:'100',value:ST.logo_opacity,onchange:()=>put('logo_opacity',op.value)});
 const lf=el('input',{type:'file',accept:'image/*',style:'display:none',onchange:async()=>{const f=lf.files[0];if(!f)return;await api('/api/settings/logo',{method:'POST',body:await imgTo(f,'image/png',320)});ST.logo=true;await put('logo_on','1');settings()}});
 const info=await api('/api/stats'),upd=VER.update,vmsg=el('span',{class:'meta'});
 const checkNow=async()=>{vmsg.textContent='Buscando…';try{await api('/api/update/check',{method:'POST'});VER=await api('/api/version');updDismissed=false;updBar();settings()}catch(e){vmsg.textContent=e.message}};
 app.replaceChildren(el('h1',{},'Configuración'),
  el('div',{class:'panel'},el('b',{},'Reproductor'),el('div',{class:'row',style:'margin-top:12px'},el('span',{class:'meta'},'Color de acento'),acc,el('button',{class:'sec',onclick:()=>{acc.value='#2ee6a6';ST.accent='#2ee6a6';applyAccent();put('accent','#2ee6a6')}},'Restablecer'),el('span',{class:'meta'},'Velocidad inicial'),spd),
   chk('controls','Mostrar la barra de controles del reproductor'),chk('autoplay','Reproducir automáticamente al abrir un vídeo'),chk('loop','Repetir vídeos por defecto'),
   el('b',{style:'display:block;margin-top:18px'},'Logotipo sobre el reproductor'),el('div',{class:'row',style:'margin-top:10px'},ST.logo?el('img',{src:'/logo?t='+Date.now(),style:'height:40px;background:#000;border-radius:6px;padding:4px'}):el('span',{class:'meta'},'Sin logotipo'),
    el('button',{class:'sec',onclick:()=>lf.click()},ST.logo?'Cambiar…':'Subir logotipo…'),lf,ST.logo?el('button',{class:'sec',onclick:async()=>{await api('/api/settings/logo',{method:'DELETE'});ST.logo=false;await put('logo_on','0');settings()}},'Quitar'):''),
   ST.logo?el('div',{},chk('logo_on','Mostrar el logotipo sobre los vídeos'),el('div',{class:'row',style:'margin-top:6px'},pos,el('span',{class:'meta'},'Opacidad'),op)):'' ),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'Privacidad y seguridad'),el('p',{class:'meta'},'Tus vídeos, miniaturas, subtítulos y estadísticas se guardan solo en este equipo. No hay cuentas, anuncios ni seguimiento. Puedes proteger vídeos con contraseña y ocultarlos de la biblioteca desde la página de cada vídeo. Las contraseñas se guardan cifradas (hash) en tu base de datos local.'),
   chk('updates','Buscar versiones nuevas (es lo único que usa internet)'),chk('updates_auto','Instalar las actualizaciones automáticamente (con 10 segundos de aviso para cancelar)')),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'Versión'),el('p',{class:'meta',style:'margin-bottom:8px'},'Klaro '+VER.version+' (beta)'+(upd?' · Hay una versión nueva: '+upd.version:' · Estás al día')),
   el('div',{class:'row'},upd?el('button',{onclick:VER.can_install?startUpdate:()=>openUrl(VER.download)},(VER.can_install?'⬆ Actualizar ahora a ':'⬇ Descargar la versión ')+upd.version):'',el('button',{class:'sec',onclick:checkNow},'Buscar actualizaciones'),vmsg)),
  el('div',{class:'panel',style:'margin-top:16px'},el('b',{},'Carpeta de datos'),el('p',{class:'meta'},'Aquí se guardan tus vídeos y la base de datos. Para hacer una copia de seguridad, copia esta carpeta.'),
   el('input',{value:info.data,readonly:'',onclick:e=>e.target.select()}),el('button',{style:'margin-top:12px',onclick:()=>api('/api/open-data',{method:'POST'})},'Abrir carpeta'),el('p',{class:'meta',style:'margin:12px 0 0'},info.videos+' vídeos · '+fmt(info.size)+' en disco')))}
function betaView(key){const b=BETA[key];if(!b){location.hash='#/';return}
 $('#app').replaceChildren(el('div',{class:'panel soonbox'},el('div',{style:'font-size:44px'},b[1]),el('h1',{style:'margin:8px 0'},b[0]),el('span',{class:'pill'},'BETA'),
  el('p',{style:'margin-top:16px'},'Esta función necesita conexión a internet y, como Klaro está en beta, todavía no se puede usar.'),el('ul',{},b[2].map(x=>el('li',{},x))),
  el('p',{class:'meta',style:'margin-top:18px'},key==='team'?'Mientras tanto, puedes usar las notas de revisión con marca de tiempo en cada vídeo.':'Mientras tanto, todo lo demás funciona sin conexión.')))}
let updDismissed=false,autoBusy=false;
function updBar(){const b=$('#updbar');if(!b)return;if(!VER.update||updDismissed){b.style.display='none';return}
 b.style.display='flex';b.replaceChildren(el('span',{style:'flex:1'},'⬆ Hay una versión nueva de Klaro: '+VER.update.version),
  el('button',{onclick:VER.can_install?startUpdate:()=>openUrl(VER.download)},VER.can_install?'Actualizar ahora':'Descargar'),el('button',{class:'sec',onclick:()=>{updDismissed=true;updBar()}},'Más tarde'))}
async function startUpdate(){const msg=el('p',{style:'margin-top:8px'},'Preparando…'),bar=el('i'),box=el('div',{class:'bar'},bar),btns=el('div',{class:'row',style:'margin-top:14px'});
 const m=el('div',{class:'mod'},el('div',{class:'panel'},el('b',{},'Actualizando Klaro'),msg,box,btns));document.body.append(m);
 const fail=t=>{msg.style.color='var(--acc2)';msg.textContent=t;box.style.display='none';btns.replaceChildren(el('button',{onclick:()=>openUrl(VER.download)},'Descargar manualmente'),el('button',{class:'sec',onclick:()=>m.remove()},'Cerrar'))};
 try{await api('/api/update/install',{method:'POST'})}catch(e){fail(e.message);return}
 const iv=setInterval(async()=>{try{const s=await api('/api/update/status');
   if(s.state==='downloading'){msg.textContent='Descargando… '+(s.total?Math.round(100*s.done/s.total)+' %':fmt(s.done));bar.style.width=(s.total?100*s.done/s.total:10)+'%'}
   else if(s.state==='installing'){bar.style.width='100%';msg.textContent='Instalando la nueva versión… Klaro se cerrará y se abrirá solo en unos segundos.'}
   else if(s.state==='error'){clearInterval(iv);fail(s.error)}}
  catch(e){clearInterval(iv);msg.textContent='Klaro se está reiniciando…'}},500)}
function autoUpdate(){if(autoBusy||ST.updates_auto!=='1'||!VER.update||!VER.can_install||updDismissed)return;
 if(/^#\/(record|edit|upload)/.test(location.hash)||(document.querySelector('video.pl')&&!document.querySelector('video.pl').paused)){setTimeout(autoUpdate,60000);return}
 autoBusy=true;let n=10;const cd=el('p',{class:'meta'},'Empieza en 10 s…'),t=el('div',{class:'mod'},el('div',{class:'panel'},el('b',{},'Actualizando Klaro a la versión '+VER.update.version),cd,el('div',{class:'row'},el('button',{class:'sec',onclick:()=>{clearInterval(iv);t.remove();updDismissed=true;autoBusy=false;updBar()}},'Cancelar'))));
 document.body.append(t);const iv=setInterval(()=>{n--;if(n<=0){clearInterval(iv);t.remove();startUpdate()}else cd.textContent='Empieza en '+n+' s…'},1000)}
async function route(){if(window.cleanup){window.cleanup();window.cleanup=null}window.onkeydown=null;head();const h=location.hash||'#/';document.title='Klaro';let m;
 if(h==='#/'){await vlist('/api/videos?','Inicio','Tu biblioteca está vacía. Pulsa «Añadir vídeos» o graba tu pantalla.');
  if(VER.update)$('#app').prepend(el('div',{class:'panel',style:'margin-bottom:18px;border-color:var(--acc);display:flex;gap:14px;align-items:center'},el('span',{style:'flex:1'},'Hay una versión nueva de Klaro: '+VER.update.version),el('button',{onclick:()=>openUrl(VER.download)},'Descargar')));return}
 if(h==='#/top')return vlist('/api/videos?','Más vistos','Aún no hay vídeos.','views');
 if(h==='#/fav')return vlist('/api/videos?f=fav','Favoritos','Marca vídeos con ♥ para verlos aquí.');
 if(h==='#/later')return vlist('/api/videos?f=later','Ver más tarde','No tienes vídeos guardados para más tarde.');
 if(h==='#/hidden')return vlist('/api/videos?f=hidden','Ocultos','No tienes vídeos ocultos. Puedes ocultar uno desde su página.');
 if(m=h.match(/^#\/tag\/(.+)$/)){const t=decodeURIComponent(m[1]);return vlist('/api/videos?tag='+enc(t),'#'+t,'No hay vídeos con esta etiqueta.')}
 if(h==='#/colls')return colls();
 if(m=h.match(/^#\/coll\/(\d+)$/))return collView(m[1]);
 if(h==='#/record')return recordView();
 if(h==='#/prompter')return prompterView();
 if(h==='#/stats')return stats();
 if(h==='#/settings')return settings();
 if(m=h.match(/^#\/beta\/(\w+)$/))return betaView(m[1]);
 if(h==='#/upload')return upload();
 if(m=h.match(/^#\/edit\/([\w-]+)$/))return editView(m[1]);
 if(m=h.match(/^#\/v\/([\w-]+)$/))return watch(m[1]);
 location.hash='#/'}
let tm;$('#q').addEventListener('input',()=>{clearTimeout(tm);tm=setTimeout(()=>{const h=location.hash;if(['#/','#/top','#/fav','#/later','#/hidden'].includes(h)||h.startsWith('#/coll/')||h.startsWith('#/tag/'))route();else location.hash='#/'},300)});
addEventListener('hashchange',route);
(async()=>{try{ST=await api('/api/settings');VER=await api('/api/version')}catch(e){}applyAccent();route();updBar();setTimeout(async()=>{try{VER=await api('/api/version');updBar();autoUpdate()}catch(e){}},8000);
 setInterval(async()=>{try{VER=await api('/api/version');updBar();autoUpdate()}catch(e){}},600000)})();
</script></body></html>"""


# ------------------------------------------------------------------ arranque
def find_browser():
    cands = []
    if os.name == "nt":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            b = os.environ.get(env)
            if b:
                cands += [os.path.join(b, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(b, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        cands = ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
    else:
        cands = [shutil.which(x) for x in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")]
    return next((c for c in cands if c and os.path.isfile(c)), None)


def run_embedded(url):
    """Ventana propia del programa (WebView2 incrustado): sale como app independiente en la barra de tareas.
    Devuelve True si se mostró y se cerró con normalidad; False si no está disponible (se usa el plan B)."""
    try:
        import webview
    except Exception:
        return False
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Klaro.Desktop")  # icono propio en la barra de tareas
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True  # para "Guardar copia"
        except Exception:
            pass
        webview.create_window(APP_NAME, url, width=1280, height=820, min_size=(900, 600), background_color="#0e1116")
        webview.start(gui="edgechromium" if os.name == "nt" else None)
        return True
    except Exception as e:  # noqa
        print("[Klaro] Ventana propia no disponible: %s" % e)
        return False


def main():
    global PORT_IN_USE
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # puerto libre elegido por el sistema
    PORT_IN_USE = srv.server_address[1]
    url = "http://127.0.0.1:%d/" % PORT_IN_USE
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    print("%s funcionando en %s  (datos en %s)" % (APP_NAME, url, DATA))

    if "--headless" in sys.argv:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return

    if run_embedded(url):  # 1) ventana propia incrustada (lo normal cuando está instalado)
        srv.shutdown()
        return

    exe = find_browser()  # 2) plan B: ventana de aplicación con Edge/Chrome
    if exe:
        t0 = time.time()
        try:
            proc = subprocess.Popen([exe, "--app=" + url, "--user-data-dir=" + os.path.join(DATA, "window"),
                                     "--window-size=1280,820", "--no-first-run", "--no-default-browser-check"])
            proc.wait()
            if time.time() - t0 > 3:  # la ventana se cerró: se termina el programa
                srv.shutdown()
                return
        except Exception as e:  # noqa
            print("[Klaro] No se pudo abrir la ventana de aplicación: %s" % e)

    try:  # 3) plan C: navegador normal + ventana de control
        import tkinter as tk
    except Exception:
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("360x270")
    root.configure(bg="#0e1116")
    tk.Label(root, text="klaro", font=("Segoe UI", 28, "bold"), fg="#2ee6a6", bg="#0e1116").pack(pady=(18, 0))
    tk.Label(root, text="Biblioteca en marcha", fg="#e8ecf3", bg="#0e1116").pack()
    tk.Label(root, text=url, fg="#8d97a8", bg="#0e1116").pack(pady=2)
    tk.Button(root, text="Abrir en el navegador", command=lambda: webbrowser.open(url), width=26).pack(pady=(12, 4))
    tk.Button(root, text="Abrir carpeta de datos", command=lambda: open_path(DATA), width=26).pack(pady=4)
    tk.Button(root, text="Salir", command=root.destroy, width=26).pack(pady=4)
    root.after(600, lambda: webbrowser.open(url))
    root.mainloop()
    srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(DATA, "error.log"), "a", encoding="utf-8") as f:
            f.write("=== %s ===\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), traceback.format_exc()))
        raise
