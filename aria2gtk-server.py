#!/usr/bin/env python3
"""aria2gtk-server: headless aria2 with a web UI, for a home server (e.g. a Proxmox LXC).

Runs a private aria2c (JSON-RPC on 127.0.0.1 with a random secret) and serves a password-protected
web page to watch and control it from any browser on your network. Only the Python standard
library is needed, plus aria2:

    apt install aria2 python3          # Debian / Ubuntu LXC
    ./aria2gtk-server.py --dir /srv/downloads

The first start prints a generated password; change it with --set-password. The page lives in
web/index.html next to this script. Settings, session and log are in --data
(default ~/.local/share/aria2gtk-server).
"""
import argparse
import base64
import getpass
import gzip
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, unquote_to_bytes, urlparse

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
WEB = HERE / "web"

# set in main() from --data; the helpers copied from aria2gtk.py use these names
DATA_DIR = Path.home() / ".local" / "share" / "aria2gtk-server"
SESSION = DATA_DIR / "session"
LOG = DATA_DIR / "aria2.log"
CONFIG = DATA_DIR / "server.json"
GEO_DB = DATA_DIR / "dbip-country-lite.mmdb"
GEO_SYSTEM = [Path("/usr/share/GeoIP/GeoLite2-Country.mmdb"),
              Path("/usr/share/GeoIP/dbip-country-lite.mmdb"),
              Path("/var/lib/GeoIP/GeoLite2-Country.mmdb")]
GEO_URL = "https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
GEO_CREDIT = "IP geolocation by DB-IP (db-ip.com), CC BY 4.0"
DOWNLOADS = str(Path.home() / "Downloads")   # replaced by --dir in main()
LOG_LIMIT = 100 << 20
LOG_KEEP = 5 << 20
SEED_FOREVER = "5256000"
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ----------------------------------------------------- which Linux this is ---
# (the same block is in aria2gtk.py and aria2gtk-server.py: keep them identical)

OS_RELEASE = ("/etc/os-release", "/usr/lib/os-release")
# family: (install command, update command, {generic name: this family's package})
# generic names: aria2, mktorrent, python3, gi (PyGObject), cairo (pycairo),
# gi-cairo (PyGObject's cairo support), gtk4, adw (libadwaita); "gtk" means all GUI pieces
PACKAGERS = {
    "arch": ("sudo pacman -S --needed", "sudo pacman -Syu", {
        "python3": "python", "gi": "python-gobject", "gi-cairo": "python-gobject",
        "cairo": "python-cairo", "gtk4": "gtk4", "adw": "libadwaita"}),
    "debian": ("sudo apt install", "sudo apt update && sudo apt full-upgrade", {
        "gi": "python3-gi", "gi-cairo": "python3-gi-cairo", "cairo": "python3-cairo",
        "gtk4": "gir1.2-gtk-4.0", "adw": "gir1.2-adw-1"}),
    "fedora": ("sudo dnf install", "sudo dnf upgrade --refresh", {
        "gi": "python3-gobject", "gi-cairo": "python3-gobject", "cairo": "python3-cairo",
        "gtk4": "gtk4", "adw": "libadwaita"}),
    "suse": ("sudo zypper install", "sudo zypper dup", {
        "gi": "python3-gobject", "gi-cairo": "python3-gobject-cairo", "cairo": "python3-cairo",
        "gtk4": "typelib-1_0-Gtk-4_0", "adw": "typelib-1_0-Adw-1"}),
    "alpine": ("sudo apk add", "sudo apk upgrade", {
        "gi": "py3-gobject3", "gi-cairo": "py3-gobject3", "cairo": "py3-cairo",
        "gtk4": "gtk4.0", "adw": "libadwaita"}),
    "void": ("sudo xbps-install -S", "sudo xbps-install -Su", {
        "gi": "python3-gobject", "gi-cairo": "python3-gobject", "cairo": "python3-cairo",
        "gtk4": "gtk4", "adw": "libadwaita"}),
    "gentoo": ("sudo emerge --ask", "sudo emerge --ask -uDN @world", {
        "aria2": "net-misc/aria2", "mktorrent": "net-p2p/mktorrent",
        "python3": "dev-lang/python", "gi": "dev-python/pygobject",
        "gi-cairo": "dev-python/pygobject", "cairo": "dev-python/pycairo",
        "gtk4": "gui-libs/gtk", "adw": "gui-libs/libadwaita"}),
}
FAMILY_OF = {"arch": "arch", "debian": "debian", "ubuntu": "debian", "fedora": "fedora",
             "rhel": "fedora", "centos": "fedora", "suse": "suse", "opensuse": "suse",
             "alpine": "alpine", "void": "void", "gentoo": "gentoo"}
GUI_PACKAGES = ("gi", "gi-cairo", "cairo", "gtk4", "adw")
MIN_GTK, MIN_ADW, MIN_PYTHON = (4, 16), (1, 6), (3, 10)


def os_info(paths=OS_RELEASE):
    """{"name": "CachyOS Linux", "id": "cachyos", "family": "arch" or ""} from os-release."""
    fields = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    key, sep, value = line.strip().partition("=")
                    if sep and not key.startswith("#"):
                        fields[key] = value.strip().strip("\"'")
            break
        except OSError:
            continue
    ids = [fields.get("ID", "").lower()] + fields.get("ID_LIKE", "").lower().split()
    family = ""
    for i in ids:
        family = FAMILY_OF.get(i) or FAMILY_OF.get(i.split("-")[0], "")
        if family:
            break
    name = fields.get("PRETTY_NAME") or fields.get("NAME") or "Linux"
    return {"name": name, "id": ids[0] or "linux", "family": family}


def _sudo(cmd):
    return cmd.replace("sudo ", "") if os.geteuid() == 0 else cmd   # root (an LXC): no sudo


def install_hint(*packages, info=None):
    """The command that installs these packages here, e.g. "sudo apt install aria2"."""
    info = info or os_info()
    cmd, _upd, names = PACKAGERS.get(info["family"], (None, None, {}))
    wanted = []
    for p in packages:
        for g in (GUI_PACKAGES if p == "gtk" else (p,)):
            if names.get(g, g) not in wanted:
                wanted.append(names.get(g, g))
    if not cmd:
        return f"install {', '.join(wanted)} with your package manager"
    return _sudo(f"{cmd} {' '.join(wanted)}")


def update_hint(info=None):
    info = info or os_info()
    upd = PACKAGERS.get(info["family"], (None, None, {}))[1]
    return _sudo(upd) if upd else "update your system with your package manager"


def _ver(t):
    return ".".join(map(str, t))


def check_deps(gui=True):
    """Everything the app needs, checked: [{name, why, kind, found, ok, old, package}].
    kind: "core" (nothing works without it), "gui" (the window), "optional"."""
    out = []

    def add(name, why, kind, package, found="", ok=True, old=False):
        out.append({"name": name, "why": why, "kind": kind, "package": package,
                    "found": found, "ok": ok, "old": old})

    py = sys.version_info[:3]
    add("Python", "running the app", "core", "python3", _ver(py), py[:2] >= MIN_PYTHON,
        py[:2] < MIN_PYTHON)
    exe = shutil.which("aria2c")
    version = ""
    if exe:
        try:
            first = subprocess.run([exe, "--version"], capture_output=True, text=True,
                                   timeout=5).stdout.split("\n")[0]
            version = first.split()[-1] if first else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    add("aria2", "downloads, torrents and seeding", "core", "aria2", version, bool(exe))
    if gui:
        try:
            import gi
            add("PyGObject", "the window (Python bindings for GTK)", "gui", "gi", gi.__version__)
        except ImportError:
            gi = None
            add("PyGObject", "the window (Python bindings for GTK)", "gui", "gi", ok=False)
        try:
            import cairo
            add("pycairo", "drawing the header banner", "gui", "cairo", cairo.version)
        except ImportError:
            add("pycairo", "drawing the header banner", "gui", "cairo", ok=False)
        if gi:
            try:
                gi.require_foreign("cairo")
                add("PyGObject cairo support", "drawing inside GTK", "gui", "gi-cairo", "yes")
            except (ImportError, AttributeError):
                add("PyGObject cairo support", "drawing inside GTK", "gui", "gi-cairo", ok=False)
            for ns, label, why, pkg, need in (("Gtk", "GTK", "the window", "gtk4", MIN_GTK),
                                              ("Adw", "libadwaita", "the GNOME look", "adw",
                                               MIN_ADW)):
                try:
                    gi.require_version(ns, "4.0" if ns == "Gtk" else "1")
                    mod = getattr(__import__("gi.repository", fromlist=[ns]), ns)
                    have = (mod.get_major_version(), mod.get_minor_version(),
                            mod.get_micro_version())
                    add(f"{label} {_ver(need)}+", why, "gui", pkg, _ver(have),
                        have[:2] >= need, have[:2] < need)
                except (ImportError, ValueError):
                    add(f"{label} {_ver(need)}+", why, "gui", pkg, ok=False)
        else:
            add(f"GTK {_ver(MIN_GTK)}+", "the window", "gui", "gtk4", ok=False)
            add(f"libadwaita {_ver(MIN_ADW)}+", "the GNOME look", "gui", "adw", ok=False)
        add("mktorrent", "making torrents on the Create tab", "optional", "mktorrent",
            "yes" if shutil.which("mktorrent") else "", bool(shutil.which("mktorrent")))
    return out


def deps_missing(report, kinds=("core", "gui", "optional")):
    return [d for d in report if not d["ok"] and d["kind"] in kinds]


def deps_guide(report, info=None, kinds=("core", "gui", "optional"), items=True):
    """Text guide: what is missing or too old here and the commands that fix it."""
    info = info or os_info()
    bad = deps_missing(report, kinds)
    if not bad:
        return ""
    lines = []
    for d in bad if items else ():
        state = f"{d['found']} is too old" if d["old"] else "missing"
        extra = " (optional)" if d["kind"] == "optional" else ""
        lines.append(f"  ✗ {d['name']}: {state}, needed for {d['why']}{extra}")
    if lines:
        lines.append("")
    new = [d["package"] for d in bad if not d["old"]]
    if new:
        lines.append(f"Install with:\n  {install_hint(*new, info=info)}")
    if any(d["old"] for d in bad):
        lines.append(f"Update your system for newer versions:\n  {update_hint(info)}")
    return "\n".join(lines)


def deps_report(report, info=None):
    """Full text report (for --check and About → Troubleshooting)."""
    info = info or os_info()
    lines = [f"System: {info['name']} (id {info['id']}, family {info['family'] or 'unknown'})"]
    for d in report:
        mark = "✓" if d["ok"] else "✗"
        found = d["found"] if d["ok"] else (f"{d['found']}, too old" if d["old"] else "missing")
        opt = ", optional" if d["kind"] == "optional" else ""
        lines.append(f"  {mark} {d['name']:<26} {found:<16} {d['why']}{opt}")
    guide = deps_guide(report, info, items=False)
    return "\n".join(lines) + ("\n\n" + guide if guide else "\n\nEverything needed is installed.")


def set_data_dir(path):
    global DATA_DIR, SESSION, LOG, CONFIG, GEO_DB
    DATA_DIR = Path(path).expanduser().resolve()
    SESSION = DATA_DIR / "session"
    LOG = DATA_DIR / "aria2.log"
    CONFIG = DATA_DIR / "server.json"
    GEO_DB = DATA_DIR / "dbip-country-lite.mmdb"


# ------------------------------------------------- helpers shared with aria2gtk.py ---

def name_of(d):
    info = (d.get("bittorrent") or {}).get("info") or {}
    if info.get("name"):
        return info["name"]
    files = d.get("files") or []
    if files and files[0].get("path"):
        base = os.path.basename(files[0]["path"])
        if base.startswith("[METADATA]"):
            return f"Magnet metadata ({base[10:18]}…)"
        return base
    if files and files[0].get("uris"):
        uri = files[0]["uris"][0]["uri"]
        return unquote(os.path.basename(urlparse(uri).path)) or uri
    return d["gid"]


def fix_magnet(uri):
    """GIO turns magnet:?xt=… into magnet:///?xt=…; put it back the way clients expect."""
    return re.sub(r"^magnet:/*", "magnet:", uri, flags=re.IGNORECASE)


SOURCEFORGE_PAGE = re.compile(
    r"^https?://(?:www\.)?sourceforge\.net/projects/([^/?#]+)/files/(.+?)/download/?(?:[?#].*)?$",
    re.IGNORECASE)


def fix_link(uri):
    """Repair links aria2 can't use as given.

    SourceForge's .../files/<path>/download is a web page that answers aria2 with 403; the same
    file comes straight from its mirror network at downloads.sourceforge.net/project/<path>.
    """
    uri = fix_magnet(uri.strip())
    m = SOURCEFORGE_PAGE.match(uri)
    if m:
        return f"https://downloads.sourceforge.net/project/{m.group(1)}/{m.group(2)}"
    return uri


def failed_for_good(d):
    """A failure that retrying won't fix: refused (HTTP 4xx), not found, or bad login."""
    if d.get("status") != "error":
        return False
    return (d.get("errorCode") in ("3", "24")
            or re.search(r"status=4\d\d", d.get("errorMessage") or "") is not None)


def tidy_session(drop=()):
    """Before aria2c reads its session file: repair links (see fix_link), merge duplicates
    (same links into the same folder) and leave out downloads that failed for good (`drop`).
    aria2 saves failed downloads too, so otherwise they come back and fail at every start."""
    try:
        text = SESSION.read_text(encoding="utf-8")
    except OSError:
        return
    blocks, cur = [], None
    for line in text.splitlines():
        if line and not line[0].isspace():
            cur = [line]
            blocks.append(cur)
        elif cur is not None and line.strip():
            cur.append(line)
    out, seen = [], set()
    for block in blocks:
        uris = block[0].split("\t")
        fixed = [fix_link(u) for u in uris if u.strip()]
        opts = dict(ln.strip().partition("=")[::2] for ln in block[1:])
        if opts.get("gid") in drop and fixed == uris:
            continue                       # failed for good and nothing to repair
        key = ("\t".join(fixed), opts.get("dir", ""), opts.get("out", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append("\n".join(["\t".join(fixed), *block[1:]]))
    new = "\n".join(out) + ("\n" if out else "")
    if new != text:
        tmp = SESSION.with_suffix(".tmp")
        tmp.write_text(new, encoding="utf-8")
        tmp.replace(SESSION)


def is_metadata_done(d):
    """aria2's finished 'fetch the torrent info' step of a magnet link."""
    files = d.get("files") or []
    return (d.get("status") == "complete" and bool(files)
            and os.path.basename(files[0].get("path", "")).startswith("[METADATA]"))


def is_seeding(d):
    """An active torrent that has all its (selected) data and only uploads now."""
    if d.get("status") != "active" or not d.get("bittorrent"):
        return False
    if d.get("seeder") == "true":
        return True
    total = int(d.get("totalLength", 0) or 0)
    return bool(total) and int(d.get("completedLength", 0) or 0) >= total


def uris_of(d):
    seen = []
    for f in d.get("files") or []:
        for u in f.get("uris") or []:
            if u["uri"] not in seen:
                seen.append(u["uri"])
    return seen


def top_path(d):
    """Path that best represents the download on disk."""
    info = (d.get("bittorrent") or {}).get("info") or {}
    files = d.get("files") or []
    if info.get("name") and len(files) > 1:
        return os.path.join(d["dir"], info["name"])
    if files and files[0].get("path"):
        return files[0]["path"]
    return d.get("dir") or DOWNLOADS


def is_follow_link(uri):
    """Links whose metadata aria2 fetches first: magnets and links to .torrent files."""
    low = uri.lower()
    return low.startswith("magnet:") or urlparse(low).path.endswith(".torrent")


def trim_log():
    """Info/Debug logging can grow aria2.log by gigabytes. Called before aria2c starts."""
    try:
        size = LOG.stat().st_size
    except OSError:
        return
    if size <= LOG_LIMIT:
        return
    try:
        with open(LOG, "rb") as f:
            f.seek(size - LOG_KEEP)
            tail = f.read()
        LOG.with_name(LOG.name + ".old").write_bytes(tail[tail.find(b"\n") + 1:])
        LOG.write_bytes(b"")
    except OSError:
        pass


def norm_limit(text):
    """aria2 speed limit: digits with an optional K or M. Anything else means unlimited (0)."""
    t = (text or "").strip().upper().replace("IB", "").replace("B", "").replace(" ", "")
    return t if re.fullmatch(r"\d+[KM]?", t) else "0"


def limit_to_kib(text):
    t = norm_limit(text)
    if t.endswith("K"):
        return int(t[:-1])
    if t.endswith("M"):
        return int(t[:-1]) * 1024
    return int(t) // 1024


class MMDB:
    """Minimal reader for the MaxMind DB format (enough for country lookups)."""

    MARKER = b"\xab\xcd\xefMaxMind.com"

    def __init__(self, path):
        self.buf = Path(path).read_bytes()
        meta_at = self.buf.rfind(self.MARKER)
        if meta_at < 0:
            raise ValueError("not a MaxMind DB file")
        self.data_start = 0
        self.meta, _ = self._decode(meta_at + len(self.MARKER), absolute=True)
        self.nodes = self.meta["node_count"]
        self.rec = self.meta["record_size"]
        self.node_bytes = self.rec * 2 // 8
        self.data_start = self.nodes * self.node_bytes + 16
        self.ipv4_root = 0
        if self.meta.get("ip_version") == 6:
            node = 0
            for _ in range(96):                 # IPv4 lives under ::/96
                if node >= self.nodes:
                    break
                node = self._record(node, 0)
            self.ipv4_root = node
        self.cache = {}

    def _record(self, node, bit):
        b = self.buf[node * self.node_bytes:(node + 1) * self.node_bytes]
        if self.rec == 24:
            return int.from_bytes(b[3:6] if bit else b[0:3], "big")
        if self.rec == 28:
            if bit:
                return ((b[3] & 0x0F) << 24) | int.from_bytes(b[4:7], "big")
            return ((b[3] & 0xF0) << 20) | int.from_bytes(b[0:3], "big")
        return int.from_bytes(b[4:8] if bit else b[0:4], "big")

    def _decode(self, off, absolute=False):
        buf = self.buf
        base = 0 if absolute else self.data_start
        pos = off if absolute else base + off
        ctrl = buf[pos]
        pos += 1
        kind = ctrl >> 5
        if kind == 1:                           # pointer
            ss, v = (ctrl >> 3) & 3, ctrl & 7
            n = ss + 1
            raw = int.from_bytes(buf[pos:pos + n], "big")
            target = ((v << (8 * n)) | raw) if ss < 3 else raw
            target += (0, 2048, 526336, 0)[ss]
            value, _ = self._decode(target)
            return value, (pos + n) - base
        if kind == 0:                           # extended type
            kind = 7 + buf[pos]
            pos += 1
        size = ctrl & 0x1F
        if size == 29:
            size, pos = 29 + buf[pos], pos + 1
        elif size == 30:
            size, pos = 285 + int.from_bytes(buf[pos:pos + 2], "big"), pos + 2
        elif size == 31:
            size, pos = 65821 + int.from_bytes(buf[pos:pos + 3], "big"), pos + 3
        if kind == 2:
            return buf[pos:pos + size].decode("utf-8", "replace"), pos + size - base
        if kind == 7:                           # map
            out, cur = {}, pos - base
            for _ in range(size):
                key, cur = self._decode(cur if not absolute else cur, absolute)
                out[key], cur = self._decode(cur, absolute)
                if absolute:
                    pass
            return out, cur
        if kind == 11:                          # array
            out, cur = [], pos - base
            for _ in range(size):
                item, cur = self._decode(cur, absolute)
                out.append(item)
            return out, cur
        if kind in (5, 6, 9, 10):               # unsigned ints
            return int.from_bytes(buf[pos:pos + size], "big"), pos + size - base
        if kind == 8:                           # int32
            return int.from_bytes(buf[pos:pos + size], "big", signed=True), pos + size - base
        if kind == 14:                          # boolean
            return bool(size), pos - base
        if kind == 3:
            import struct
            return struct.unpack(">d", buf[pos:pos + 8])[0], pos + 8 - base
        if kind == 15:
            import struct
            return struct.unpack(">f", buf[pos:pos + 4])[0], pos + 4 - base
        return buf[pos:pos + size], pos + size - base      # bytes / unknown

    def country(self, ip):
        """(ISO code, English name) for an IP address, or None."""
        if ip in self.cache:
            return self.cache[ip]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        bits = addr.max_prefixlen
        value = int(addr)
        node = self.ipv4_root if addr.version == 4 else 0
        result = None
        for i in range(bits - 1, -1, -1):
            if node >= self.nodes:
                break
            node = self._record(node, (value >> i) & 1)
        if node > self.nodes:
            rec, _ = self._decode(node - self.nodes - 16)
            c = (rec or {}).get("country") or (rec or {}).get("registered_country") or {}
            if c.get("iso_code"):
                result = (c["iso_code"], (c.get("names") or {}).get("en", c["iso_code"]))
        if len(self.cache) > 20000:
            self.cache.clear()
        self.cache[ip] = result
        return result


_geo = {"db": None, "path": None, "mtime": None}


def geo_path():
    for p in [GEO_DB, *GEO_SYSTEM]:
        if p.is_file():
            return p
    return None


def geo_db():
    """The loaded database (reloaded if the file changed), or None."""
    p = geo_path()
    if not p:
        _geo.update(db=None, path=None, mtime=None)
        return None
    mtime = p.stat().st_mtime
    if _geo["path"] != p or _geo["mtime"] != mtime:
        try:
            _geo.update(db=MMDB(p), path=p, mtime=mtime)
        except (OSError, ValueError, KeyError, IndexError):
            _geo.update(db=None, path=p, mtime=mtime)
    return _geo["db"]


def flag(iso):
    """Regional-indicator emoji flag for a two-letter country code."""
    if not iso or len(iso) != 2 or not iso.isalpha():
        return "🏳"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


def geo_months():
    """This month and last month as 'YYYY-MM' (DB-IP publishes one edition a month)."""
    today = time.gmtime()
    prev = (today.tm_year - 1, 12) if today.tm_mon == 1 else (today.tm_year, today.tm_mon - 1)
    return [f"{today.tm_year}-{today.tm_mon:02d}", f"{prev[0]}-{prev[1]:02d}"]


def geo_edition():
    """'YYYY-MM' edition of the installed database, or None."""
    db = geo_db()
    epoch = db.meta.get("build_epoch") if db else None
    if not epoch:
        return None
    t = time.gmtime(epoch + 3 * 86400)     # an edition may be built a day or two early
    return f"{t.tm_year}-{t.tm_mon:02d}"


def geo_latest():
    """Newest edition DB-IP offers ('YYYY-MM'). Asks only for headers, sends nothing else."""
    last_error = None
    for month in geo_months():
        req = urllib.request.Request(GEO_URL.format(month=month), method="HEAD",
                                     headers={"User-Agent": f"aria2gtk/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=20):
                return month
        except urllib.error.HTTPError as e:
            last_error = e
            if e.code != 404:
                break
        except Exception as e:  # noqa: BLE001
            last_error = e
            break
    raise RuntimeError(f"could not check for updates: {last_error}")


def download_geo_db():
    """Fetch DB-IP Country Lite (this month's, else last month's). Returns its path."""
    last_error = None
    for month in geo_months():
        req = urllib.request.Request(GEO_URL.format(month=month),
                                     headers={"User-Agent": f"aria2gtk/{VERSION}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = gzip.decompress(resp.read())
        except Exception as e:  # noqa: BLE001
            last_error = e
            continue
        if MMDB.MARKER not in data[-200000:]:
            last_error = ValueError("downloaded file is not a country database")
            continue
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = GEO_DB.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(GEO_DB)
        return GEO_DB
    raise RuntimeError(f"could not download the database: {last_error}")


PEER_CLIENTS = {
    "qB": "qBittorrent", "TR": "Transmission", "UT": "µTorrent", "UM": "µTorrent Mac",
    "LT": "libtorrent", "lt": "rTorrent", "DE": "Deluge", "AZ": "Vuze", "BT": "BitTorrent",
    "KT": "KTorrent", "BC": "BitComet", "TX": "Tixati", "FD": "Free Download Manager",
    "BI": "BiglyBT", "PI": "PicoTorrent", "WW": "WebTorrent", "WD": "WebTorrent Desktop",
    "XL": "Xunlei", "SD": "Thunder", "FW": "FrostWire", "LW": "LimeWire", "AG": "Ares",
    "HL": "Halite", "TT": "TuoTu", "SZ": "Shareaza", "MO": "MonoTorrent", "BW": "BitWombat",
    "FL": "Flud", "ML": "MLDonkey", "RT": "Retriever", "TS": "TorrentStorm", "Tr": "Tribler",
}


def peer_client(peer_id):
    """Readable client name from aria2's percent-encoded 20-byte peer id."""
    try:
        raw = unquote_to_bytes(peer_id or "")
    except (TypeError, ValueError):
        return ""
    if len(raw) >= 8 and raw[:1] == b"-" and raw[7:8] == b"-":
        code = raw[1:3].decode("latin-1")
        ver = raw[3:7].decode("latin-1")
        name = PEER_CLIENTS.get(code, code)
        if code == "BC" and ver.isdigit():          # BitComet: -BC0218- = 2.18
            return f"{name} {int(ver[:2])}.{ver[2:]}"
        if ver.isalnum():
            if ver[-1].isalpha():            # trailing letter = build type (µTorrent 355W)
                ver = ver[:-1]
            digits = [str(int(c, 36)) for c in ver]
            while len(digits) > 2 and digits[-1] == "0":
                digits.pop()
            return f"{name} {'.'.join(digits)}"
        return name
    if raw.startswith(b"A2-"):                      # aria2: A2-1-37-0-
        parts = raw[3:].split(b"-")
        return "aria2 " + ".".join(x.decode("latin-1") for x in parts[:3] if x.isdigit())
    if raw[:1] == b"M" and b"-" in raw[:8]:         # mainline: M7-4-3--
        return "BitTorrent " + raw[1:8].decode("latin-1").strip("-").replace("-", ".")
    text = "".join(chr(b) for b in raw[:8] if 32 < b < 127)
    return text or "unknown"


def bitfield_fraction(bitfield, pieces):
    if not bitfield or not pieces:
        return 0.0
    try:
        have = bin(int(bitfield, 16)).count("1")
    except ValueError:
        return 0.0
    return min(have / pieces, 1.0)


# ---------------------------------------------------------------- settings ---

_config_lock = threading.Lock()


# ---------------------------------------------------------------- torrents ---

PIECE_CHOICES = ["Auto (recommended)", "256 KiB", "512 KiB", "1 MiB", "2 MiB",
                 "4 MiB", "8 MiB", "16 MiB", "32 MiB"]
PIECE_EXPS = [None, 18, 19, 20, 21, 22, 23, 24, 25]


def bdecode(data, i=0):
    """Minimal bencode decoder. Dicts get b"__info_span__" = (start, end) of an info value."""
    c = data[i:i + 1]
    if c == b"i":
        end = data.index(b"e", i)
        return int(data[i + 1:end]), end + 1
    if c == b"l":
        i, out = i + 1, []
        while data[i:i + 1] != b"e":
            value, i = bdecode(data, i)
            out.append(value)
        return out, i + 1
    if c == b"d":
        i, out = i + 1, {}
        while data[i:i + 1] != b"e":
            key, i = bdecode(data, i)
            start = i
            out[key], i = bdecode(data, i)
            if key == b"info":
                out[b"__info_span__"] = (start, i)
        return out, i + 1
    colon = data.index(b":", i)
    start = colon + 1
    end = start + int(data[i:colon])
    return data[start:end], end


def bencode(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode("utf-8", "surrogateescape")
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(bencode(x) for x in v) + b"e"
    if isinstance(v, dict):
        keys = sorted(v, key=lambda k: k.encode() if isinstance(k, str) else k)
        return b"d" + b"".join(bencode(k) + bencode(v[k]) for k in keys) + b"e"
    raise TypeError(f"cannot bencode {type(v).__name__}")


def torrent_meta_bytes(data):
    """(v1 info hash as lowercase hex, top-level name) of .torrent bytes, or None."""
    try:
        top, _ = bdecode(data)
        start, end = top[b"__info_span__"]
        info = top[b"info"]
        name = info.get(b"name.utf-8") or info.get(b"name") or b""
        return hashlib.sha1(data[start:end]).hexdigest(), name.decode("utf-8", "replace")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def torrent_meta(path):
    try:
        return torrent_meta_bytes(Path(path).read_bytes())
    except OSError:
        return None


def auto_exp(size):
    if size <= 1 << 30:
        return 20      # 1 MiB
    if size <= 10 << 30:
        return 22      # 4 MiB
    if size <= 100 << 30:
        return 23      # 8 MiB
    return 24          # 16 MiB


def magnet_of(info_hash, name, trackers):
    q = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}"
    return q + "".join(f"&tr={urllib.parse.quote(t, safe='')}" for t in trackers)


def make_torrent(src, trackers, exp=None, private=False, progress=None):
    """Build a v1 .torrent for a file or folder in pure Python (no mktorrent needed).
    Returns (torrent bytes, info hash)."""
    src = Path(src)
    if src.is_file():
        files = [(src, [], src.stat().st_size)]
    elif src.is_dir():
        files = []
        for root, dirs, names in os.walk(src):
            dirs.sort()
            for n in sorted(names):
                p = Path(root) / n
                if p.is_file():
                    files.append((p, list(p.relative_to(src).parts), p.stat().st_size))
    else:
        raise FileNotFoundError(f"{src} is not there")
    total = sum(f[2] for f in files)
    if not total:
        raise ValueError("Nothing to share: no data in it")
    plen = 1 << (exp or auto_exp(total))
    pieces, h, filled, done, last = [], hashlib.sha1(), 0, 0, 0.0
    for path, _parts, size in files:
        left = size
        with open(path, "rb") as f:
            while left:
                chunk = f.read(min(4 << 20, left))
                if not chunk:
                    raise OSError(f"{path.name} got smaller while it was read")
                left -= len(chunk)
                mv = memoryview(chunk)
                while mv:
                    take = min(len(mv), plen - filled)
                    h.update(mv[:take])
                    filled += take
                    mv = mv[take:]
                    if filled == plen:
                        pieces.append(h.digest())
                        h, filled = hashlib.sha1(), 0
                done += len(chunk)
                if progress and time.monotonic() - last > 0.3:
                    last = time.monotonic()
                    progress(done / total)
    if filled:
        pieces.append(h.digest())
    info = {"name": src.name, "piece length": plen, "pieces": b"".join(pieces)}
    if src.is_file():
        info["length"] = total
    else:
        info["files"] = [{"length": size, "path": parts} for _p, parts, size in files]
    if private:
        info["private"] = 1
    meta = {"info": info, "created by": f"aria2gtk {VERSION}", "creation date": int(time.time())}
    if trackers:
        meta["announce"] = trackers[0]
        if len(trackers) > 1:
            meta["announce-list"] = [[t] for t in trackers]
    if progress:
        progress(1.0)
    return bencode(meta), hashlib.sha1(bencode(info)).hexdigest()


def safe_name(name):
    name = re.sub(r"[/\\\x00-\x1f]", "_", name).strip(". ") or "torrent"
    return name[:200]


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    with _config_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(CONFIG)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 300_000)
    return {"salt": salt, "hash": digest.hex(), "iterations": 300_000}


def check_password(cfg, password):
    rec = cfg.get("password") or {}
    if not rec.get("salt") or not rec.get("hash"):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(rec["salt"]),
                                 int(rec.get("iterations", 300_000)))
    return hmac.compare_digest(digest.hex(), rec["hash"])


# Options a user may set from the page: per download, and globally (saved for the next start).
DOWNLOAD_OPTIONS = {"dir", "out", "select-file", "max-download-limit", "max-upload-limit",
                    "pause", "pause-metadata", "split", "max-connection-per-server",
                    "seed-ratio", "seed-time", "bt-save-metadata", "bt-load-saved-metadata",
                    "check-integrity", "rpc-save-upload-metadata", "header", "referer",
                    "user-agent"}
GLOBAL_OPTIONS = {"max-overall-download-limit", "max-overall-upload-limit",
                  "max-concurrent-downloads", "max-connection-per-server", "split",
                  "seed-ratio", "seed-time", "max-download-limit", "max-upload-limit",
                  "bt-max-peers", "user-agent"}
RPC_METHODS = {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped", "aria2.tellStatus",
               "aria2.getGlobalStat", "aria2.getVersion", "aria2.addUri", "aria2.addTorrent",
               "aria2.addMetalink", "aria2.pause", "aria2.unpause", "aria2.forcePause",
               "aria2.remove", "aria2.forceRemove", "aria2.removeDownloadResult",
               "aria2.purgeDownloadResult", "aria2.changeOption", "aria2.getOption",
               "aria2.getFiles", "aria2.getPeers", "aria2.getUris", "aria2.changeGlobalOption",
               "aria2.getGlobalOption", "aria2.pauseAll", "aria2.unpauseAll",
               "aria2.changePosition"}
KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "dir",
        "files", "bittorrent", "errorCode", "errorMessage", "connections", "numSeeders",
        "infoHash", "seeder", "verifiedLength", "verifyIntegrityPending", "uploadLength",
        "followedBy", "following", "numPieces", "pieceLength"]


class Aria2:
    def __init__(self, port, secret):
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self.token = f"token:{secret}"

    def _post(self, method, params, timeout=10):
        body = json.dumps({"jsonrpc": "2.0", "id": "srv", "method": method,
                           "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json"})
        try:
            with LOCAL_OPENER.open(req, timeout=timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            data = json.load(e)
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "aria2 error"))
        return data["result"]

    def call(self, method, *params, timeout=10):
        return self._post(method, [self.token, *params], timeout)

    def multicall(self, calls):
        payload = [{"methodName": m, "params": [self.token, *p]} for m, *p in calls]
        out = []
        for r in self._post("system.multicall", [payload]):
            out.append(r[0] if isinstance(r, list) else {"error": r.get("message", "error")})
        return out


# ------------------------------------------------------------------ engine ---

class Engine:
    """Owns aria2c: starts it, restarts it if it dies, and keeps the app's housekeeping."""

    def __init__(self, cfg, download_dir, bt_port):
        self.cfg = cfg
        self.dir = str(Path(download_dir).expanduser().resolve())
        self.bt_port = bt_port
        self.proc = None
        self.rpc = None
        self.version = ""
        self.lock = threading.Lock()
        self.stopping = False
        self.tidied = set()
        self.seed_status = {}     # info hash -> (gid, status) from the last state()
        self.seed_errors = {}
        self.job = {}

    def start(self):
        trim_log()
        tidy_session(set(self.cfg.pop("drop_gids", [])))
        save_config(self.cfg)
        exe = shutil.which("aria2c")
        if not exe:
            raise SystemExit(f"aria2c not found. Install it: {install_hint('aria2')}")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        Path(self.dir).mkdir(parents=True, exist_ok=True)
        SESSION.touch()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        secret = secrets.token_hex(16)
        args = [exe, "--enable-rpc=true", "--rpc-listen-all=false",
                f"--rpc-listen-port={port}", f"--rpc-secret={secret}",
                "--rpc-max-request-size=64M", f"--stop-with-process={os.getpid()}",
                f"--dir={self.dir}", "--continue=true", f"--input-file={SESSION}",
                f"--save-session={SESSION}", "--save-session-interval=30", "--force-save=false",
                f"--log={LOG}", "--log-level=notice", "--console-log-level=error",
                "--summary-interval=0", "--enable-dht=true", "--bt-enable-lpd=true",
                f"--listen-port={self.bt_port}", f"--dht-listen-port={self.bt_port}",
                "--max-connection-per-server=8", "--split=8",
                "--bt-save-metadata=true", "--bt-load-saved-metadata=true"]
        args += [f"--{k}={v}" for k, v in self.cfg.get("aria2", {}).items()
                 if k in GLOBAL_OPTIONS]
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        self.rpc = Aria2(port, secret)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"aria2c exited with code {self.proc.returncode}; see {LOG}")
            try:
                self.version = self.rpc.call("aria2.getVersion", timeout=1).get("version", "")
                return
            except (OSError, RuntimeError):
                time.sleep(0.2)
        raise RuntimeError("aria2c did not answer on its RPC port")

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            try:
                self.rpc.call("aria2.forceShutdown", timeout=2)   # saves the session
                self.proc.wait(8)
            except Exception:  # noqa: BLE001
                self.proc.terminate()
                try:
                    self.proc.wait(4)
                except subprocess.TimeoutExpired:
                    self.proc.kill()

    def watchdog(self):
        """Restart aria2c if it exits; trim the log; keep the country database fresh."""
        last_trim = last_geo = 0
        while not self.stopping:
            time.sleep(3)
            if self.stopping:
                break
            if self.proc and self.proc.poll() is not None:
                print(f"aria2c exited ({self.proc.returncode}); restarting", flush=True)
                try:
                    with self.lock:
                        self.start()
                    self.seed_status = {}
                    self.resume_seeds()
                except Exception as e:  # noqa: BLE001
                    print(f"could not restart aria2c: {e}", flush=True)
                    time.sleep(10)
            now = time.time()
            if now - last_trim > 60:
                last_trim = now
                trim_log()
            if now - last_geo > 86400 and self.cfg.get("geo_auto", True):
                last_geo = now
                threading.Thread(target=self.update_geo, daemon=True).start()

    def update_geo(self, force=False):
        try:
            edition = geo_edition() if geo_path() else None
            if not force and edition and geo_latest() <= edition:
                return "up to date"
            download_geo_db()
            return f"updated to {geo_edition()}"
        except Exception as e:  # noqa: BLE001
            return str(e)

    # -- the page's view of aria2 ---------------------------------------------

    def state(self):
        res = self.rpc.multicall([("aria2.tellActive", KEYS), ("aria2.tellWaiting", 0, 1000, KEYS),
                                  ("aria2.tellStopped", 0, 1000, KEYS), ("aria2.getGlobalStat",)])
        active, waiting, stopped, stat = [r if not isinstance(r, dict) or "error" not in r
                                          else ([] if i < 3 else {}) for i, r in enumerate(res)]
        items = []
        drop = self.cfg.setdefault("drop_gids", [])
        dirty = False
        seeds = self.seed_entries()
        own = {s["hash"] for s in seeds if s["hash"]}
        seed_items = {}
        for d in [*active, *waiting, *stopped]:
            if d.get("infoHash") in own:
                seed_items.setdefault(d["infoHash"], d)   # newest first: active, waiting, stopped
                continue
            if is_metadata_done(d):                       # internal magnet step: never shown
                if d["gid"] not in self.tidied:
                    self.tidied.add(d["gid"])
                    try:
                        self.rpc.call("aria2.removeDownloadResult", d["gid"], timeout=3)
                    except Exception:  # noqa: BLE001
                        pass
                continue
            if failed_for_good(d) and d["gid"] not in drop:
                drop.append(d["gid"])
                del drop[:-200]
                dirty = True
            items.append(slim(d))
        if dirty:
            save_config(self.cfg)
        try:
            usage = shutil.disk_usage(self.dir)
            disk = {"free": usage.free, "total": usage.total}
        except OSError:
            disk = {}
        self.seed_status = {h: (d["gid"], d["status"]) for h, d in seed_items.items()}
        return {"items": items, "stat": stat, "disk": disk, "dir": self.dir,
                "seeds": [self.seed_view(s, seed_items.get(s["hash"])) for s in seeds],
                "job": self.job, "version": self.version, "server": VERSION}

    def peers(self, gid):
        status = self.rpc.call("aria2.tellStatus", gid, ["numPieces", "bittorrent", "status"])
        pieces = int(status.get("numPieces") or 0)
        db = geo_db()
        out = []
        for p in self.rpc.call("aria2.getPeers", gid):
            ip = p.get("ip", "")
            seeder = p.get("seeder") == "true"
            iso, country = (db.country(ip) if db else None) or (None, None)
            out.append({"ip": ip, "port": p.get("port"), "seeder": seeder,
                        "client": peer_client(p.get("peerId")),
                        "progress": 1.0 if seeder else bitfield_fraction(p.get("bitfield"), pieces),
                        "down": int(p.get("downloadSpeed") or 0),
                        "up": int(p.get("uploadSpeed") or 0),
                        "choked": p.get("peerChoking") == "true",
                        "choking": p.get("amChoking") == "true",
                        "flag": flag(iso) if iso else ("🌐" if db else ""),
                        "country": f"{country} ({iso})" if iso else ""})
        return {"peers": out, "geo": bool(db), "credit": GEO_CREDIT if db else ""}

    def subdirs(self, rel, files=False):
        base = Path(self.dir)
        target = self.inside(rel)
        names, fnames = [], []
        try:
            for e in os.scandir(target):
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    names.append(e.name)
                elif files and e.is_file():
                    fnames.append(e.name)
        except OSError:
            pass
        return {"base": str(base), "path": str(target.relative_to(base)) if target != base else "",
                "dirs": sorted(names)[:500], "files": sorted(fnames)[:500]}

    def inside(self, path):
        """A folder below the download folder (absolute or relative); refuses ../ escapes."""
        base = Path(self.dir)
        p = Path(path or "")
        target = (p if p.is_absolute() else base / p).resolve()
        if target != base and base not in target.parents:
            raise PermissionError("Folders must be inside the download folder")
        return target

    # -- your own seeds (the Seeding tab) ---------------------------------------
    # Headless: kept in server.json. The desktop app overrides these to use its Seeding tab.

    @property
    def torrent_dir(self):
        return DATA_DIR / "torrents"

    def seed_entries(self):
        """[{torrent, source, want, upload_limit, hash, name, error}] of your seeds."""
        out = []
        for s in self.cfg.setdefault("seeds", []):
            if not s.get("hash"):
                s["hash"], s["name"] = torrent_meta(s["torrent"]) or ("", "")
            out.append({**s, "error": self.seed_errors.get(s["hash"], "")})
        return out

    def _seed(self, info_hash):
        for s in self.cfg.setdefault("seeds", []):
            if s.get("hash") == info_hash:
                return s
        raise KeyError("That seed is not on the list any more")

    def seed_options(self, src, upload_limit):
        return {"dir": os.path.dirname(src), "check-integrity": "true", "seed-ratio": "0.0",
                "seed-time": SEED_FOREVER, "rpc-save-upload-metadata": "false",
                "max-upload-limit": f"{upload_limit}K" if upload_limit else "0"}

    def seed_start(self, s):
        h = s["hash"]
        self.seed_errors.pop(h, None)
        src = s["source"].rstrip("/")
        try:
            data = Path(s["torrent"]).read_bytes()
        except OSError:
            self.seed_errors[h] = "Torrent file not found"
            return
        if not os.path.exists(src):
            self.seed_errors[h] = "Original data not found"
            return
        s["want"] = True
        save_config(self.cfg)
        try:
            self.rpc.call("aria2.addTorrent", base64.b64encode(data).decode(), [],
                          self.seed_options(src, s.get("upload_limit")), timeout=60)
        except Exception as e:  # noqa: BLE001
            if "already" not in str(e).lower():
                self.seed_errors[h] = str(e)
                return
            for d in self.rpc.call("aria2.tellWaiting", 0, 1000, ["gid", "infoHash", "status"]):
                if d.get("infoHash") == h and d["status"] == "paused":
                    self.rpc.call("aria2.unpause", d["gid"])

    def resume_seeds(self):
        for s in self.seed_entries():
            if s.get("want") and s["hash"] and s["hash"] not in self.seed_status:
                self.seed_start(self._seed(s["hash"]))

    def seed_add(self, torrent, source):
        meta = torrent_meta(torrent)
        if not meta:
            raise ValueError("That is not a valid .torrent file")
        seeds = self.cfg.setdefault("seeds", [])
        if any(s.get("hash") == meta[0] for s in seeds):
            raise ValueError("This torrent is already on the list")
        seeds[:] = [s for s in seeds if s["torrent"] != str(torrent)]   # same file, new content
        s = {"torrent": str(torrent), "source": str(source), "want": True,
             "hash": meta[0], "name": meta[1]}
        seeds.append(s)
        save_config(self.cfg)
        self.seed_start(s)

    def seed_toggle(self, info_hash):
        s = self._seed(info_hash)
        gid, status = self.seed_status.get(info_hash, (None, ""))
        if gid and status in ("active", "waiting"):
            s["want"] = False
            save_config(self.cfg)
            self.rpc.call("aria2.forcePause", gid)
        elif gid and status == "paused":
            s["want"] = True
            save_config(self.cfg)
            self.rpc.call("aria2.unpause", gid)
        else:
            if gid:
                try:
                    self.rpc.call("aria2.removeDownloadResult", gid)
                except Exception:  # noqa: BLE001
                    pass
            self.seed_start(s)

    def seed_remove(self, info_hash):
        s = self._seed(info_hash)
        gid, _status = self.seed_status.pop(info_hash, (None, ""))
        self.cfg["seeds"].remove(s)
        self.seed_errors.pop(info_hash, None)
        save_config(self.cfg)
        if gid:
            self._drop_gid(gid)

    def _drop_gid(self, gid):
        def work():
            try:
                self.rpc.call("aria2.forceRemove", gid)
                for _ in range(50):
                    if self.rpc.call("aria2.tellStatus", gid, ["status"])["status"] == "removed":
                        break
                    time.sleep(0.1)
            except Exception:  # noqa: BLE001
                pass
            try:
                self.rpc.call("aria2.removeDownloadResult", gid)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=work, daemon=True).start()

    def seed_limit(self, info_hash, kib):
        s = self._seed(info_hash)
        s["upload_limit"] = int(kib)
        save_config(self.cfg)
        gid, _status = self.seed_status.get(info_hash, (None, ""))
        if gid:
            self.rpc.call("aria2.changeOption", gid,
                          {"max-upload-limit": f"{int(kib)}K" if kib else "0"})

    def seed_all(self, run):
        for s in self.seed_entries():
            gid, status = self.seed_status.get(s["hash"], (None, ""))
            if run and status not in ("active", "waiting"):
                self.seed_toggle(s["hash"])
            elif not run and status in ("active", "waiting"):
                self.seed_toggle(s["hash"])

    # -- making torrents (the Create tab) --------------------------------------

    def create_defaults(self):
        return dict({"host": "", "port": "6969", "extra": "", "private": False, "piece": 0},
                    **self.cfg.get("create", {}))

    def save_create_defaults(self, d):
        self.cfg["create"] = d
        save_config(self.cfg)

    def create(self, body):
        if self.job.get("running"):
            raise ValueError("A torrent is being made already")
        src = self.inside(body.get("source") or "")
        if src == Path(self.dir):
            raise ValueError("Choose a folder or file inside the download folder")
        if not src.exists():
            raise ValueError("That folder or file is not there")
        host = str(body.get("host") or "").strip()
        port = str(body.get("port") or "6969").strip()
        extra = str(body.get("extra") or "").split()
        private = bool(body.get("private"))
        piece = int(body.get("piece") or 0)
        if piece not in range(len(PIECE_EXPS)):
            piece = 0
        if not re.fullmatch(r"\d{1,5}", port):
            raise ValueError("The tracker port must be a number")
        trackers = ([f"udp://{host}:{port}/announce"] if host else []) + \
            [t for t in extra if re.match(r"^(udp|https?|wss?)://", t)]
        if private and not trackers:
            raise ValueError("A private torrent needs a tracker: enter your IP or domain")
        self.save_create_defaults({"host": host, "port": port, "extra": " ".join(extra),
                                   "private": private, "piece": piece})
        out = Path(self.torrent_dir) / f"{safe_name(src.name)}.torrent"
        job = {"running": True, "progress": 0.0, "name": src.name, "error": "",
               "source": str(src), "torrent": "", "magnet": "", "hash": "", "private": private}
        self.job = job

        def work():
            try:
                data, info_hash = make_torrent(src, trackers, PIECE_EXPS[piece], private,
                                               lambda f: job.__setitem__("progress", f))
                Path(self.torrent_dir).mkdir(parents=True, exist_ok=True)
                tmp = out.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(out)
                job.update(torrent=str(out), hash=info_hash,
                           magnet="" if private else magnet_of(info_hash, src.name, trackers))
            except Exception as e:  # noqa: BLE001
                job["error"] = str(e)
            job["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def torrent_file(self, info_hash):
        """Path of a .torrent the page may download: a seed's, or the one just made."""
        if self.job.get("hash") == info_hash and self.job.get("torrent"):
            return Path(self.job["torrent"])
        for s in self.seed_entries():
            if s["hash"] == info_hash:
                return Path(s["torrent"])
        raise KeyError("No such torrent")

    def seed_view(self, s, d):
        """What the page shows for one of your seeds (d: its aria2 status, or None)."""
        v = {"id": s["hash"], "name": s.get("name") or Path(s["source"]).name,
             "source": s["source"], "want": bool(s.get("want")),
             "limit": s.get("upload_limit"), "error": s.get("error") or "", "status": "stopped"}
        if d:
            v.update(gid=d["gid"], status=d["status"], up=int(d.get("uploadSpeed") or 0),
                     peers=int(d.get("connections") or 0), sent=int(d.get("uploadLength") or 0),
                     total=int(d.get("totalLength") or 0),
                     verified=int(d.get("verifiedLength") or 0),
                     pending=d.get("verifyIntegrityPending") == "true",
                     seeder=d.get("seeder") == "true")
            if d["status"] == "error":
                v["error"] = d.get("errorMessage") or "error"
        return v


def slim(d):
    """What the page needs from a download (files trimmed to names and sizes)."""
    files = d.get("files") or []
    bt = d.get("bittorrent") or {}
    return {**{k: d.get(k) for k in KEYS if k not in ("files", "bittorrent")},
            "name": name_of(d), "path": top_path(d),
            "isBt": bool(bt), "seeding": is_seeding(d),
            "nfiles": len(files),
            "metadata": bool(files) and os.path.basename(
                files[0].get("path", "")).startswith("[METADATA]"),
            "uris": uris_of(d)[:3],
            "failedForGood": failed_for_good(d)}


# ------------------------------------------------------------------- http ---

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server_version = f"aria2gtk-server/{VERSION}"
    engine = None
    cfg = None
    sessions = {}          # token -> expiry
    failures = {}          # ip -> (count, first time)
    secure = False
    SESSION_DAYS = 30

    def log_message(self, fmt, *args):      # quiet: only errors go to the console
        pass

    # -- plumbing

    def send_json(self, obj, code=200, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, limit=64 << 20):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "a2s":
                return v
        return None

    # Sign-ins survive restarts: kept in sessions.json as SHA-256 of the cookie, never the cookie.
    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode()).hexdigest() if token else ""

    @classmethod
    def load_sessions(cls):
        try:
            data = json.loads((DATA_DIR / "sessions.json").read_text())
            cls.sessions = {k: v for k, v in data.items() if v > time.time()}
        except (OSError, ValueError):
            cls.sessions = {}

    @classmethod
    def save_sessions(cls):
        path = DATA_DIR / "sessions.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({k: v for k, v in cls.sessions.items() if v > time.time()}))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    def authed(self):
        k = self.key(self.token())
        exp = self.sessions.get(k) if k else None
        return bool(exp and exp > time.time())

    def same_origin(self):
        """API writes need a JSON body and our custom header: blocks cross-site form posts."""
        return self.headers.get("X-Aria2gtk") == "1"

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' "
                         "'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; frame-ancestors 'none'")

    # -- GET

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            return self.send_file(WEB / "index.html", "text/html; charset=utf-8")
        if url.path == "/favicon.svg":
            return self.send_file(WEB / "favicon.svg", "image/svg+xml")
        if url.path == "/api/me":
            return self.send_json({"ok": self.authed()}, 200 if self.authed() else 401)
        if not url.path.startswith("/api/"):
            return self.send_json({"error": "not found"}, 404)
        if not self.authed():
            return self.send_json({"error": "login required"}, 401)
        q = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path == "/api/state":
                return self.send_json(self.engine.state())
            if url.path == "/api/peers":
                return self.send_json(self.engine.peers(q.get("gid", "")))
            if url.path == "/api/files":
                return self.send_json({"files": self.engine.rpc.call("aria2.getFiles",
                                                                     q.get("gid", ""))})
            if url.path == "/api/dirs":
                return self.send_json(self.engine.subdirs(q.get("path", ""), q.get("files") == "1"))
            if url.path == "/api/create":
                return self.send_json({"job": self.engine.job,
                                       "defaults": self.engine.create_defaults(),
                                       "pieces": PIECE_CHOICES})
            if url.path == "/api/torrent":
                return self.send_torrent(self.engine.torrent_file(q.get("id", "")))
            if url.path == "/api/settings":
                return self.send_json(self.settings())
        except PermissionError as e:
            return self.send_json({"error": str(e)}, 403)
        except KeyError as e:
            return self.send_json({"error": e.args[0] if e.args else "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 502)
        return self.send_json({"error": "not found"}, 404)

    def send_torrent(self, path):
        body = path.read_bytes()
        name = urllib.parse.quote(path.name)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-bittorrent")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{name}")
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            return self.send_json({"error": f"{path.name} is missing next to the server"}, 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    # -- POST

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if not self.same_origin():
            return self.send_json({"error": "bad request"}, 400)
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad request"}, 400)
        if url.path == "/api/login":
            return self.login(body)
        if url.path == "/api/logout":
            self.sessions.pop(self.key(self.token()), None)
            self.save_sessions()
            return self.send_json({"ok": True}, headers={"Set-Cookie": self.cookie("", 0)})
        if not self.authed():
            return self.send_json({"error": "login required"}, 401)
        try:
            if url.path == "/api/rpc":
                return self.send_json({"result": self.rpc(body.get("method", ""),
                                                          body.get("params") or [])})
            if url.path == "/api/add":
                return self.send_json(self.add(body))
            if url.path == "/api/settings":
                return self.send_json(self.change_settings(body))
            if url.path == "/api/geo":
                return self.send_json({"message": self.engine.update_geo(force=True)})
            if url.path == "/api/password":
                return self.change_password(body)
            if url.path == "/api/seed":
                return self.send_json(self.seed(body))
            if url.path == "/api/create":
                return self.send_json(self.engine.create(body))
            if url.path == "/api/publicip":
                with urllib.request.urlopen("https://ifconfig.me/ip", timeout=6) as r:
                    return self.send_json({"ip": r.read(64).decode().strip()})
        except PermissionError as e:
            return self.send_json({"error": str(e)}, 403)
        except (KeyError, ValueError) as e:
            return self.send_json({"error": e.args[0] if e.args else "bad request"}, 400)
        except Exception as e:  # noqa: BLE001
            return self.send_json({"error": str(e)}, 502)
        return self.send_json({"error": "not found"}, 404)

    def cookie(self, value, max_age):
        flags = "; Secure" if self.secure else ""
        return f"a2s={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{flags}"

    def login(self, body):
        ip = self.client_address[0]
        count, first = self.failures.get(ip, (0, time.time()))
        if time.time() - first > 300:
            count, first = 0, time.time()
        if count >= 5:
            return self.send_json({"error": "Too many attempts. Wait a few minutes."}, 429)
        if not check_password(self.cfg, str(body.get("password", ""))):
            self.failures[ip] = (count + 1, first)
            time.sleep(1)
            return self.send_json({"error": "Wrong password"}, 403)
        self.failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        self.sessions[self.key(token)] = time.time() + self.SESSION_DAYS * 86400
        self.save_sessions()
        return self.send_json({"ok": True}, headers={
            "Set-Cookie": self.cookie(token, self.SESSION_DAYS * 86400)})

    def change_password(self, body):
        if not check_password(self.cfg, str(body.get("old", ""))):
            return self.send_json({"error": "The current password is wrong"}, 403)
        new = str(body.get("new", ""))
        if len(new) < 8:
            return self.send_json({"error": "Use at least 8 characters"}, 400)
        self.cfg["password"] = hash_password(new)
        save_config(self.cfg)
        keep = self.key(self.token())
        for k in list(self.sessions):          # sign out every other browser
            if k != keep:
                self.sessions.pop(k, None)
        self.save_sessions()
        return self.send_json({"ok": True})

    # -- aria2 access, checked

    def clean_options(self, opts, allowed):
        out = {}
        for k, v in (opts or {}).items():
            if k not in allowed:
                raise PermissionError(f"Option not allowed: {k}")
            if k == "dir":
                v = str(self.engine.inside(v))
            out[k] = str(v)
        return out

    def rpc(self, method, params):
        if method not in RPC_METHODS:
            raise PermissionError(f"Not allowed: {method}")
        params = list(params)
        if method == "aria2.addUri" and params:
            params[0] = [fix_link(u) for u in params[0]]
            if len(params) > 1:
                params[1] = self.clean_options(params[1], DOWNLOAD_OPTIONS)
        elif method in ("aria2.addTorrent", "aria2.addMetalink"):
            at = 2 if method == "aria2.addTorrent" else 1      # where the options go
            if len(params) > at:
                params[at] = self.clean_options(params[at], DOWNLOAD_OPTIONS)
        elif method == "aria2.changeOption" and len(params) > 1:
            params[1] = self.clean_options(params[1], DOWNLOAD_OPTIONS)
        elif method == "aria2.changeGlobalOption" and params:
            params[0] = self.clean_options(params[0], GLOBAL_OPTIONS)
            store = self.cfg.setdefault("aria2", {})
            store.update(params[0])
            save_config(self.cfg)
        return self.engine.rpc.call(method, *params, timeout=60)

    def add(self, body):
        """Links (one per line), .torrent / .metalink files (base64), into a folder."""
        opts = self.clean_options({"dir": body.get("dir") or self.engine.dir}, DOWNLOAD_OPTIONS)
        choose = bool(body.get("choose"))
        added, errors = [], []
        for raw in re.split(r"[\r\n]+", body.get("links") or ""):
            uri = fix_link(raw)
            if not uri:
                continue
            if not re.match(r"^(https?|ftp|sftp|magnet):", uri, re.IGNORECASE):
                errors.append(f"Not a link: {raw.strip()[:80]}")
                continue
            o = dict(opts)
            if uri.lower().startswith("magnet:"):
                o["bt-save-metadata"] = "true"
                o["bt-load-saved-metadata"] = "true"
                if choose:
                    o["pause-metadata"] = "true"      # the real download waits for file choice
            try:
                added.append({"gid": self.engine.rpc.call("aria2.addUri", [uri], o),
                              "choose": False})
            except Exception as e:  # noqa: BLE001
                errors.append(f"{uri[:60]}: {e}")
        for f in body.get("files") or []:
            name = str(f.get("name", ""))
            data = str(f.get("data", ""))
            low = name.lower()
            o = dict(opts)
            try:
                if low.endswith(".torrent"):
                    torrent = choose
                    if torrent:
                        o["pause"] = "true"
                    gid = self.engine.rpc.call("aria2.addTorrent", data, [], o, timeout=60)
                    added.append({"gid": gid, "choose": torrent})
                elif low.endswith((".metalink", ".meta4")):
                    for gid in self.engine.rpc.call("aria2.addMetalink", data, o, timeout=60):
                        added.append({"gid": gid, "choose": False})
                else:
                    errors.append(f"{name}: not a .torrent or .metalink file")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{name}: {e}")
        return {"added": added, "errors": errors}

    def seed(self, body):
        """Your own seeds: add (an uploaded .torrent, or the one just made), toggle, remove…"""
        e, action, h = self.engine, body.get("action"), str(body.get("id", ""))
        if action == "add":
            if body.get("job"):
                job = e.job
                if not job.get("torrent") or job.get("running"):
                    raise ValueError("Make a torrent first")
                e.seed_add(job["torrent"], job["source"])
                return {"ok": True}
            data = base64.b64decode(str(body.get("torrent", "")), validate=True)
            meta = torrent_meta_bytes(data)
            if not meta:
                raise ValueError("That is not a valid .torrent file")
            folder = e.inside(body.get("dir") or e.dir)
            source = folder / meta[1]
            if not meta[1] or not source.exists():
                raise ValueError(f"“{meta[1]}” is not in {folder}: choose the folder that holds it")
            tdir = Path(e.torrent_dir)
            tdir.mkdir(parents=True, exist_ok=True)
            out = tdir / f"{safe_name(meta[1])}.torrent"
            if out.exists() and out.read_bytes() != data:
                out = tdir / f"{safe_name(meta[1])}-{meta[0][:8]}.torrent"
            out.write_bytes(data)
            e.seed_add(str(out), str(source))
            return {"ok": True}
        if action == "toggle":
            e.seed_toggle(h)
        elif action == "remove":
            e.seed_remove(h)
        elif action == "limit":
            e.seed_limit(h, max(0, int(body.get("kib") or 0)))
        elif action in ("startAll", "pauseAll"):
            e.seed_all(action == "startAll")
        else:
            raise ValueError("Unknown action")
        return {"ok": True}

    def settings(self):
        opts = self.engine.rpc.call("aria2.getGlobalOption")
        path = geo_path()
        return {"options": {k: opts.get(k, "") for k in sorted(GLOBAL_OPTIONS)},
                "dir": self.engine.dir, "version": self.engine.version, "server": VERSION,
                "geo": {"file": path.name if path else "", "edition": geo_edition() if path else "",
                        "auto": self.cfg.get("geo_auto", True), "credit": GEO_CREDIT},
                "log": str(LOG), "os": os_info()["name"]}

    def change_settings(self, body):
        if "geo_auto" in body:
            self.cfg["geo_auto"] = bool(body["geo_auto"])
            save_config(self.cfg)
        return {"ok": True}


# -------------------------------------------------------------------- main ---

def lan_addresses():
    out = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))           # no packet is sent; picks the LAN interface
            out.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(out) or ["<this-machine>"]


def main():
    ap = argparse.ArgumentParser(description="aria2 with a web UI, for a home server.")
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (default all)")
    ap.add_argument("--port", type=int, default=6880, help="web UI port (default 6880)")
    ap.add_argument("--dir", default=str(Path.home() / "Downloads"),
                    help="download folder (default ~/Downloads)")
    ap.add_argument("--data", default=str(DATA_DIR),
                    help="settings, session and log folder")
    ap.add_argument("--bt-port", type=int, default=51413,
                    help="BitTorrent / DHT port to forward on your router (default 51413)")
    ap.add_argument("--cert", help="TLS certificate (PEM) to serve https")
    ap.add_argument("--key", help="TLS private key (PEM)")
    ap.add_argument("--set-password", action="store_true", help="set the web UI password and exit")
    args, extra = ap.parse_known_args()
    if extra:
        print(f"Ignoring options the web UI doesn't use: {' '.join(extra)}", flush=True)

    global DOWNLOADS
    set_data_dir(args.data)
    DOWNLOADS = str(Path(args.dir).expanduser().resolve())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)          # password hash, sign-ins and the session live here
    cfg = load_config()

    if args.set_password:
        pw = getpass.getpass("New password: ")
        if len(pw) < 8 or pw != getpass.getpass("Again: "):
            raise SystemExit("Passwords differ or are shorter than 8 characters.")
        cfg["password"] = hash_password(pw)
        save_config(cfg)
        (DATA_DIR / "sessions.json").unlink(missing_ok=True)    # sign out every browser
        print(f"Password saved in {CONFIG}; every browser has to sign in again")
        return
    report = check_deps(gui=False)
    if deps_missing(report, ("core",)):
        raise SystemExit(f"Can't start on {os_info()['name']}:\n"
                         f"{deps_guide(report, kinds=('core',))}")
    if not cfg.get("password"):
        pw = secrets.token_urlsafe(9)
        cfg["password"] = hash_password(pw)
        save_config(cfg)
        print(f"\n  First start: web UI password is  {pw}\n"
              f"  (change it with --set-password or in the page's Settings)\n", flush=True)

    try:                                    # claim the port first: fail before starting aria2c
        httpd = Server((args.bind, args.port), Handler)
    except OSError as e:
        raise SystemExit(f"Cannot listen on {args.bind}:{args.port}: {e.strerror}")
    engine = Engine(cfg, args.dir, args.bt_port)
    engine.start()
    Handler.engine, Handler.cfg = engine, cfg
    Handler.load_sessions()
    if not geo_path() and cfg.get("geo_auto", True):
        threading.Thread(target=engine.update_geo, daemon=True).start()
    threading.Thread(target=engine.watchdog, daemon=True).start()
    threading.Thread(target=engine.resume_seeds, daemon=True).start()

    scheme = "http"
    if args.cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        Handler.secure, scheme = True, "https"

    def shutdown(*_):
        engine.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    host = lan_addresses()[0] if args.bind in ("0.0.0.0", "::") else args.bind
    print(f"aria2 {engine.version} on {os_info()['name']}, downloads in {engine.dir}", flush=True)
    print(f"Web UI: {scheme}://{host}:{args.port}/", flush=True)
    try:
        httpd.serve_forever()
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
