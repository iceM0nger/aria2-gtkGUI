#!/usr/bin/env python3
"""Aria2 GTK — downloads, torrent creation and seeding with aria2c (GTK4 + libadwaita).

Starts a private aria2c instance (JSON-RPC on a random localhost port with a
random secret) and controls it over RPC. Unfinished downloads are kept in a
session file and resume the next time the app starts.

Needs: aria2, python-gobject, gtk4 >= 4.16, libadwaita >= 1.6, mktorrent (Arch names).
Merged from share-torrent-gui.py: Seeding and Create tabs, tray, opentracker tools.
"""
import base64
import hashlib
import importlib.util
import gzip
import ipaddress
import json
import math
import random
from collections import deque
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def _no_gui_reason():
    """Why the window can't open here (None if it can): no display, or no GTK installed."""
    if "--headless" in sys.argv:
        return "--headless was given"
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return "no graphical display (WAYLAND_DISPLAY / DISPLAY are not set)"
    try:
        import cairo  # noqa: F401
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError) as e:
        return f"GTK 4 / libadwaita are not available ({e})"
    return None


def _run_headless(reason):
    """No window possible: serve the web UI instead (aria2gtk-server.py, next to this file)."""
    import importlib.util
    path = Path(os.path.realpath(__file__)).parent / "aria2gtk-server.py"
    if not path.is_file():
        raise SystemExit(f"Cannot open a window ({reason}) and {path.name} is missing.")
    print(f"aria2gtk: {reason}; starting the web UI instead.", flush=True)
    spec = importlib.util.spec_from_file_location("aria2gtk_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.argv = [str(path)] + [a for a in sys.argv[1:] if a not in ("--headless", "--hidden")]
    mod.main()
    raise SystemExit(0)


if __name__ == "__main__":
    _reason = _no_gui_reason()
    if _reason:
        _run_headless(_reason)

import cairo  # noqa: E402
from urllib.parse import quote, unquote, unquote_to_bytes, urlparse  # noqa: E402

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango, PangoCairo  # noqa: E402

# Use the OpenGL renderer so GTK doesn't load Vulkan layers (silences the lsfg-vk
# warning). GTK 4.18 renamed "ngl" to "gl". GSK_RENDERER=vulkan in the environment
# overrides this. GTK reads it when the first window is realized, so setting it here works.
if "GSK_RENDERER" not in os.environ:
    os.environ["GSK_RENDERER"] = "gl" if Gtk.get_minor_version() >= 18 else "ngl"

APP_ID = "io.github.aria2gtk"
VERSION = "0.1.0"
DATA_DIR = Path(GLib.get_user_data_dir()) / "aria2gtk"
SESSION = DATA_DIR / "session"
SETTINGS = DATA_DIR / "settings.json"
LOG = DATA_DIR / "aria2.log"
DOWNLOADS = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(
    Path.home() / "Downloads"
)
KEYS = [
    "gid", "status", "totalLength", "completedLength", "downloadSpeed",
    "uploadSpeed", "dir", "files", "bittorrent", "errorCode", "errorMessage",
    "connections", "numSeeders", "infoHash", "seeder", "verifiedLength",
    "verifyIntegrityPending", "uploadLength", "followedBy", "following",
]
LINK_SCHEMES = ("magnet:", "http://", "https://", "ftp://", "sftp://")
PAGE_OF = {
    "active": "active",
    "waiting": "queued",
    "paused": "queued",
    "complete": "done",
    "error": "done",
    "removed": "done",
}
CSS = """
.download-row { padding: 10px 12px; }

/* status bar heartbeat: flashes each time aria2c answers */
.heartbeat { color: color-mix(in srgb, currentColor 25%, transparent);
             transition: color 700ms ease-out; }
.heartbeat.beat { color: var(--accent-color); transition: none; }

/* selected torrent: accent glow (inner ring + soft halo), fades in */
list.glow-list > row {
  transition: box-shadow 180ms ease-out, background-color 180ms ease-out;
}
list.glow-list > row:selected {
  background-color: color-mix(in srgb, var(--accent-bg-color) 14%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent-color) 85%, transparent),
              inset 0 0 14px 1px color-mix(in srgb, var(--accent-color) 30%, transparent),
              0 0 14px 2px color-mix(in srgb, var(--accent-bg-color) 55%, transparent);
}
list.glow-list > row:selected:hover {
  background-color: color-mix(in srgb, var(--accent-bg-color) 20%, transparent);
}
.download-row progressbar trough, .download-row progressbar progress { min-height: 4px; }
.links-card { padding: 4px; }
.links-card textview, .links-card textview text { background: none; }
"""


# ---------------------------------------------------------------- helpers ---

def fmt_size(n):
    return GLib.format_size(max(0, int(n)))


def fmt_speed(n):
    # aria2 keeps speeds in a 32-bit int: above ~2 GiB/s (local/LAN copies) it wraps negative
    n = int(n)
    if n < 0:
        n += 1 << 32
    if n <= 0:
        return f"{GLib.format_size(n)}/s"
    bits = GLib.format_size_full(n * 8, GLib.FormatSizeFlags.BITS)
    return f"{GLib.format_size(n)}/s ({bits}/s)"


def fmt_clock(seconds):
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def fmt_eta(seconds):
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    return f"{s // 86400}d {s % 86400 // 3600}h"


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


def load_settings():
    try:
        return json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return {}


def save_settings(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2))


def method_for_file(path):
    low = path.lower()
    if low.endswith(".torrent"):
        return "aria2.addTorrent"
    if low.endswith((".metalink", ".meta4")):
        return "aria2.addMetalink"
    return None


def torrent_cache(info_hash):
    """Where the app keeps its own copy of every torrent it added (for re-seeding)."""
    return DATA_DIR / "torrents" / f"{info_hash}.torrent"


def file_call(path, opts):
    raw = Path(path).read_bytes()
    data = base64.b64encode(raw).decode()
    method = method_for_file(path)
    if method == "aria2.addTorrent":
        meta = torrent_meta(path)
        if meta:
            try:
                keep = torrent_cache(meta[0])
                keep.parent.mkdir(parents=True, exist_ok=True)
                if not keep.exists():
                    keep.write_bytes(raw)
            except OSError:
                pass
        return (method, data, [], opts)
    return (method, data, opts)


# ------------------------------------------------------------- aria2 rpc ---

class Aria2:
    def __init__(self, port, secret):
        self.url = f"http://127.0.0.1:{port}/jsonrpc"
        self.token = f"token:{secret}"

    def _post(self, method, params, timeout=10):
        body = json.dumps(
            {"jsonrpc": "2.0", "id": "gtk", "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            self.url, body, {"Content-Type": "application/json"}
        )
        try:
            with LOCAL_OPENER.open(req, timeout=timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:  # aria2 answers errors with HTTP 400
            data = json.load(e)
        if "error" in data:
            raise RuntimeError(data["error"].get("message", "aria2 error"))
        return data["result"]

    def call(self, method, *params, timeout=10):
        return self._post(method, [self.token, *params], timeout)

    def multicall(self, calls):
        """calls: [(method, *params)]. Returns list of results or Exceptions."""
        payload = [{"methodName": m, "params": [self.token, *p]} for m, *p in calls]
        out = []
        for r in self._post("system.multicall", [payload]):
            if isinstance(r, list):
                out.append(r[0])
            else:
                out.append(RuntimeError(r.get("message", "aria2 error")))
        return out


LOG_LIMIT = 100 << 20       # trim aria2.log above 100 MiB …
LOG_KEEP = 5 << 20          # … keeping its last 5 MiB as aria2.log.old


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


def log_files():
    """(path, kind) of aria2 logs worth showing: ours, our trimmed tail, and aria2.conf's."""
    out = [(LOG, "live"), (LOG.with_name(LOG.name + ".old"), "old")]
    conf = Path.home() / ".aria2" / "aria2.conf"
    extra = {Path.home() / ".aria2" / "aria2.log"}
    try:
        for line in conf.read_text(errors="replace").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "log" and value.strip() not in ("", "-"):
                extra.add(Path(os.path.expanduser(value.strip())))
    except OSError:
        pass
    out += [(p, "external") for p in sorted(extra) if p not in (LOG,)]
    seen, result = set(), []
    for path, kind in out:
        if path in seen:
            continue
        seen.add(path)
        try:
            if path.is_file():
                result.append((path, kind, path.stat().st_size))
        except OSError:
            pass
    return result


def clear_log(path, kind):
    """Empty a log in place (aria2c appends, so it keeps writing fine) or delete a tail."""
    if kind == "old":
        path.unlink(missing_ok=True)
    else:
        with open(path, "r+b") as f:
            f.truncate(0)


class Daemon:
    def __init__(self):
        self.proc = None
        self.rpc = None

    def start(self, download_dir, options=None, log_level="notice", drop=()):
        trim_log()
        tidy_session(drop)
        exe = shutil.which("aria2c")
        if not exe:
            raise FileNotFoundError("aria2c")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SESSION.touch()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        secret = secrets.token_hex(16)
        args = [
            exe,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={port}",
            f"--rpc-secret={secret}",
            "--rpc-max-request-size=32M",
            f"--stop-with-process={os.getpid()}",
            f"--dir={download_dir}",
            "--continue=true",
            f"--input-file={SESSION}",
            f"--save-session={SESSION}",
            "--save-session-interval=30",
            "--force-save=false",
            f"--log={LOG}",
            f"--log-level={log_level}",
            "--console-log-level=error",
            "--summary-interval=0",
        ]
        args += [f"--{k}={v}" for k, v in (options or {}).items() if k != "dir"]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self.rpc = Aria2(port, secret)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"aria2c exited with code {self.proc.returncode} (see {LOG})"
                )
            try:
                return self.rpc.call("aria2.getVersion", timeout=1)
            except (OSError, RuntimeError):
                time.sleep(0.15)
        raise RuntimeError("aria2c did not answer on its RPC port")

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.rpc.call("aria2.forceShutdown", timeout=2)  # also saves the session
            self.proc.wait(5)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ------------------------------------------------------------------- rows ---

LIMIT_STEPS = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 0]
LIMIT_KEYS = {"down": "max-download-limit", "up": "max-upload-limit"}   # KiB/s, 0 = unlimited


def limit_text(kib):
    return "Unlimited" if not kib else fmt_speed(kib * 1024)


def limit_step(kib):
    """Slider position for a limit in KiB/s (the nearest step; unlimited is the far right)."""
    if not kib:
        return len(LIMIT_STEPS) - 1
    return min(range(len(LIMIT_STEPS) - 1),
               key=lambda i: abs(math.log(LIMIT_STEPS[i]) - math.log(kib)))


def limit_note(limits):
    """'max ↓ 2,1 MB/s ↑ 524,3 kB/s' for a row's subtitle, or ''."""
    bits = [f"{arrow} {fmt_size(limits[k] * 1024)}/s" for k, arrow in (("down", "↓"), ("up", "↑"))
            if limits.get(k)]
    return "max " + " ".join(bits) if bits else ""


class SpeedLimitPopover(Gtk.Popover):
    """Right-click on a download or seed: sliders for its own upload / download limit."""

    def __init__(self, parent, title, kinds, limits, apply):
        super().__init__(autohide=True, has_arrow=True)
        self.limits, self.apply, self.timer = dict(limits), apply, 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=330,
                      margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        box.append(Gtk.Label(label="Speed Limits", xalign=0, css_classes=["heading"]))
        box.append(Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                             max_width_chars=40, css_classes=["dim-label", "caption"]))
        self.scales = {}
        for kind in kinds:
            head = Gtk.Box(spacing=6)
            head.append(Gtk.Label(label="Upload" if kind == "up" else "Download", xalign=0,
                                  hexpand=True))
            value = Gtk.Label(xalign=1, css_classes=["numeric", "dim-label"])
            head.append(value)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0,
                                             len(LIMIT_STEPS) - 1, 1)
            scale.set_draw_value(False)
            scale.set_round_digits(0)
            for i in (0, LIMIT_STEPS.index(1024), LIMIT_STEPS.index(16384)):
                scale.add_mark(i, Gtk.PositionType.BOTTOM, limit_text(LIMIT_STEPS[i]).split(" (")[0])
            scale.add_mark(len(LIMIT_STEPS) - 1, Gtk.PositionType.BOTTOM, "∞")
            scale.set_value(limit_step(self.limits.get(kind, 0)))
            value.set_label(limit_text(self.limits.get(kind, 0)))
            scale.connect("value-changed", self.on_changed, kind, value)
            self.scales[kind] = scale
            box.append(head)
            box.append(scale)
        reset = Gtk.Button(label="No Limits", halign=Gtk.Align.END, css_classes=["flat"])
        reset.connect("clicked", lambda _b: [sc.set_value(len(LIMIT_STEPS) - 1)
                                              for sc in self.scales.values()])
        box.append(reset)
        self.set_child(box)
        self.set_parent(parent)
        self.connect("closed", lambda *_: GLib.idle_add(self._done))

    def on_changed(self, scale, kind, value):
        kib = LIMIT_STEPS[int(round(scale.get_value()))]
        value.set_label(limit_text(kib))
        self.limits[kind] = kib
        if self.timer:
            GLib.source_remove(self.timer)
        self.timer = GLib.timeout_add(250, self._send)   # don't flood aria2c while dragging

    def _send(self):
        self.timer = 0
        self.apply(dict(self.limits))
        return False

    def _done(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self._send()
        self.unparent()
        return False


def popup_at(widget, popover, x, y):
    rect = Gdk.Rectangle()
    rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
    popover.set_pointing_to(rect)
    popover.popup()


def on_right_click(widget, callback):
    click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
    click.connect("pressed", lambda g, _n, x, y: callback(x, y))
    widget.add_controller(click)


class DownloadRow(Gtk.ListBoxRow):
    def __init__(self, win, gid):
        super().__init__(activatable=False, selectable=True)
        self.win = win
        self.gid = gid
        self.page = None
        self.data = {}
        self.add_css_class("download-row")

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        top = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(pixel_size=32, valign=Gtk.Align.CENTER)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                       valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                               css_classes=["heading"])
        self.subtitle = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                  css_classes=["dim-label", "caption", "numeric"])
        text.append(self.title)
        text.append(self.subtitle)

        self.toggle = Gtk.Button(valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        self.toggle.connect("clicked", self.on_toggle)
        folder = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Show in Folder",
                            valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        folder.connect("clicked", self.on_folder)
        remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove",
                            valign=Gtk.Align.CENTER, css_classes=["flat", "circular"])
        remove.connect("clicked", self.on_remove)

        self.files_btn = Gtk.Button(icon_name="view-list-bullet-symbolic",
                                    tooltip_text="Choose Files", valign=Gtk.Align.CENTER,
                                    css_classes=["flat", "circular"], visible=False)
        self.files_btn.connect("clicked", lambda _b: FileSelectDialog(
            self.win, self.gid, start=False).present())
        self.peers_btn = Gtk.Button(icon_name="system-users-symbolic", tooltip_text="Peers",
                                    valign=Gtk.Align.CENTER, css_classes=["flat", "circular"],
                                    visible=False)
        self.peers_btn.connect("clicked", lambda _b: PeersWindow(
            self.win, self.gid, name_of(self.data)).present())
        # icon <-> spinner: the spinner shows while aria2c works on something without
        # visible progress (checking files, getting metadata, looking for peers)
        self.icon_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    valign=Gtk.Align.CENTER)
        self.icon_stack.add_named(self.icon, "icon")
        self.icon_stack.add_named(Adw.Spinner(width_request=32, height_request=32), "busy")
        self.last_done = None       # completedLength at the previous poll
        self.moved = False          # has data ever arrived while we watched?
        self.since = time.monotonic()  # when the current wait / stall began
        for w in (self.icon_stack, text, self.toggle, self.files_btn, self.peers_btn, folder,
                  remove):
            top.append(w)
        self.progress = Gtk.ProgressBar()
        outer.append(top)
        outer.append(self.progress)
        self.set_child(outer)
        self.limits = {}            # own limits in KiB/s ("up" / "down"), 0 = none
        self.set_tooltip_text("Right-click for speed limits")
        on_right_click(self, self.show_limits)
        self.read_limits()

    def read_limits(self):
        rpc = self.win.rpc
        if not rpc:
            return

        def work():
            try:
                o = rpc.call("aria2.getOption", self.gid)
            except Exception:  # noqa: BLE001
                return
            GLib.idle_add(self._got_limits, {k: limit_to_kib(o.get(key, "0"))
                                             for k, key in LIMIT_KEYS.items()})

        threading.Thread(target=work, daemon=True).start()

    def _got_limits(self, limits):
        self.limits = limits
        if self.data:
            self.update(self.data)
        return False

    def show_limits(self, x, y):
        d = self.data
        total, done = int(d.get("totalLength", 0) or 0), int(d.get("completedLength", 0) or 0)
        finished = d.get("status") == "complete" or (total and done >= total)
        if d.get("status") in ("complete", "removed", "error") and not d.get("bittorrent"):
            self.win.toast("Speed limits apply to downloads that are still running")
            return
        kinds = (["up"] if finished else ["down"]) + ([] if finished or not d.get("bittorrent")
                                                      else ["up"])
        popup_at(self, SpeedLimitPopover(self, name_of(d), kinds, self.limits,
                                         self.set_limits), x, y)

    def set_limits(self, limits):
        self.limits = limits
        opts = {key: f"{limits.get(k, 0)}K" if limits.get(k) else "0"
                for k, key in LIMIT_KEYS.items()}
        rpc, gid = self.win.rpc, self.gid
        if rpc:
            self.win.run(lambda: rpc.call("aria2.changeOption", gid, opts))
        if self.data:
            self.update(self.data)

    def update(self, d):
        self.data = d
        st = d["status"]
        total = int(d.get("totalLength", 0))
        done = int(d.get("completedLength", 0))
        down = int(d.get("downloadSpeed", 0))
        up = int(d.get("uploadSpeed", 0))
        is_bt = bool(d.get("bittorrent"))
        frac = done / total if total else 0.0

        self.title.set_label(name_of(d))
        self.title.set_tooltip_text(top_path(d))
        self.progress.set_fraction(frac)
        self.progress.set_visible(st in ("active", "waiting", "paused"))

        sizes = f"{fmt_size(done)} of {fmt_size(total)} ({frac:.0%})" if total else ""
        parts = []
        icon = "folder-download-symbolic"
        busy = None
        if st == "active" and total and done >= total and is_bt:
            icon = "emblem-shared-symbolic"
            parts += ["Seeding", fmt_size(total), f"↑ {fmt_speed(up)}"]
        elif st == "active":
            busy = self.activity(d, total, done, down, is_bt)
            if busy:
                parts.append(busy)
                if sizes and not busy.startswith("Checking"):
                    parts.append(sizes)
                if up:
                    parts.append(f"↑ {fmt_speed(up)}")
            else:
                parts.append(sizes or (fmt_size(done) if done else "Connecting…"))
                parts.append(f"↓ {fmt_speed(down)}")
                if up:
                    parts.append(f"↑ {fmt_speed(up)}")
                if down and total:
                    parts.append(f"{fmt_eta((total - done) / down)} left")
                if is_bt and d.get("numSeeders"):
                    parts.append(f"{d['numSeeders']} seeds")
        elif st == "waiting":
            icon = "document-open-recent-symbolic"
            parts += ["Queued"] + ([sizes] if sizes else [])
        elif st == "paused":
            icon = "media-playback-pause-symbolic"
            parts += ["Paused"] + ([sizes] if sizes else [])
        elif st == "complete":
            icon = "emblem-ok-symbolic"
            parts += ["Completed", fmt_size(total)]
        elif st == "error":
            icon = "dialog-error-symbolic"
            parts.append(f"Failed: {d.get('errorMessage') or 'error ' + d.get('errorCode', '?')}")
        elif st == "removed":
            icon = "edit-delete-symbolic"
            parts.append("Removed")
        if st != "active":                  # waits/stalls are timed from the next start
            self.last_done, self.since, self.moved = None, time.monotonic(), False
        note = limit_note(self.limits) if st in ("active", "waiting", "paused") else ""
        if note:
            parts.append(note)
        self.icon_stack.set_visible_child_name("busy" if busy else "icon")
        self.icon.set_from_icon_name(icon)
        self.subtitle.set_label("  ·  ".join(parts))
        self.subtitle.set_tooltip_text(self.subtitle.get_label())
        if st == "error":
            self.subtitle.add_css_class("error")
        else:
            self.subtitle.remove_css_class("error")

        retryable = st in ("error", "removed") and not is_bt and uris_of(d)
        if st in ("active", "waiting"):
            self._set_toggle("media-playback-pause-symbolic", "Pause")
        elif st == "paused":
            self._set_toggle("media-playback-start-symbolic", "Resume")
        elif retryable:
            self._set_toggle("view-refresh-symbolic", "Retry")
        elif st == "complete" and is_bt and d.get("infoHash"):
            self._set_toggle("media-playback-start-symbolic", "Continue Seeding")
        else:
            self.toggle.set_visible(False)
        self.peers_btn.set_visible(is_bt and st == "active")
        self.files_btn.set_visible(is_bt and len(d.get("files") or []) > 1
                                   and st in ("active", "waiting", "paused"))

    def activity(self, d, total, done, down, is_bt):
        """What an active download is busy with when no data flows, or None if all is
        normal. Also drives the progress bar for those states."""
        now = time.monotonic()
        if down:              # real transfer (a file check also raises completedLength)
            self.moved = True
        if self.last_done is None or done != self.last_done:
            self.last_done, self.since = done, now
        waited = fmt_clock(now - self.since)
        conns = int(d.get("connections", 0) or 0)
        verified = int(d.get("verifiedLength", 0) or 0)
        files = d.get("files") or []
        if d.get("verifyIntegrityPending") == "true":
            self.progress.pulse()
            return "Waiting to check existing files…"
        if total and 0 < verified < total:      # aria2 hashes the data already on disk
            self.progress.set_fraction(verified / total)
            return (f"Checking existing files… {verified * 100 // total}%  "
                    f"({fmt_size(verified)} of {fmt_size(total)})")
        if files and os.path.basename(files[0].get("path", "")).startswith("[METADATA]"):
            self.progress.pulse()
            return f"Getting torrent info from peers…  ·  {conns} connected  ·  {waited}"
        if down or (total and done >= total):
            return None
        if conns == 0:
            self.progress.pulse() if not done else None
            return f"{'Looking for peers' if is_bt else 'Connecting'}…  {waited}"
        if not self.moved:
            return (f"Connected to {conns} peer{'s' if conns != 1 else ''}, "
                    f"waiting for data…  {waited}")
        if now - self.since >= 30:
            return f"No data for {waited}  ·  {conns} peer{'s' if conns != 1 else ''}"
        return None

    def _set_toggle(self, icon, tip):
        self.toggle.set_icon_name(icon)
        self.toggle.set_tooltip_text(tip)
        self.toggle.set_visible(True)

    def on_toggle(self, _btn):
        st = self.data.get("status")
        rpc = self.win.rpc
        if st in ("active", "waiting"):
            self.win.run(lambda: rpc.call("aria2.pause", self.gid))
        elif st == "paused":
            self.win.run(lambda: rpc.call("aria2.unpause", self.gid))
        elif st == "complete" and self.data.get("bittorrent"):
            self.win.continue_seeding(self.data)
        elif st in ("error", "removed"):
            uris, ddir, gid = uris_of(self.data), self.data.get("dir"), self.gid

            def retry():
                rpc.call("aria2.addUri", [fix_link(u) for u in uris],
                         {"dir": ddir} if ddir else {})
                rpc.call("aria2.removeDownloadResult", gid)

            self.win.run(retry)

    def on_folder(self, _btn):
        path = top_path(self.data)
        if os.path.exists(path) and path != self.data.get("dir"):
            Gtk.FileLauncher(file=Gio.File.new_for_path(path)).open_containing_folder(
                self.win, None, None)
        else:
            folder = self.data.get("dir") or DOWNLOADS
            Gtk.FileLauncher(file=Gio.File.new_for_path(folder)).launch(self.win, None, None)

    def on_remove(self, _btn):
        if self.page == "done" and self.data.get("status") != "complete":
            self.win.run(lambda: self.win.rpc.call("aria2.removeDownloadResult", self.gid))
            return
        self.win.confirm_remove(self)


# ------------------------------------------------------------- add dialog ---

class AddDialog(Adw.Dialog):
    def __init__(self, win, text="", files=None):
        super().__init__(title="Add Download", content_width=520)
        self.win = win
        self.dir = win.settings.get("dir", DOWNLOADS)
        self.files = []

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.ok = Gtk.Button(label="Download", css_classes=["suggested-action"], sensitive=False)
        self.ok.connect("clicked", self.on_ok)
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.pack_start(cancel)
        header.pack_end(self.ok)

        page = Adw.PreferencesPage()

        links = Adw.PreferencesGroup(
            title="Links", description="HTTP, FTP or magnet links, one download per line")
        self.buffer = Gtk.TextBuffer(text=text)
        self.buffer.connect("changed", lambda *_: self.validate())
        view = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.CHAR,
                            top_margin=8, bottom_margin=8, left_margin=8, right_margin=8,
                            accepts_tab=False)
        scroller = Gtk.ScrolledWindow(child=view, min_content_height=96,
                                      max_content_height=200, propagate_natural_height=True,
                                      css_classes=["card", "links-card"])
        links.add(scroller)
        page.add(links)

        source = Adw.PreferencesGroup()
        self.file_row = Adw.ActionRow(title="Torrent or Metalink Files", subtitle="None",
                                      subtitle_lines=3)
        pick = Gtk.Button(icon_name="document-open-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Choose Files", css_classes=["flat"])
        pick.connect("clicked", self.on_pick_file)
        self.clear_file = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                                     tooltip_text="Clear", css_classes=["flat"], visible=False)
        self.clear_file.connect("clicked", lambda *_: self.set_files([]))
        self.file_row.add_suffix(self.clear_file)
        self.file_row.add_suffix(pick)
        source.add(self.file_row)
        page.add(source)

        opts = Adw.PreferencesGroup(title="Options")
        self.dir_row = Adw.ActionRow(title="Save To", subtitle=self.dir)
        pick_dir = Gtk.Button(icon_name="folder-symbolic", valign=Gtk.Align.CENTER,
                              tooltip_text="Choose Folder", css_classes=["flat"])
        pick_dir.connect("clicked", self.on_pick_dir)
        self.dir_row.add_suffix(pick_dir)
        self.dir_row.set_activatable_widget(pick_dir)
        self.out_row = Adw.EntryRow(title="File Name (single link only)")
        self.conn_row = Adw.SpinRow.new_with_range(1, 16, 1)
        self.conn_row.set_title("Connections per Server")
        self.conn_row.set_value(win.settings.get("connections", 8))
        self.paused_row = Adw.SwitchRow(title="Add Paused")
        self.choose_row = Adw.SwitchRow(title="Choose Files Before Downloading",
                                        subtitle="For torrents and magnet links",
                                        active=win.settings.get("choose_files", True))
        for row in (self.dir_row, self.out_row, self.conn_row, self.paused_row,
                    self.choose_row):
            opts.add(row)
        page.add(opts)

        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(header)
        self.set_child(tv)
        self.set_default_widget(self.ok)
        self.set_files(files or [])

    def links(self):
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, False)
        return [line.strip() for line in text.splitlines() if line.strip()]

    def validate(self):
        self.ok.set_sensitive(bool(self.links() or self.files))

    def set_files(self, paths):
        self.files = list(dict.fromkeys(paths))  # keep order, drop duplicates
        names = ", ".join(os.path.basename(p) for p in self.files)
        self.file_row.set_subtitle(GLib.markup_escape_text(names) if names else "None")
        self.clear_file.set_visible(bool(self.files))
        self.validate()

    def on_pick_file(self, _btn):
        filt = Gtk.FileFilter(name="Torrent & Metalink")
        for pat in ("*.torrent", "*.metalink", "*.meta4"):
            filt.add_pattern(pat)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        dialog = Gtk.FileDialog(title="Choose Files", filters=store, default_filter=filt)

        def done(dlg, res):
            try:
                picked = dlg.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [picked.get_item(i).get_path() for i in range(picked.get_n_items())]
            self.set_files(self.files + [p for p in paths if p])

        dialog.open_multiple(self.win, None, done)

    def on_pick_dir(self, _btn):
        dialog = Gtk.FileDialog(title="Save To", initial_folder=Gio.File.new_for_path(self.dir))

        def done(dlg, res):
            try:
                self.dir = dlg.select_folder_finish(res).get_path()
                self.dir_row.set_subtitle(self.dir)
            except GLib.Error:
                pass

        dialog.select_folder(self.win, None, done)

    def on_ok(self, _btn):
        n = int(self.conn_row.get_value())
        opts = {"dir": self.dir, "max-connection-per-server": str(n), "split": str(n)}
        if self.paused_row.get_active():
            opts["pause"] = "true"
        links = self.links()
        out = self.out_row.get_text().strip()
        try:   # aria2 can't save magnet metadata into a folder that doesn't exist yet
            os.makedirs(self.dir, exist_ok=True)
        except OSError as e:
            self.win.toast(f"Cannot create {self.dir}: {e.strerror}")
            return
        choose = self.choose_row.get_active()
        start = not self.paused_row.get_active()
        calls, select = [], []
        for uri in links:
            o = dict(opts)
            if out and len(links) == 1 and not self.files:
                o["out"] = out
            if uri.lower().startswith("magnet:"):
                o["bt-save-metadata"] = "true"   # keeps <infohash>.torrent for re-seeding
                o["bt-load-saved-metadata"] = "true"  # and reuses it after a restart
            if choose and is_follow_link(uri):
                o["pause-metadata"] = "true"     # real download is created paused
                select.append(("follow", start))
            else:
                select.append(None)
            calls.append(("aria2.addUri", [fix_link(uri)], o))
        for path in self.files:
            o = dict(opts)
            torrent = method_for_file(path) == "aria2.addTorrent"
            if choose and torrent:
                o["pause"] = "true"
            try:
                calls.append(file_call(path, o))
            except OSError as e:
                self.win.toast(f"Cannot read {os.path.basename(path)}: {e.strerror}")
                return
            select.append(("torrent", start) if choose and torrent else None)
        self.win.settings.update(dir=self.dir, connections=n, choose_files=choose)
        save_settings(self.win.settings)
        self.win.add_calls(calls, select)
        self.close()


# ---------------------------------------------------------------- sharing ---

# Calls to this PC (aria2c RPC, opentracker stats) must never go through a system proxy.
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SHARE_TORRENT_CONFIG = Path(GLib.get_user_config_dir()) / "share-torrent" / "config.json"
AUTOSTART_FILE = Path(GLib.get_user_config_dir()) / "autostart" / f"{APP_ID}.desktop"
SEED_FOREVER = "5256000"  # seed-time in minutes (10 years): seeds never stop on their own
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
HASH_RE = re.compile(r"Hashed (\d+) of (\d+)")
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


def torrent_meta(path):
    """(v1 info hash as lowercase hex, top-level name) of a .torrent file, or None."""
    try:
        data = Path(path).read_bytes()
        top, _ = bdecode(data)
        start, end = top[b"__info_span__"]
        info = top[b"info"]
        name = info.get(b"name.utf-8") or info.get(b"name") or b""
        return hashlib.sha1(data[start:end]).hexdigest(), name.decode("utf-8", "replace")
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


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


def describe_seed(d):
    """aria2 status dict -> (text, running, state)."""
    st = d.get("status", "")
    up = int(d.get("uploadSpeed", 0) or 0)
    sent = int(d.get("uploadLength", 0) or 0)
    peers = int(d.get("connections", 0) or 0)
    total = int(d.get("totalLength", 0) or 0)
    verified = int(d.get("verifiedLength", 0) or 0)
    done = int(d.get("completedLength", 0) or 0)
    if st == "error":
        return f"Error: {d.get('errorMessage') or 'unknown'}", False, "error"
    if st == "paused":
        return "Paused", False, "paused"
    if st == "waiting":
        return "Waiting…", True, "waiting"
    if st == "removed":
        return "Removed", False, "removed"
    if st == "complete":
        return "Finished", False, "complete"
    if d.get("seeder") == "true":
        return (f"Seeding  ·  {peers} peer{'s' if peers != 1 else ''}  ·  ↑ {fmt_speed(up)}"
                f"  ·  sent {fmt_size(sent)}"), True, "active"
    if d.get("verifyIntegrityPending") == "true":
        return "Waiting to check files…", True, "active"
    if total and 0 < verified < total:
        return f"Checking files… {verified * 100 // total}%", True, "active"
    if total and done < total:
        return (f"Fetching missing data  ·  {done * 100 // total}%  ·  "
                f"↓ {fmt_speed(d.get('downloadSpeed', 0))}"), True, "active"
    return "Starting…", True, "active"


def path_size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def auto_exp(size):
    if size <= 1 << 30:
        return 20      # 1 MiB
    if size <= 10 << 30:
        return 22      # 4 MiB
    if size <= 100 << 30:
        return 23      # 8 MiB
    return 24          # 16 MiB


def get_torrent_info(path):
    """(full text output, magnet or "") from `aria2c -S`."""
    try:
        res = subprocess.run(["aria2c", "-S", path], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Error running aria2c: {exc}", ""
    text = ANSI_RE.sub("", res.stdout + res.stderr)
    for line in text.splitlines():
        if line.lower().startswith("magnet uri:"):
            return text, line.split(":", 1)[1].strip()
    return text, ""


def copy_text(widget, text):
    provider = Gdk.ContentProvider.new_for_bytes(
        "text/plain;charset=utf-8", GLib.Bytes.new(text.encode()))
    widget.get_clipboard().set_content(provider)


def set_desktop_autostart(enable):
    """Start the app hidden in the tray when you log in."""
    try:
        if enable:
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            script = os.path.realpath(__file__)
            AUTOSTART_FILE.write_text(
                "[Desktop Entry]\nType=Application\nName=Aria2 GTK\n"
                "Comment=Downloads and torrent seeding in the system tray\n"
                f'Exec="{script}" --hidden\nIcon=folder-download\nTerminal=false\n'
                "X-GNOME-Autostart-enabled=true\n")
        elif AUTOSTART_FILE.exists():
            AUTOSTART_FILE.unlink()
        return True
    except OSError:
        return False


def import_share_torrent(settings):
    """One-time import of the old Share Torrent app's settings. Its files are left alone."""
    if settings.get("share_imported") or not SHARE_TORRENT_CONFIG.exists():
        return False
    try:
        old = json.loads(SHARE_TORRENT_CONFIG.read_text())
    except (OSError, ValueError):
        return False
    for key in ("tracker_host", "tracker_port", "extra_trackers", "external_ip",
                "torrent_dir", "piece", "private"):
        if key in old:
            settings.setdefault(key, old[key])
    settings.setdefault("seed_upload_limit", norm_limit(old.get("upload_limit", "0")))
    settings.setdefault("seed_autostart", bool(old.get("autostart", False)))
    settings.setdefault("background", bool(old.get("background", True)))
    if old.get("force_dark"):
        settings.setdefault("color_scheme", "dark")
    seeds = settings.setdefault("seeds", [])
    known = {s["torrent"] for s in seeds}
    for item in old.get("seeds", []):
        if isinstance(item, dict) and item.get("torrent") and item.get("source") \
                and item["torrent"] not in known:
            seeds.append({"torrent": item["torrent"], "source": item["source"], "want": True})
    a2 = settings.setdefault("aria2", {})
    port = str(old.get("seed_port") or "").strip()
    if port and valid_ports(port):
        a2.setdefault("listen-port", port)      # the port forwarded on the router
        a2.setdefault("dht-listen-port", port)
    total = norm_limit(old.get("total_upload_limit", "0"))
    if total != "0":
        a2.setdefault("max-overall-upload-limit", total)
    settings["share_imported"] = True
    return True


COLOR_SCHEMES = [("system", "Follow System"), ("light", "Light"), ("dark", "Dark")]


def apply_color_scheme(name):
    scheme = {"light": Adw.ColorScheme.FORCE_LIGHT,
              "dark": Adw.ColorScheme.FORCE_DARK}.get(name, Adw.ColorScheme.DEFAULT)
    Adw.StyleManager.get_default().set_color_scheme(scheme)


# ------------------------------------------------------------------- tray ---
# StatusNotifierItem for KDE / any StatusNotifier host, spoken over D-Bus with Gio
# (GTK4 has no tray API). Left click toggles the window, right click shows the menu.

SNI_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"

SNI_XML = """
<node>
 <interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="u" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
  <property name="AttentionMovieName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
  <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
  <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewAttentionIcon"/>
  <signal name="NewOverlayIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s" name="status"/></signal>
 </interface>
</node>
"""

MENU_XML = """
<node>
 <interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePaths" type="as" access="read"/>
  <method name="GetLayout">
   <arg type="i" name="parentId" direction="in"/>
   <arg type="i" name="recursionDepth" direction="in"/>
   <arg type="as" name="propertyNames" direction="in"/>
   <arg type="u" name="revision" direction="out"/>
   <arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
   <arg type="ai" name="ids" direction="in"/>
   <arg type="as" name="propertyNames" direction="in"/>
   <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
   <arg type="i" name="id" direction="in"/>
   <arg type="s" name="name" direction="in"/>
   <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
   <arg type="i" name="id" direction="in"/>
   <arg type="s" name="eventId" direction="in"/>
   <arg type="v" name="data" direction="in"/>
   <arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
   <arg type="a(isvu)" name="events" direction="in"/>
   <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow">
   <arg type="i" name="id" direction="in"/>
   <arg type="b" name="needUpdate" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
   <arg type="ai" name="ids" direction="in"/>
   <arg type="ai" name="updatesNeeded" direction="out"/>
   <arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
   <arg type="a(ia{sv})" name="updatedProps"/>
   <arg type="a(ia{sv}as)" name="removedProps"/>
  </signal>
  <signal name="LayoutUpdated">
   <arg type="u" name="revision"/>
   <arg type="i" name="parent"/>
  </signal>
  <signal name="ItemActivationRequested">
   <arg type="i" name="id"/>
   <arg type="u" name="timestamp"/>
  </signal>
 </interface>
</node>
"""


def _in_arrow(px, py):
    if 0.26 <= py <= 0.50:                      # arrow head, widening downward
        return abs(px - 0.5) <= (py - 0.26) / 0.24 * 0.22
    if 0.50 < py <= 0.74:                       # arrow stem
        return abs(px - 0.5) <= 0.075
    return False


def make_tray_pixmaps(sizes=(22, 32, 48, 64)):
    """Purple disc with a white up arrow, as ARGB32 pixmaps (width, height, bytes)."""
    result = []
    ss = 3
    for n in sizes:
        buf = bytearray()
        for y in range(n):
            for x in range(n):
                cover = arrow = 0
                for sy in range(ss):
                    for sx in range(ss):
                        px = (x + (sx + 0.5) / ss) / n
                        py = (y + (sy + 0.5) / ss) / n
                        if (px - 0.5) ** 2 + (py - 0.5) ** 2 <= 0.46 ** 2:
                            cover += 1
                            if _in_arrow(px, py):
                                arrow += 1
                if cover == 0:
                    buf += bytes((0, 0, 0, 0))
                    continue
                t = arrow / cover
                buf += bytes((int(255 * cover / (ss * ss)), int(142 + 113 * t),
                              int(68 + 187 * t), int(173 + 82 * t)))
        result.append((n, n, bytes(buf)))
    return result


class Tray:
    """StatusNotifierItem with a small dbusmenu. Only needs Gio/GLib."""

    ITEMS = (1, 2, 3, 4)

    def __init__(self, on_toggle, on_quit):
        self.on_toggle = on_toggle
        self.on_quit = on_quit
        self.available = False        # True once a tray host accepted our icon
        self._conn = None
        self._owned = False
        self._watcher_conn = None
        self._revision = 1
        self._status_text = "Starting aria2c…"
        self._tooltip = ("Aria2 GTK", "")
        self._pixmaps = None
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._sni_info = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        self._menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._own_id = Gio.bus_own_name(
            Gio.BusType.SESSION, self._bus_name, Gio.BusNameOwnerFlags.NONE,
            self._on_bus_acquired, self._on_name_acquired, self._on_name_lost)
        self._watch_id = Gio.bus_watch_name(
            Gio.BusType.SESSION, WATCHER_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_watcher_appeared, self._on_watcher_vanished)

    # -- bus plumbing

    def _on_bus_acquired(self, conn, _name):
        self._conn = conn
        conn.register_object(SNI_PATH, self._sni_info, self._sni_call, self._sni_get, None)
        conn.register_object(MENU_PATH, self._menu_info, self._menu_call, self._menu_get, None)

    def _on_name_acquired(self, _conn, _name):
        self._owned = True
        self._try_register()

    def _on_name_lost(self, _conn, _name):
        self._owned = False
        self.available = False

    def _on_watcher_appeared(self, conn, _name, _owner):
        self._watcher_conn = conn
        self._try_register()

    def _on_watcher_vanished(self, _conn, _name):
        self._watcher_conn = None
        self.available = False

    def _try_register(self):
        if not (self._owned and self._watcher_conn):
            return
        self._watcher_conn.call(
            WATCHER_NAME, "/StatusNotifierWatcher", WATCHER_NAME, "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._bus_name,)), None, Gio.DBusCallFlags.NONE, 5000, None,
            self._on_registered)

    def _on_registered(self, conn, res):
        try:
            conn.call_finish(res)
            self.available = True
        except GLib.Error:
            self.available = False

    def close(self):
        if self._watch_id:
            Gio.bus_unwatch_name(self._watch_id)
            Gio.bus_unown_name(self._own_id)
            self._watch_id = self._own_id = 0
        self.available = False

    # -- public updates

    def set_status(self, text, tooltip):
        if text != self._status_text:
            self._status_text = text
            self._revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                       GLib.Variant("(ui)", (self._revision, 0)))
        if ("Aria2 GTK", tooltip) != self._tooltip:
            self._tooltip = ("Aria2 GTK", tooltip)
            self._emit(SNI_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)

    def _emit(self, path, iface, signal, params):
        if self._conn is not None:
            try:
                self._conn.emit_signal(None, path, iface, signal, params)
            except GLib.Error:
                pass

    # -- org.kde.StatusNotifierItem

    def _sni_get(self, _conn, _sender, _path, _iface, prop):
        if self._pixmaps is None:
            self._pixmaps = make_tray_pixmaps()
        title, desc = self._tooltip
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "aria2gtk"),
            "Title": GLib.Variant("s", "Aria2 GTK"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("u", 0),
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionIconName": GLib.Variant("s", ""),
            "AttentionIconPixmap": GLib.Variant("a(iiay)", []),
            "AttentionMovieName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], title, desc)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return values.get(prop)

    def _sni_call(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self._safe, self.on_toggle)
        invocation.return_value(None)

    @staticmethod
    def _safe(fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print("tray callback error:", exc)
        return False

    # -- com.canonical.dbusmenu

    def _item_props(self, item_id):
        if item_id == 3:
            return {"type": GLib.Variant("s", "separator")}
        label, enabled = {
            1: ("Show / Hide Window", True),
            2: (self._status_text, False),
            4: ("Quit (stops downloads and seeds)", True),
        }[item_id]
        return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", enabled),
                "visible": GLib.Variant("b", True)}

    def _menu_get(self, _conn, _sender, _path, _iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePaths": GLib.Variant("as", [])}.get(prop)

    def _click(self, item_id, event_id):
        if event_id != "clicked":
            return
        if item_id == 1:
            GLib.idle_add(self._safe, self.on_toggle)
        elif item_id == 4:
            GLib.idle_add(self._safe, self.on_quit)

    def _menu_call(self, _conn, _sender, _path, _iface, method, params, invocation):
        args = params.unpack() if params is not None else ()
        if method == "GetLayout":
            parent = args[0]
            if parent == 0:
                children = [GLib.Variant("(ia{sv}av)", (i, self._item_props(i), []))
                            for i in self.ITEMS]
                layout = (0, {"children-display": GLib.Variant("s", "submenu")}, children)
            elif parent in self.ITEMS:
                layout = (parent, self._item_props(parent), [])
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs",
                                             "no such item")
                return
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self._revision, layout)))
        elif method == "GetGroupProperties":
            ids = args[0] or list(self.ITEMS)
            invocation.return_value(GLib.Variant(
                "(a(ia{sv}))", ([(i, self._item_props(i)) for i in ids if i in self.ITEMS],)))
        elif method == "GetProperty":
            props = self._item_props(args[0]) if args[0] in self.ITEMS else {}
            if args[1] in props:
                invocation.return_value(GLib.Variant("(v)", (props[args[1]],)))
            else:
                invocation.return_dbus_error("org.freedesktop.DBus.Error.InvalidArgs",
                                             "no such property")
        elif method == "Event":
            self._click(args[0], args[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _ts in args[0]:
                self._click(item_id, event_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)


# ---------------------------------------------------------- share widgets ---

class LogView(Gtk.ScrolledWindow):
    MAX_LINES = 3000

    def __init__(self, height=170):
        super().__init__(min_content_height=height, vexpand=False,
                         css_classes=["card", "links-card"])
        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 left_margin=10, right_margin=10, top_margin=8, bottom_margin=8)
        self.set_child(self.view)
        self.buf = self.view.get_buffer()

    def append(self, text):
        text = ANSI_RE.sub("", text)
        if not text.endswith("\n"):
            text += "\n"
        self.buf.insert(self.buf.get_end_iter(), text)
        if self.buf.get_line_count() > self.MAX_LINES:
            self.buf.delete(self.buf.get_start_iter(), self.buf.get_iter_at_line(1000)[1])
        mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
        self.view.scroll_mark_onscreen(mark)
        self.buf.delete_mark(mark)

    def clear(self):
        self.buf.set_text("")


class PathRow(Adw.ActionRow):
    """A row showing a chosen path, with browse buttons."""

    def __init__(self, title, empty, modes=("folder",), filter_name=None, filter_glob=None):
        super().__init__(title=title)
        self.path = ""
        self._empty = empty
        self._filter_name = filter_name
        self._filter_glob = filter_glob
        self.set_subtitle(GLib.markup_escape_text(empty))
        for mode in modes:
            btn = Gtk.Button(icon_name="folder-open-symbolic" if mode == "folder"
                             else "document-open-symbolic",
                             valign=Gtk.Align.CENTER, css_classes=["flat"],
                             tooltip_text="Choose Folder" if mode == "folder" else "Choose File")
            btn.connect("clicked", self._pick, mode)
            self.add_suffix(btn)

    def set_path(self, path):
        self.path = path or ""
        self.set_subtitle(GLib.markup_escape_text(self.path or self._empty))

    def _pick(self, _btn, mode):
        dlg = Gtk.FileDialog()
        win = self.get_root()
        if mode == "folder":
            dlg.select_folder(win, None, self._folder_done)
            return
        if self._filter_glob:
            flt = Gtk.FileFilter(name=self._filter_name or "Files")
            flt.add_pattern(self._filter_glob)
            store = Gio.ListStore.new(Gtk.FileFilter)
            store.append(flt)
            dlg.set_filters(store)
        dlg.open(win, None, self._file_done)

    def _folder_done(self, dlg, res):
        try:
            self.set_path(dlg.select_folder_finish(res).get_path())
        except GLib.Error:
            pass

    def _file_done(self, dlg, res):
        try:
            self.set_path(dlg.open_finish(res).get_path())
        except GLib.Error:
            pass


class FolderEntryRow(Adw.EntryRow):
    """Editable folder path with a Browse button. The folder is created if missing."""

    def __init__(self, title):
        super().__init__(title=title)
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Choose Existing Folder", css_classes=["flat"])
        btn.connect("clicked", self._pick)
        self.add_suffix(btn)

    @property
    def path(self):
        return os.path.expanduser(self.get_text().strip())

    def set_path(self, path):
        self.set_text(path or "")

    def _pick(self, _btn):
        dlg = Gtk.FileDialog()
        if self.path and os.path.isdir(self.path):
            dlg.set_initial_folder(Gio.File.new_for_path(self.path))
        dlg.select_folder(self.get_root(), None, self._done)

    def _done(self, dlg, res):
        try:
            self.set_path(dlg.select_folder_finish(res).get_path())
        except GLib.Error:
            pass


def make_page(*children):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
    for child in children:
        box.append(child)
    return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=900, child=box), vexpand=True,
                              hscrollbar_policy=Gtk.PolicyType.NEVER)


def group(title=None, description=None):
    g = Adw.PreferencesGroup()
    if title:
        g.set_title(title)
    if description:
        g.set_description(description)
    return g


# ----------------------------------------------------------- choose files ---

def index_ranges(indexes):
    """[1, 2, 3, 5, 7, 8] -> "1-3,5,7-8" (aria2 select-file syntax)."""
    out, run = [], []
    for i in sorted(indexes):
        if run and i == run[-1] + 1:
            run.append(i)
            continue
        if run:
            out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
        run = [i]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ",".join(out)


def is_follow_link(uri):
    """Links whose metadata aria2 fetches first: magnets and links to .torrent files."""
    low = uri.lower()
    return low.startswith("magnet:") or urlparse(low).path.endswith(".torrent")


class FileSelectDialog(Adw.Window):
    """Pick which files of a torrent to download (aria2 select-file).

    A real window (not an Adw.Dialog) so it can be resized and maximized; its size is kept.
    """

    def __init__(self, win, gid, start, initial=False):
        size = win.settings.get("files_window", {})
        super().__init__(title="Choose Files", transient_for=win, modal=True,
                         default_width=size.get("width", 760),
                         default_height=size.get("height", 680),
                         maximized=size.get("maximized", False))
        self.win, self.gid, self.start, self.initial = win, gid, start, initial
        self.rows, self.query, self.bulk, self.applied = [], "", False, False

        header = Adw.HeaderBar()
        self.ok = Gtk.Button(label="Download" if start else "Apply",
                             css_classes=["suggested-action"], sensitive=False)
        self.ok.connect("clicked", self.on_ok)
        header.pack_start(self.ok)

        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                              css_classes=["heading"])
        search = Gtk.SearchEntry(placeholder_text="Filter files", hexpand=True)
        search.connect("search-changed", self.on_search)
        all_btn = Gtk.Button(label="All", tooltip_text="Select all shown files")
        all_btn.connect("clicked", lambda _b: self.set_all(True))
        none_btn = Gtk.Button(label="None", tooltip_text="Deselect all shown files")
        none_btn.connect("clicked", lambda _b: self.set_all(False))
        tools = Gtk.Box(spacing=6)
        for w in (search, all_btn, none_btn):
            tools.append(w)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                   css_classes=["boxed-list"], valign=Gtk.Align.START)
        self.listbox.set_filter_func(lambda row: self.query in row.rel_lower)
        self.listbox.connect("row-activated",
                             lambda _lb, row: row.check.set_active(not row.check.get_active()))
        scroller = Gtk.ScrolledWindow(child=self.listbox, vexpand=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.summary = Gtk.Label(xalign=0, css_classes=["dim-label", "caption", "numeric"])
        note = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label", "caption"],
                         label="A skipped file next to a chosen one can get the piece they "
                               "share written into it. It may show its full size, but only "
                               "that piece uses disk space.")

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6,
                       margin_bottom=12, margin_start=12, margin_end=12)
        for w in (self.name, tools, scroller, self.summary, note):
            body.append(w)
        self.stack = Gtk.Stack()
        self.stack.add_named(Adw.Spinner(), "loading")
        self.stack.add_named(body, "files")
        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(header)
        self.set_content(tv)
        self.connect("close-request", self.on_closed)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                           Gtk.CallbackAction.new(lambda *_: self.close())))
        self.add_controller(keys)
        win.select_dialog = self

        rpc = win.rpc

        def work():
            try:
                files = rpc.call("aria2.getFiles", gid)
                st = rpc.call("aria2.tellStatus", gid, ["dir", "bittorrent", "files", "status"])
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.failed, str(e))
                return
            GLib.idle_add(self.build, files, st)

        threading.Thread(target=work, daemon=True).start()

    def failed(self, msg):
        self.win.toast(f"Could not read the file list: {msg}")
        self.close()
        return False

    def build(self, files, st):
        top = name_of(st)
        self.name.set_label(top)
        base = st.get("dir") or ""
        rels = [os.path.relpath(f["path"], base) if f.get("path") and base
                else (f.get("path") or "(unnamed)") for f in files]
        # every path starts with the torrent's folder, already shown above: drop it
        prefix = top + os.sep
        if len(rels) > 1 and all(r.startswith(prefix) for r in rels):
            rels = [r[len(prefix):] for r in rels]
        for f, rel in zip(files, rels):
            row = Gtk.ListBoxRow(activatable=True)
            box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=8,
                          margin_end=12)
            row.check = Gtk.CheckButton(active=f.get("selected") == "true")
            row.check.connect("toggled", lambda *_: self.bulk or self.update_summary())
            label = Gtk.Label(label=rel, xalign=0, hexpand=True,
                              ellipsize=Pango.EllipsizeMode.MIDDLE, tooltip_text=rel)
            size = int(f.get("length", 0) or 0)
            box.append(row.check)
            box.append(label)
            box.append(Gtk.Label(label=fmt_size(size), css_classes=["dim-label", "numeric"]))
            row.set_child(box)
            row.index, row.size, row.rel_lower = int(f["index"]), size, rel.lower()
            self.rows.append(row)
            self.listbox.append(row)
        self.update_summary()
        self.stack.set_visible_child_name("files")
        return False

    def on_search(self, entry):
        self.query = entry.get_text().strip().lower()
        self.listbox.invalidate_filter()

    def set_all(self, active):
        self.bulk = True
        for row in self.rows:
            if self.query in row.rel_lower:
                row.check.set_active(active)
        self.bulk = False
        self.update_summary()

    def update_summary(self):
        chosen = [r for r in self.rows if r.check.get_active()]
        total = sum(r.size for r in self.rows)
        self.summary.set_label(
            f"{len(chosen)} of {len(self.rows)} files  ·  "
            f"{fmt_size(sum(r.size for r in chosen))} of {fmt_size(total)}")
        self.ok.set_sensitive(bool(chosen))

    def on_ok(self, _btn):
        spec = index_ranges(r.index for r in self.rows if r.check.get_active())
        rpc, gid, start = self.win.rpc, self.gid, self.start
        self.applied = True

        def work():
            # pause-metadata off: otherwise every restart re-creates it paused
            rpc.call("aria2.changeOption", gid, {"select-file": spec, "pause-metadata": "false"})
            if start:
                rpc.call("aria2.unpause", gid)

        self.win.run(work)
        self.close()

    def on_closed(self, *_):
        width, height = self.get_default_size()
        self.win.settings["files_window"] = {"width": width, "height": height,
                                             "maximized": self.is_maximized()}
        save_settings(self.win.settings)
        if self.win.select_dialog is self:
            self.win.select_dialog = None
        if self.initial and not self.applied:
            self.win.toast("Kept paused in Queued. Use its Files button to choose later.")
        self.win.next_file_choice()
        return False  # let the window close


# ------------------------------------------------------------ preferences ---

# libadwaita (>= 1.6) palette, made translucent by the opacity setting.
# card is None where Adwaita's card colour is already a translucent overlay.
PALETTE = {
    False: {"window": (250, 250, 251), "headerbar": (255, 255, 255),
            "backdrop": (250, 250, 251), "card": (255, 255, 255)},
    True: {"window": (34, 34, 38), "headerbar": (46, 46, 50),
           "backdrop": (34, 34, 38), "card": None},
}


def translucent_css(alpha, dark):
    p = PALETTE[dark]

    def c(key):
        r, g, b = p[key]
        return f"rgba({r},{g},{b},{alpha:.3f})"

    card = f" --card-bg-color: {c('card')};" if p["card"] else ""
    return (
        f"window.translucent {{ background-color: {c('window')}; }}\n"
        f".translucent-content {{ --window-bg-color: {c('window')};"
        f" --headerbar-bg-color: {c('headerbar')};"
        f" --headerbar-backdrop-color: {c('backdrop')};{card} }}\n"
    )


# aria2 options that changeGlobalOption ignores: they apply after a restart
RESTART_KEYS = {"check-certificate", "enable-dht", "listen-port", "dht-listen-port"}
PORTS_RE = re.compile(r"^\d{1,5}(-\d{1,5})?(,\d{1,5}(-\d{1,5})?)*$")
FILE_ALLOCATION = [
    ("none", "None", "Fastest start, may fragment"),
    ("prealloc", "Preallocate", "Writes the whole file first, slow for big files"),
    ("trunc", "Truncate", "Sets the size without writing"),
    ("falloc", "Fast (falloc)", "Instant on ext4, btrfs, xfs"),
]


def valid_ports(text):
    if not PORTS_RE.match(text):
        return False
    nums = [int(n) for n in re.split(r"[-,]", text)]
    return all(1024 <= n <= 65535 for n in nums)


PROXY_RE = re.compile(r"^((https?)://)?([^\s:@/]+(:[^\s@/]*)?@)?[\w.\-]+(:\d{1,5})?/?$")


def valid_proxy(text):
    return text == "" or bool(PROXY_RE.match(text))


def kib(value):
    return int(value or 0) / 1024


def mib(value):
    return int(value or 0) / (1024 * 1024)


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences", search_enabled=True)
        self.win = win
        self.quiet = False
        self.opts = win.current_options()

        self.add(self.downloads_page())
        self.add(self.network_page())
        self.add(self.bittorrent_page())
        self.add(self.sharing_page())
        self.add(self.appearance_page())
        self.add(self.storage_page())
        self.add(self.web_page())
        self.connect("closed", lambda *_: save_settings(self.win.settings))

    # -- pages ----------------------------------------------------------------

    def downloads_page(self):
        page = Adw.PreferencesPage(title="Downloads", icon_name="folder-download-symbolic")

        where = Adw.PreferencesGroup(title="Saving")
        self.dir_row = Adw.ActionRow(title="Download Folder",
                                     subtitle=self.win.settings.get("dir", DOWNLOADS))
        pick = Gtk.Button(icon_name="folder-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Choose Folder", css_classes=["flat"])
        pick.connect("clicked", self.on_pick_dir)
        self.dir_row.add_suffix(pick)
        self.dir_row.set_activatable_widget(pick)
        where.add(self.dir_row)
        self.switch(where, "auto-file-renaming", "Rename If File Exists",
                    "Saves as file.1.ext instead of overwriting")
        self.combo(where, "file-allocation", "Disk Space Allocation", FILE_ALLOCATION)
        page.add(where)

        queue = Adw.PreferencesGroup(title="Queue and Connections")
        self.spin(queue, "max-concurrent-downloads", "Simultaneous Downloads", 1, 100)
        self.spin(queue, "max-connection-per-server", "Connections per Server", 1, 16,
                  subtitle="Also splits each file into this many parts",
                  extra=lambda v: {"split": v}, after=self.remember_connections)
        self.spin(queue, "min-split-size", "Minimum Segment Size (MiB)", 1, 1024,
                  subtitle="Files smaller than twice this aren't split",
                  to_ui=mib, from_ui=lambda v: f"{int(v)}M")
        page.add(queue)

        retry = Adw.PreferencesGroup(title="Retries")
        self.spin(retry, "max-tries", "Attempts per Download", 0, 100, subtitle="0 = unlimited")
        self.spin(retry, "retry-wait", "Wait Between Attempts (seconds)", 0, 600)
        self.spin(retry, "timeout", "Connection Timeout (seconds)", 1, 600)
        page.add(retry)
        return page

    def network_page(self):
        page = Adw.PreferencesPage(title="Network", icon_name="network-transmit-receive-symbolic")

        speed = Adw.PreferencesGroup(title="Speed Limits", description="0 = unlimited")
        for key, title in (("max-overall-download-limit", "Download Limit (KiB/s)"),
                           ("max-overall-upload-limit", "Upload Limit (KiB/s)")):
            self.spin(speed, key, title, 0, 10_000_000, step=64, to_ui=kib,
                      from_ui=lambda v: f"{int(v)}K")
        page.add(speed)

        conn = Adw.PreferencesGroup(title="Connection")
        self.entry(conn, "all-proxy", "Proxy (e.g. http://host:3128, empty = none)",
                   validate=valid_proxy, invalid="Use http://[user:pass@]host[:port]")
        self.entry(conn, "user-agent", "User Agent")
        self.switch(conn, "check-certificate", "Verify HTTPS Certificates",
                    "Turning this off is insecure")
        page.add(conn)

        geo = Adw.PreferencesGroup(
            title="Peer Countries",
            description="Flags in the Peers window come from a local database, so peer "
                        "addresses never leave this PC. " + GEO_CREDIT + ".")
        self.geo_row = Adw.ActionRow(title="Country Database")
        self.geo_btn = Gtk.Button(valign=Gtk.Align.CENTER)
        self.geo_btn.connect("clicked", self.on_geo_download)
        self.geo_row.add_suffix(self.geo_btn)
        geo.add(self.geo_row)
        auto = Adw.SwitchRow(title="Update Automatically",
                             subtitle="Check once a day, download when a new edition is out",
                             active=self.win.settings.get("geo_auto", True))
        auto.connect("notify::active",
                     lambda r, _p: self.win.settings.__setitem__("geo_auto", r.get_active()))
        geo.add(auto)
        self.geo_latest = None        # newest edition DB-IP offers, once checked
        self.geo_check_error = None
        self.geo_checking = False
        self.show_geo_state()
        if geo_path() == GEO_DB:
            self.check_geo()
        page.add(geo)

        reset = Adw.PreferencesGroup(
            title="Reset",
            description="Forget every aria2 option set here and restart aria2c. "
                        "Values from your aria2.conf apply again.")
        row = Adw.ButtonRow(title="Forget All aria2 Options")
        row.add_css_class("destructive-action")
        row.connect("activated", self.on_reset)
        reset.add(row)
        page.add(reset)
        return page

    def bittorrent_page(self):
        page = Adw.PreferencesPage(title="BitTorrent", icon_name="emblem-shared-symbolic")

        seed = Adw.PreferencesGroup(title="Seeding",
                                    description="Seeding stops when either limit is reached")
        seed_time = self.opts.get("seed-time")
        seeding = seed_time != "0"
        self.seed_switch = Adw.SwitchRow(title="Seed Finished Torrents",
                                         subtitle="Keep uploading after the download completes",
                                         active=seeding)
        seed.add(self.seed_switch)
        ratio = self.spin(seed, "seed-ratio", "Ratio Limit", 0, 100, step=0.1, digits=1,
                          subtitle="Uploaded ÷ downloaded (0 = no ratio limit)",
                          from_ui=lambda v: f"{v:.1f}")
        self.seed_time_row = Adw.SpinRow.new_with_range(1, 525600, 10)
        self.seed_time_row.set_title("Time Limit (minutes)")
        self.seed_time_row.set_value(float(seed_time) if seeding and seed_time else 1440)
        self.seed_time_row.prev = self.seed_time_row.get_value()
        self.seed_time_row.connect("notify::value", self.on_seed_time)
        seed.add(self.seed_time_row)
        for row in (ratio, self.seed_time_row):
            self.seed_switch.bind_property("active", row, "sensitive",
                                           GObject.BindingFlags.SYNC_CREATE)
        self.seed_switch.connect("notify::active", self.on_seed_switch)
        page.add(seed)

        peers = Adw.PreferencesGroup(title="Peers")
        self.spin(peers, "bt-max-peers", "Max Peers per Torrent", 0, 512, subtitle="0 = unlimited")
        self.switch(peers, "enable-dht", "DHT",
                    "Find peers without a tracker. Also needed for udp:// trackers")
        self.switch(peers, "enable-peer-exchange", "Peer Exchange (PEX)")
        self.switch(peers, "bt-enable-lpd", "Local Peer Discovery", "Find peers on your LAN")
        self.switch(peers, "bt-require-crypto", "Require Encryption",
                    "Refuse peers that don't encrypt")
        page.add(peers)

        ports = Adw.PreferencesGroup(title="Ports",
                                     description="A port, a range like 6881-6999, or a list")
        self.entry(ports, "listen-port", "Listen Port (TCP)", validate=valid_ports,
                   invalid="Use ports 1024-65535")
        self.entry(ports, "dht-listen-port", "DHT Port (UDP)", validate=valid_ports,
                   invalid="Use ports 1024-65535")
        page.add(ports)

        trackers = Adw.PreferencesGroup(title="Trackers")
        self.entry(trackers, "bt-tracker", "Extra Trackers (comma separated)")
        page.add(trackers)
        return page

    def sharing_page(self):
        s = self.win.settings
        page = Adw.PreferencesPage(title="Sharing", icon_name="emblem-shared-symbolic")

        seed = Adw.PreferencesGroup(
            title="Seeding",
            description="For torrents on the Seeding tab. They seed until you stop them. "
                        "The listen port is on the BitTorrent page, the overall upload "
                        "limit on the Network page.")
        limit = Adw.SpinRow.new_with_range(0, 10_000_000, 64)
        limit.set_title("Upload Limit per Seed (KiB/s)")
        limit.set_subtitle("0 = unlimited")
        limit.set_value(limit_to_kib(s.get("seed_upload_limit", "0")))
        limit.connect("notify::value", self.on_seed_limit)
        seed.add(limit)
        ip = Adw.EntryRow(title="Public IP to Announce (optional)",
                          text=s.get("external_ip", ""), show_apply_button=True)
        ip.connect("apply", self.on_external_ip)
        seed.add(ip)
        auto = Adw.SwitchRow(title="Start Seeds When the App Opens",
                             subtitle="Resumes every seed that was running",
                             active=bool(s.get("seed_autostart", False)))
        auto.connect("notify::active", lambda r, _p: self.set_app("seed_autostart",
                                                                   r.get_active()))
        seed.add(auto)
        page.add(seed)

        bg = Adw.PreferencesGroup(title="Background")
        tray = Adw.SwitchRow(
            title="Keep Running in the Tray",
            subtitle="Closing the window hides it, downloads and seeds keep going. "
                     "Quit from the tray icon or with Ctrl+Q.",
            active=bool(s.get("background", True)))
        tray.connect("notify::active", lambda r, _p: self.set_app("background", r.get_active()))
        bg.add(tray)
        desktop = Adw.SwitchRow(title="Start With the Desktop",
                                subtitle="Starts hidden in the tray when you log in",
                                active=AUTOSTART_FILE.exists())
        desktop.connect("notify::active", self.on_desktop_autostart)
        bg.add(desktop)
        page.add(bg)
        return page

    def set_app(self, key, value):
        self.win.settings[key] = value
        save_settings(self.win.settings)

    def on_seed_limit(self, row, _pspec):
        kib_value = int(row.get_value())
        self.set_app("seed_upload_limit", f"{kib_value}K" if kib_value else "0")
        self.win.seeding.apply_upload_limit()

    def on_external_ip(self, row):
        text = row.get_text().strip()
        if text:
            try:
                ipaddress.ip_address(text)
            except ValueError:
                self.add_toast(Adw.Toast(title="Not an IP address"))
                row.set_text(self.win.settings.get("external_ip", ""))
                return
        self.set_app("external_ip", text)
        self.add_toast(Adw.Toast(title="Applies to seeds started from now on"))

    def on_desktop_autostart(self, row, _pspec):
        if not set_desktop_autostart(row.get_active()):
            self.add_toast(Adw.Toast(title="Could not change the autostart entry"))

    def on_banner(self, row, _pspec):
        self.set_app("banner", row.get_active())
        self.win.apply_banner(row.get_active())

    def on_color_scheme(self, row, _pspec):
        name = COLOR_SCHEMES[row.get_selected()][0]
        self.set_app("color_scheme", name)
        apply_color_scheme(name)

    def appearance_page(self):
        page = Adw.PreferencesPage(title="Appearance",
                                   icon_name="applications-graphics-symbolic")
        style = Adw.PreferencesGroup(title="Style")
        scheme = Adw.ComboRow(title="Color Scheme",
                              model=Gtk.StringList.new([label for _n, label in COLOR_SCHEMES]))
        names = [n for n, _l in COLOR_SCHEMES]
        current = self.win.settings.get("color_scheme", "system")
        scheme.set_selected(names.index(current) if current in names else 0)
        scheme.connect("notify::selected", self.on_color_scheme)
        style.add(scheme)
        banner = Adw.SwitchRow(title="Header Banner",
                               subtitle="Animated strip above the tabs with live speeds",
                               active=self.win.settings.get("banner", True))
        banner.connect("notify::active", self.on_banner)
        style.add(banner)
        page.add(style)
        look = Adw.PreferencesGroup(
            title="Window",
            description="Below 100% the desktop shows through the window. "
                        "With KDE's blur effect on, that gives an acrylic look.")
        opacity_row = Adw.ActionRow(title="Opacity")
        self.adj = Gtk.Adjustment(lower=30, upper=100, step_increment=1, page_increment=10,
                                  value=round(self.win.settings.get("opacity", 1.0) * 100))
        scale = Gtk.Scale(adjustment=self.adj, hexpand=True, valign=Gtk.Align.CENTER,
                          draw_value=True, value_pos=Gtk.PositionType.RIGHT, digits=0,
                          width_request=240)
        scale.set_format_value_func(lambda _s, v: f"{v:.0f}%")
        scale.add_mark(100, Gtk.PositionType.BOTTOM, None)
        self.adj.connect("value-changed", self.on_opacity)
        opacity_row.add_suffix(scale)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Reset to 100%", css_classes=["flat"])
        reset.connect("clicked", lambda *_: self.adj.set_value(100))
        opacity_row.add_suffix(reset)
        look.add(opacity_row)
        page.add(look)

        ident = Adw.PreferencesGroup(
            title="Window Identity",
            description="Use this in KDE Window Rules or in a blur effect's window class list.")
        id_row = Adw.ActionRow(title="Window Class (Wayland app ID)", subtitle=APP_ID,
                               subtitle_selectable=True)
        id_row.add_css_class("property")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Copy", css_classes=["flat"])
        copy.connect("clicked", self.on_copy_id)
        id_row.add_suffix(copy)
        ident.add(id_row)
        page.add(ident)
        return page

    # -- row builders ---------------------------------------------------------

    def switch(self, group, key, title, subtitle=None):
        row = Adw.SwitchRow(title=title, subtitle=subtitle or "",
                            active=self.opts.get(key) == "true")

        def changed(r, _pspec):
            if not self.apply({key: "true" if r.get_active() else "false"}):
                self.revert(lambda: r.set_active(not r.get_active()))

        row.connect("notify::active", changed)
        group.add(row)
        return row

    def spin(self, group, key, title, lo, hi, step=1, digits=0, subtitle=None,
             to_ui=float, from_ui=lambda v: str(int(v)), extra=None, after=None):
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        row.set_digits(digits)
        if subtitle:
            row.set_subtitle(subtitle)
        try:
            row.set_value(to_ui(self.opts.get(key)))
        except (TypeError, ValueError):
            pass
        row.prev = row.get_value()

        def changed(r, _pspec):
            value = r.get_value()
            if value == r.prev or self.quiet:
                return
            text = from_ui(value)
            changes = {key: text, **(extra(text) if extra else {})}
            if self.apply(changes):
                r.prev = value
                if after:
                    after(value)
            else:
                self.revert(lambda: r.set_value(r.prev))

        row.connect("notify::value", changed)
        group.add(row)
        return row

    def combo(self, group, key, title, choices):
        row = Adw.ComboRow(title=title,
                           model=Gtk.StringList.new([label for _v, label, _d in choices]))
        values = [v for v, _l, _d in choices]
        current = self.opts.get(key)
        row.set_selected(values.index(current) if current in values else 0)
        row.set_subtitle(choices[row.get_selected()][2])
        row.prev = row.get_selected()

        def changed(r, _pspec):
            if self.quiet:
                return
            i = r.get_selected()
            if self.apply({key: values[i]}):
                r.prev = i
                r.set_subtitle(choices[i][2])
            else:
                self.revert(lambda: r.set_selected(r.prev))

        row.connect("notify::selected", changed)
        group.add(row)
        return row

    def entry(self, group, key, title, validate=None, invalid="Invalid value"):
        row = Adw.EntryRow(title=title, text=self.opts.get(key) or "", show_apply_button=True)
        row.prev = row.get_text()

        def applied(r):
            text = r.get_text().strip()
            if validate and not validate(text):
                self.add_toast(Adw.Toast(title=invalid))
                r.set_text(r.prev)
                return
            if self.apply({key: text}):
                r.prev = text
            else:
                r.set_text(r.prev)

        row.connect("apply", applied)
        group.add(row)
        return row

    # -- applying -------------------------------------------------------------

    def revert(self, fn):
        self.quiet = True
        try:
            fn()
        finally:
            self.quiet = False

    def apply(self, changes):
        """Send live options to aria2c now, remember all of them for the next start."""
        if self.quiet:
            return True
        live = {k: v for k, v in changes.items() if k not in RESTART_KEYS}
        if live and self.win.rpc:
            try:
                self.win.rpc.call("aria2.changeGlobalOption", live, timeout=3)
            except Exception as e:  # noqa: BLE001
                self.add_toast(Adw.Toast(title=GLib.markup_escape_text(f"aria2 refused: {e}")))
                return False
        store = self.win.settings.setdefault("aria2", {})
        for k, v in changes.items():
            if k == "dir":
                self.win.settings["dir"] = v
            else:
                store[k] = v
        save_settings(self.win.settings)
        if RESTART_KEYS & changes.keys():
            toast = Adw.Toast(title="Restart aria2c to apply", button_label="Restart",
                              action_name="win.restart-aria2", timeout=0)
            self.add_toast(toast)
        return True

    def remember_connections(self, value):
        self.win.settings["connections"] = int(value)

    def on_seed_switch(self, row, _pspec):
        if self.quiet:
            return
        value = str(int(self.seed_time_row.get_value())) if row.get_active() else "0"
        if not self.apply({"seed-time": value}):
            self.revert(lambda: row.set_active(not row.get_active()))

    def on_seed_time(self, row, _pspec):
        if self.quiet or row.get_value() == row.prev or not self.seed_switch.get_active():
            return
        if self.apply({"seed-time": str(int(row.get_value()))}):
            row.prev = row.get_value()
        else:
            self.revert(lambda: row.set_value(row.prev))

    def on_pick_dir(self, _btn):
        current = self.win.settings.get("dir", DOWNLOADS)
        dialog = Gtk.FileDialog(title="Download Folder",
                                initial_folder=Gio.File.new_for_path(current))

        def done(dlg, res):
            try:
                path = dlg.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            if self.apply({"dir": path}):
                self.dir_row.set_subtitle(path)

        dialog.select_folder(self.win, None, done)

    def show_geo_state(self):
        path = geo_path()
        btn = self.geo_btn
        btn.remove_css_class("suggested-action")
        if not path:
            self.geo_row.set_subtitle("Not downloaded  ·  DB-IP Country Lite, about 8 MB")
            btn.set_label("Download")
            btn.set_sensitive(True)
            return
        edition = geo_edition()
        parts = [path.name, fmt_size(path.stat().st_size)]
        if edition:
            parts.append(f"edition {edition}")
        if path != GEO_DB:
            parts.append("system file")
            btn.set_label("Update")
            btn.set_sensitive(False)
            btn.set_tooltip_text("Installed by your system; update it with your package manager")
        elif self.geo_checking:
            parts.append("checking for updates…")
            btn.set_label("Checking…")
            btn.set_sensitive(False)
        elif self.geo_latest and (not edition or self.geo_latest > edition):
            parts.append(f"update available: {self.geo_latest}")
            btn.set_label("Update")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
        else:
            if self.geo_latest:
                parts.append("up to date")
            elif self.geo_check_error:
                parts.append("could not check for updates")
            btn.set_label("Check Again" if self.geo_latest or self.geo_check_error
                          else "Check for Update")
            btn.set_sensitive(True)
        self.geo_row.set_subtitle("  ·  ".join(parts))

    def check_geo(self):
        self.geo_checking = True
        self.show_geo_state()

        def work():
            try:
                latest, err = geo_latest(), None
            except Exception as e:  # noqa: BLE001
                latest, err = None, str(e)
            GLib.idle_add(done, latest, err)

        def done(latest, err):
            self.geo_checking = False
            self.geo_latest, self.geo_check_error = latest, err
            self.win.settings["geo_checked"] = time.time()
            self.show_geo_state()
            return False

        threading.Thread(target=work, daemon=True).start()

    def on_geo_download(self, btn):
        edition = geo_edition()
        if geo_path() and not (self.geo_latest and (not edition or self.geo_latest > edition)):
            self.check_geo()                   # "Check for Update" / "Check Again"
            return
        btn.set_sensitive(False)
        btn.remove_css_class("suggested-action")
        btn.set_label("Downloading…")

        def work():
            try:
                download_geo_db()
                msg = f"Country database {geo_edition() or ''} ready".replace("  ", " ")
            except Exception as e:  # noqa: BLE001
                msg = str(e)
            GLib.idle_add(done, msg)

        def done(msg):
            self.geo_latest = self.geo_latest or geo_edition()
            self.show_geo_state()
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg)))
            return False

        threading.Thread(target=work, daemon=True).start()

    # -- storage page -----------------------------------------------------------

    LOG_KINDS = {"live": "This app's aria2c log",
                 "old": "Tail kept from the last trim",
                 "external": "Set in ~/.aria2/aria2.conf, written when you run aria2c yourself"}

    def web_page(self):
        page = Adw.PreferencesPage(title="Web UI", name="web",
                                   icon_name="network-server-symbolic")
        web = self.win.web
        group = Adw.PreferencesGroup(
            title="Web UI",
            description="Watch and control these downloads from a browser on another PC, "
                        "phone or tablet in your network, while this app runs. "
                        "Allow the port in your firewall if the page doesn't open.")
        self.web_switch = Adw.SwitchRow(title="Enable Web UI",
                                        active=bool(self.win.settings.get("web_ui")))
        self.web_switch.connect("notify::active", self.on_web_switch)
        group.add(self.web_switch)
        port = Adw.SpinRow.new_with_range(1024, 65535, 1)
        port.set_title("Port")
        port.set_value(int(self.win.settings.get("web_port", WEB_PORT)))
        port.connect("notify::value", self.on_web_port)
        group.add(port)
        self.web_addr = Adw.ActionRow(title="Address", subtitle_selectable=True)
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy Address",
                          valign=Gtk.Align.CENTER, css_classes=["flat"])
        copy.connect("clicked", lambda _b: (self.win.get_clipboard().set(web.urls()[0]),
                                            self.add_toast(Adw.Toast(title="Address copied"))))
        self.web_addr.add_suffix(copy)
        group.add(self.web_addr)
        page.add(group)

        pw = Adw.PreferencesGroup(
            title="Password",
            description="Browsers stay signed in for 30 days. A new password signs every "
                        "browser out.")
        self.web_pw = Adw.PasswordEntryRow(title="New Password (8 or more characters)",
                                           show_apply_button=True)
        self.web_pw.connect("apply", self.on_web_password)
        pw.add(self.web_pw)
        page.add(pw)
        self.show_web_state()
        return page

    def show_web_state(self):
        web = self.win.web
        if web.running:
            self.web_addr.set_subtitle("\n".join(web.urls()))
        elif web.error:
            self.web_addr.set_subtitle(f"Not running: {web.error}")
        else:
            self.web_addr.set_subtitle("Off")

    def on_web_switch(self, row, _p):
        on = row.get_active()
        web = self.win.web
        self.win.settings["web_ui"] = on
        save_settings(self.win.settings)
        if not on:
            web.stop()
            web.error = None
            self.show_web_state()
            return
        new_pw = err = None
        try:
            if not web.has_password():
                new_pw = secrets.token_urlsafe(9)
                web.set_password(new_pw)
        except Exception as e:  # noqa: BLE001
            err = web.error = str(e)
        if not err:
            err = web.start()
        self.show_web_state()
        if err:
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(f"Web UI: {err}")))
        elif new_pw:
            self.show_new_password(new_pw)

    def show_new_password(self, password):
        dialog = Adw.AlertDialog(
            heading="Web UI Password",
            body=f"Sign in at {self.win.web.urls()[0]} with this password:\n\n{password}\n\n"
                 "It is shown only now. You can set your own below.")
        dialog.set_body_use_markup(False)
        dialog.add_response("copy", "Copy Password")
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.connect("response", lambda _d, r: r == "copy" and self.win.get_clipboard()
                       .set(password))
        dialog.present(self)

    def on_web_port(self, row, _p):
        self.win.settings["web_port"] = int(row.get_value())
        web = self.win.web
        if web.running and web.port != int(row.get_value()):
            web.stop()
            err = web.start()
            if err:
                self.add_toast(Adw.Toast(title=GLib.markup_escape_text(f"Web UI: {err}")))
        self.show_web_state()

    def on_web_password(self, row):
        text = row.get_text()
        if len(text) < 8:
            self.add_toast(Adw.Toast(title="Use at least 8 characters"))
            return
        try:
            self.win.web.set_password(text)
        except Exception as e:  # noqa: BLE001
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(str(e))))
            return
        row.set_text("")
        self.add_toast(Adw.Toast(title="Password saved. Browsers have to sign in again."))

    def storage_page(self):
        page = Adw.PreferencesPage(title="Storage", name="storage",
                                   icon_name="drive-harddisk-symbolic")
        self.logs_group = Adw.PreferencesGroup(
            title="Log Files",
            description=f"Logs above {fmt_size(LOG_LIMIT)} are marked. Info and Debug "
                        "console levels can make them grow by gigabytes.")
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh",
                             valign=Gtk.Align.CENTER, css_classes=["flat"])
        refresh.connect("clicked", lambda *_: self.show_logs())
        self.logs_group.set_header_suffix(refresh)
        self.log_rows = []
        page.add(self.logs_group)

        trim = Adw.PreferencesGroup()
        row = Adw.SwitchRow(
            title="Trim Automatically",
            subtitle=f"Keep aria2.log under {fmt_size(LOG_LIMIT)}, saving its last "
                     f"{fmt_size(LOG_KEEP)} as aria2.log.old",
            active=self.win.settings.get("log_autotrim", True))
        row.connect("notify::active",
                    lambda r, _p: self.win.settings.__setitem__("log_autotrim", r.get_active()))
        trim.add(row)
        page.add(trim)
        self.show_logs()
        return page

    def show_logs(self):
        for r in self.log_rows:
            self.logs_group.remove(r)
        self.log_rows = []
        files = log_files()
        if not files:
            r = Adw.ActionRow(title="No log files")
            self.logs_group.add(r)
            self.log_rows.append(r)
            return
        for path, kind, size in files:
            r = Adw.ActionRow(title=path.name, subtitle=f"{self.LOG_KINDS[kind]}\n{path}",
                              subtitle_lines=2, title_selectable=False)
            if size > LOG_LIMIT:
                r.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic",
                                       css_classes=["warning"],
                                       tooltip_text="Large log file"))
            label = Gtk.Label(label=fmt_size(size), css_classes=["numeric"],
                              valign=Gtk.Align.CENTER)
            label.add_css_class("warning" if size > LOG_LIMIT else "dim-label")
            r.add_suffix(label)
            btn = Gtk.Button(label="Delete" if kind == "old" else "Empty",
                             valign=Gtk.Align.CENTER, sensitive=size > 0,
                             tooltip_text="Delete this file" if kind == "old"
                             else "Empty it; logging carries on in the same file")
            if size > LOG_LIMIT:
                btn.add_css_class("destructive-action")
            btn.connect("clicked", self.on_clear_log, path, kind, size)
            r.add_suffix(btn)
            self.logs_group.add(r)
            self.log_rows.append(r)

    def on_clear_log(self, _btn, path, kind, size):
        verb = "Delete" if kind == "old" else "Empty"
        dialog = Adw.AlertDialog(
            heading=f"{verb} {path.name}?",
            body=f"This frees {fmt_size(size)} and cannot be undone. Downloads are not "
                 "affected; only the log history is lost.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("clear", verb)
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def answered(_d, response):
            if response != "clear":
                return
            try:
                clear_log(path, kind)
                msg = f"{fmt_size(size)} freed"
            except OSError as e:
                msg = f"Could not {verb.lower()} {path.name}: {e.strerror}"
            self.show_logs()
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg)))

        dialog.connect("response", answered)
        dialog.present(self)

    def on_reset(self, _row):
        self.win.settings.pop("aria2", None)
        self.win.settings.pop("connections", None)
        save_settings(self.win.settings)
        self.close()
        self.win.restart_aria2()

    def on_opacity(self, adj):
        alpha = adj.get_value() / 100
        self.win.settings["opacity"] = alpha
        self.win.apply_opacity(alpha)

    def on_copy_id(self, _btn):
        provider = Gdk.ContentProvider.new_for_bytes(
            "text/plain;charset=utf-8", GLib.Bytes.new(APP_ID.encode()))
        self.get_clipboard().set_content(provider)
        self.add_toast(Adw.Toast(title="Window class copied"))


# --------------------------------------------------------------- seeding ---

class SeedRow(Adw.ActionRow):
    """One torrent on the Seeding tab."""

    def __init__(self, page, seed):
        title = os.path.basename(seed["torrent"])
        if title.endswith(".torrent"):
            title = title[:-len(".torrent")]
        super().__init__(title=GLib.markup_escape_text(title), title_lines=1, subtitle_lines=2)
        self.set_tooltip_text(seed["source"])
        self.icon_stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                                    valign=Gtk.Align.CENTER)
        self.icon_stack.add_named(Gtk.Image(icon_name="emblem-shared-symbolic", pixel_size=24),
                                  "icon")
        self.icon_stack.add_named(Adw.Spinner(width_request=24, height_request=24), "busy")
        self.add_prefix(self.icon_stack)
        self.toggle = Gtk.Button(icon_name="media-playback-start-symbolic",
                                 valign=Gtk.Align.CENTER, tooltip_text="Start",
                                 css_classes=["flat", "circular"])
        self.toggle.connect("clicked", lambda _b: page.toggle(seed))
        folder = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Show Data in Folder", css_classes=["flat", "circular"])
        folder.connect("clicked", lambda _b: page.show_folder(seed))
        remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove from List (files are kept)",
                            css_classes=["flat", "circular"])
        remove.connect("clicked", lambda _b: page.remove(seed))
        peers = Gtk.Button(icon_name="system-users-symbolic", valign=Gtk.Align.CENTER,
                           tooltip_text="Peers", css_classes=["flat", "circular"])
        peers.connect("clicked", lambda _b: PeersWindow(
            page.win, seed.get("gid"), seed.get("name") or title, seed=seed).present())
        for btn in (self.toggle, peers, folder, remove):
            self.add_suffix(btn)
        self.error = False
        self.page, self.seed, self.status_text = page, seed, ""
        on_right_click(self, self.show_limits)
        self.set_status("Stopped", False)

    def show_limits(self, x, y):
        kib = self.seed.get("upload_limit")
        if kib is None:
            kib = limit_to_kib(self.page.win.settings.get("seed_upload_limit", "0"))
        popup_at(self, SpeedLimitPopover(self, self.get_title(), ["up"], {"up": kib},
                                         lambda lim: self.page.set_seed_limit(self.seed,
                                                                              lim["up"])), x, y)

    def set_status(self, text, running, error=False):
        self.error = error
        self.status_text, self.running = text, running
        # spinner while it starts / checks files / fetches data, icon once it seeds
        busy = running and not error and not text.startswith("Seeding")
        self.icon_stack.set_visible_child_name("busy" if busy else "icon")
        if self.seed.get("upload_limit"):
            text += f"  ·  max ↑ {fmt_size(self.seed['upload_limit'] * 1024)}/s"
        self.set_subtitle(GLib.markup_escape_text(text))
        if error:
            self.add_css_class("error")
        else:
            self.remove_css_class("error")
        self.toggle.set_icon_name("media-playback-pause-symbolic" if running
                                  else "media-playback-start-symbolic")
        self.toggle.set_tooltip_text("Pause" if running else "Start")


class SeedingPage(Gtk.Box):
    """Your own torrents, seeded forever by the app's aria2c (one engine, one port)."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.seeds = []

        add = group("Add a Seed",
                    "Pick the torrent and the original data it was made from. "
                    "aria2c checks the files, then seeds until you stop it.")
        self.torrent_row = PathRow("Torrent File", "Choose a .torrent", modes=("file",),
                                   filter_name="Torrent files", filter_glob="*.torrent")
        self.source_row = PathRow("Original Folder or File", "Choose the data you shared",
                                  modes=("folder", "file"))
        add_row = Adw.ActionRow(title="Add to the List", subtitle="Starts seeding right away")
        add_btn = Gtk.Button(label="Add Seed", valign=Gtk.Align.CENTER,
                             css_classes=["suggested-action"])
        add_btn.connect("clicked", self.on_add)
        add_row.add_suffix(add_btn)
        for row in (self.torrent_row, self.source_row, add_row):
            add.add(row)

        seeds = group("Seeds")
        buttons = Gtk.Box(spacing=6)
        for label, cb in (("Start All", self.start_all), ("Pause All", self.pause_all)):
            btn = Gtk.Button(label=label, css_classes=["flat"])
            btn.connect("clicked", lambda _b, f=cb: f())
            buttons.append(btn)
        seeds.set_header_suffix(buttons)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                   css_classes=["boxed-list", "glow-list"])
        self.listbox.set_placeholder(Gtk.Label(label="No seeds yet. Add one above, or make one "
                                                     "on the Create tab.",
                                               margin_top=18, margin_bottom=18, wrap=True,
                                               css_classes=["dim-label"]))
        seeds.add(self.listbox)
        self.summary = Gtk.Label(label="", css_classes=["dim-label", "caption", "numeric"])
        self.append(make_page(add, seeds, self.summary))

        for item in win.settings.get("seeds", []):
            if isinstance(item, dict) and item.get("torrent") and item.get("source"):
                self._add_entry(item["torrent"], item["source"], item.get("want", True),
                                item.get("upload_limit"))

    # -- list

    def _add_entry(self, torrent, source, want=True, upload_limit=None):
        meta = torrent_meta(torrent)
        seed = {"torrent": torrent, "source": source, "want": want, "gid": None, "state": "",
                "starting": False, "hash": meta[0] if meta else None,
                "name": meta[1] if meta else "", "upload_limit": upload_limit}
        seed["row"] = SeedRow(self, seed)
        self.seeds.append(seed)
        self.listbox.append(seed["row"])
        return seed

    def save(self):
        self.win.settings["seeds"] = [
            {"torrent": s["torrent"], "source": s["source"], "want": s["want"],
             **({"upload_limit": s["upload_limit"]} if s.get("upload_limit") is not None else {})}
            for s in self.seeds]
        save_settings(self.win.settings)

    def hashes(self):
        return {s["hash"]: s for s in self.seeds if s["hash"]}

    def add_seed(self, torrent, source, start=True):
        """Add (or replace a re-created torrent with the same path) and optionally start."""
        meta = torrent_meta(torrent)
        if not meta:
            self.win.toast("That is not a valid .torrent file")
            return None
        for old in [s for s in self.seeds if s["torrent"] == torrent or s["hash"] == meta[0]]:
            if old["torrent"] == torrent and old["hash"] != meta[0]:
                self.remove(old)                      # same file, new content: replace
            else:
                self.win.toast("This torrent is already on the list")
                return old
        seed = self._add_entry(torrent, source)
        self.save()
        if start:
            self.start(seed)
        return seed

    def on_add(self, _btn):
        torrent, source = self.torrent_row.path, self.source_row.path.rstrip("/")
        if not torrent or not os.path.isfile(torrent):
            self.win.toast("Choose the .torrent file")
            return
        if not source or not os.path.exists(source):
            self.win.toast("Choose the original folder or file")
            return
        if self.add_seed(torrent, source):
            self.torrent_row.set_path("")
            self.source_row.set_path("")

    def remove(self, seed):
        gid, rpc = seed.get("gid"), self.win.rpc
        if gid and rpc:
            def work():
                try:
                    rpc.call("aria2.forceRemove", gid)
                    for _ in range(50):
                        if rpc.call("aria2.tellStatus", gid, ["status"])["status"] == "removed":
                            break
                        time.sleep(0.1)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    rpc.call("aria2.removeDownloadResult", gid)
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=work, daemon=True).start()
        if seed in self.seeds:
            self.seeds.remove(seed)
            self.listbox.remove(seed["row"])
            self.save()

    def show_folder(self, seed):
        src = seed["source"].rstrip("/")
        if os.path.exists(src):
            Gtk.FileLauncher(file=Gio.File.new_for_path(src)).open_containing_folder(
                self.win, None, None)
        else:
            self.win.toast("The original data is not there any more")

    # -- engine control

    def seed_limit(self, seed):
        """This seed's own upload limit for aria2, else the Preferences one."""
        if seed and seed.get("upload_limit") is not None:
            return f"{seed['upload_limit']}K" if seed["upload_limit"] else "0"
        return norm_limit(self.win.settings.get("seed_upload_limit", "0"))

    def set_seed_limit(self, seed, kib):
        seed["upload_limit"] = kib
        self.save()
        seed["row"].set_status(seed["row"].status_text, seed["row"].running, seed["row"].error)
        rpc, gid = self.win.rpc, seed.get("gid")
        if rpc and gid:
            limit = self.seed_limit(seed)
            self.win.run(lambda: rpc.call("aria2.changeOption", gid, {"max-upload-limit": limit}))

    def seed_options(self, src, seed=None):
        # rpc-save-upload-metadata off: otherwise aria2 drops <infohash>.torrent next to your data
        opts = {"dir": os.path.dirname(src), "check-integrity": "true", "seed-ratio": "0.0",
                "seed-time": SEED_FOREVER, "rpc-save-upload-metadata": "false",
                "max-upload-limit": self.seed_limit(seed)}
        ip = (self.win.settings.get("external_ip") or "").strip()
        if ip:
            opts["bt-external-ip"] = ip
        return opts

    def start(self, seed):
        rpc = self.win.rpc
        if not rpc:
            self.win.toast("aria2c isn't running yet")
            return
        row = seed["row"]
        torrent, src = seed["torrent"], seed["source"].rstrip("/")
        if not os.path.isfile(torrent):
            row.set_status("Torrent file not found", False, True)
            return
        if not os.path.exists(src):
            row.set_status("Original data not found", False, True)
            return
        meta = torrent_meta(torrent)
        if not meta:
            row.set_status("Not a valid .torrent file", False, True)
            return
        seed["hash"], seed["name"] = meta
        if meta[1] and os.path.basename(src) != meta[1]:
            # aria2 looks for <dir>/<name>; a different name would start a fresh download
            row.set_status(f"The data must be named “{meta[1]}” to match the torrent",
                           False, True)
            return
        try:
            data = base64.b64encode(Path(torrent).read_bytes()).decode()
        except OSError as e:
            row.set_status(f"Cannot read the torrent: {e.strerror}", False, True)
            return
        seed["want"], seed["starting"] = True, True
        self.save()
        row.set_status("Starting…", True)
        opts, info_hash = self.seed_options(src, seed), seed["hash"]

        def work():
            try:
                rpc.call("aria2.addTorrent", data, [], opts, timeout=60)
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "already" not in msg.lower():
                    GLib.idle_add(self._failed, seed, msg)
                    return
                try:  # same torrent already in aria2c: resume it instead
                    keys = ["gid", "infoHash", "status"]
                    for d in rpc.call("aria2.tellWaiting", 0, 1000, keys):
                        if d.get("infoHash") == info_hash and d["status"] == "paused":
                            rpc.call("aria2.unpause", d["gid"])
                except Exception:  # noqa: BLE001
                    pass
            GLib.idle_add(lambda: self.win.poll() and False)

        threading.Thread(target=work, daemon=True).start()

    def _failed(self, seed, msg):
        seed["starting"] = False
        seed["row"].set_status(f"Error: {msg}", False, True)
        return False

    def _rpc_bg(self, method, gid):
        rpc = self.win.rpc

        def work():
            try:
                rpc.call(method, gid)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.win.toast, str(e))
            GLib.idle_add(lambda: self.win.poll() and False)

        threading.Thread(target=work, daemon=True).start()

    def toggle(self, seed):
        gid, state = seed.get("gid"), seed.get("state")
        if gid and self.win.rpc and state in ("active", "waiting"):
            seed["want"] = False
            self.save()
            self._rpc_bg("aria2.forcePause", gid)
        elif gid and self.win.rpc and state == "paused":
            seed["want"] = True
            self.save()
            self._rpc_bg("aria2.unpause", gid)
        else:
            if gid and self.win.rpc:   # finished/failed entry: clear it before re-adding
                try:
                    self.win.rpc.call("aria2.removeDownloadResult", gid, timeout=3)
                except Exception:  # noqa: BLE001
                    pass
            self.start(seed)

    def start_all(self):
        for seed in self.seeds:
            if seed.get("state") == "paused" and seed.get("gid"):
                seed["want"] = True
                self._rpc_bg("aria2.unpause", seed["gid"])
            elif not seed.get("gid") and not seed.get("starting"):
                self.start(seed)
        self.save()

    def pause_all(self):
        for seed in self.seeds:
            if seed.get("gid") and seed.get("state") in ("active", "waiting"):
                seed["want"] = False
                self._rpc_bg("aria2.forcePause", seed["gid"])
        self.save()

    def resume_wanted(self):
        """After aria2c (re)starts: bring back the seeds that were meant to be running."""
        for seed in self.seeds:
            if seed["want"] and not seed.get("gid") and not seed.get("starting"):
                self.start(seed)

    def apply_upload_limit(self):
        rpc = self.win.rpc
        limit = norm_limit(self.win.settings.get("seed_upload_limit", "0"))
        gids = [s["gid"] for s in self.seeds if s.get("gid") and s.get("upload_limit") is None]
        if not rpc or not gids:
            return

        def work():
            for gid in gids:
                try:
                    rpc.call("aria2.changeOption", gid, {"max-upload-limit": limit})
                except Exception:  # noqa: BLE001
                    pass

        threading.Thread(target=work, daemon=True).start()

    def update(self, items):
        """items: infoHash -> aria2 status dict. Returns (active seeds, upload speed)."""
        active = up = 0
        for seed in self.seeds:
            row = seed["row"]
            d = items.get(seed.get("hash"))
            if d:
                seed["starting"] = False
                seed["gid"] = d["gid"]
                text, running, state = describe_seed(d)
                seed["state"] = state
                row.set_status(text, running, state == "error")
                if state == "active":
                    active += 1
                    up += int(d.get("uploadSpeed", 0) or 0)
            else:
                seed["gid"], seed["state"] = None, ""
                if not seed["starting"] and not row.error:
                    row.set_status("Stopped", False)
        if self.seeds:
            self.summary.set_label(f"{active} of {len(self.seeds)} seeding  ·  "
                                   f"↑ {fmt_speed(up)}")
        else:
            self.summary.set_label("")
        return active, up


# ---------------------------------------------------------------- create ---

class CreatePage(Gtk.Box):
    """Make a .torrent with mktorrent for your own tracker (opentracker)."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        s = win.settings
        self.last_torrent = self.last_source = self.magnet = ""
        self._pulse_id = 0
        self._got_progress = False

        what = group("What to Share", "Pick the folder (or a single file) your friends should get.")
        self.src_row = PathRow("Folder or File", "Nothing selected", modes=("folder", "file"))
        what.add(self.src_row)

        tracker = group("Your Tracker",
                        "Your opentracker service. Forward this port (TCP and UDP) on your router.")
        self.host_row = Adw.EntryRow(title="Public IP or Domain", text=s.get("tracker_host", ""))
        detect = Gtk.Button(label="Detect", valign=Gtk.Align.CENTER, css_classes=["flat"])
        detect.connect("clicked", self.on_detect_ip)
        self.host_row.add_suffix(detect)
        self.port_row = Adw.EntryRow(title="Tracker Port", text=str(s.get("tracker_port", "6969")))
        self.extra_row = Adw.EntryRow(title="Extra Trackers (optional, space separated)",
                                      text=s.get("extra_trackers", ""))
        for row in (self.host_row, self.port_row, self.extra_row):
            tracker.add(row)

        opts = group("Options")
        self.piece_row = Adw.ComboRow(title="Piece Size",
                                      subtitle="Auto picks a good size from the total data size",
                                      model=Gtk.StringList.new(PIECE_CHOICES))
        self.piece_row.set_selected(min(int(s.get("piece", 0)), len(PIECE_CHOICES) - 1))
        self.private_row = Adw.SwitchRow(
            title="Private Torrent",
            subtitle="Only your tracker, no DHT. Friends need the .torrent file (a magnet won't work).",
            active=bool(s.get("private", True)))
        self.dir_row = FolderEntryRow("Save .torrent Files In (type a new folder or browse)")
        self.dir_row.set_path(s.get("torrent_dir", os.path.expanduser("~/torrents")))
        for row in (self.piece_row, self.private_row, self.dir_row):
            opts.add(row)

        self.create_btn = Gtk.Button(label="Create Torrent", halign=Gtk.Align.CENTER,
                                     css_classes=["suggested-action", "pill"])
        self.create_btn.connect("clicked", self.on_create)
        self.progress = Gtk.ProgressBar(visible=False)
        self.status = Gtk.Label(label="", xalign=0, wrap=True, css_classes=["dim-label"])

        self.result = group("Done")
        self.result.set_visible(False)
        self.file_row = Adw.ActionRow(title="Torrent File", subtitle_selectable=True)
        self.magnet_row = Adw.ActionRow(title="Magnet Link", subtitle_selectable=True,
                                        subtitle_lines=3)
        for row, getter, tip in ((self.file_row, lambda: self.last_torrent, "Copy Path"),
                                 (self.magnet_row, lambda: self.magnet, "Copy Magnet")):
            btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text=tip, css_classes=["flat"])
            btn.connect("clicked", lambda b, g=getter: self.copy(b, g()))
            row.add_suffix(btn)
        self.copy_magnet_btn = btn
        seed_row = Adw.ActionRow(title="Start Sharing Now",
                                 subtitle="Adds it to the Seeding tab and starts seeding")
        go = Gtk.Button(label="Seed Now", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        go.connect("clicked", self.on_seed_now)
        seed_row.add_suffix(go)
        for row in (self.file_row, self.magnet_row, seed_row):
            self.result.add(row)

        self.log = LogView(140)
        self.append(make_page(what, tracker, opts, self.create_btn, self.progress, self.status,
                              self.result, self.log))

    def copy(self, widget, text):
        if text:
            copy_text(widget, text)
            self.win.toast("Copied")

    def remember(self):
        self.win.settings.update(
            tracker_host=self.host_row.get_text().strip(),
            tracker_port=self.port_row.get_text().strip() or "6969",
            extra_trackers=self.extra_row.get_text().strip(),
            piece=self.piece_row.get_selected(),
            private=self.private_row.get_active(),
            torrent_dir=self.dir_row.path or os.path.expanduser("~/torrents"))
        save_settings(self.win.settings)

    def on_detect_ip(self, _btn):
        self.win.toast("Detecting public IP…")

        def work():
            ip = ""
            try:
                with urllib.request.urlopen("https://ifconfig.me/ip", timeout=6) as r:
                    ip = r.read().decode().strip()
            except Exception:  # noqa: BLE001
                pass
            GLib.idle_add(self._ip_done, ip)

        threading.Thread(target=work, daemon=True).start()

    def _ip_done(self, ip):
        if ip:
            self.host_row.set_text(ip)
            self.win.toast(f"Public IP: {ip}")
        else:
            self.win.toast("Could not detect the IP, type it in")
        return False

    def on_create(self, _btn):
        src = self.src_row.path.rstrip("/")
        host = self.host_row.get_text().strip()
        port = self.port_row.get_text().strip() or "6969"
        if not shutil.which("mktorrent"):
            self.win.toast("mktorrent is missing: sudo pacman -S mktorrent")
            return
        if not src or not os.path.exists(src):
            self.win.toast("Choose a folder or file to share")
            return
        if not host:
            self.win.toast("Enter your public IP or domain (or press Detect)")
            return
        self.remember()
        out_dir = self.dir_row.path or os.path.expanduser("~/torrents")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as exc:
            self.win.toast(f"Cannot create {out_dir}: {exc.strerror}")
            return
        out = os.path.join(out_dir, os.path.basename(src) + ".torrent")
        trackers = [f"udp://{host}:{port}/announce"] + self.extra_row.get_text().split()
        exp_choice = PIECE_EXPS[self.piece_row.get_selected()]
        private = self.private_row.get_active()

        self.create_btn.set_sensitive(False)
        self.result.set_visible(False)
        self.log.clear()
        self.progress.set_visible(True)
        self.progress.set_fraction(0)
        self._got_progress = False
        self._pulse_id = GLib.timeout_add(150, self._pulse)
        self.status.set_label("Measuring size…")
        threading.Thread(target=self._worker, args=(src, out, trackers, exp_choice, private),
                         daemon=True).start()

    def _pulse(self):
        if not self.progress.get_visible():
            self._pulse_id = 0
            return False
        if not self._got_progress:
            self.progress.pulse()
        return True

    def _ui(self, fn, *args):
        GLib.idle_add(lambda: fn(*args) and False)

    def _worker(self, src, out, trackers, exp_choice, private):
        size = path_size(src)
        exp = exp_choice or auto_exp(size)
        self._ui(self.status.set_label,
                 f"Hashing {fmt_size(size)} with {fmt_size(1 << exp)} pieces… "
                 "(this can take a while)")
        self._ui(self.log.append, f"Source: {src}\nSize: {fmt_size(size)}  piece: 2^{exp}")
        cmd = ["mktorrent", "-l", str(exp), "-t", str(min(os.cpu_count() or 2, 20))]
        if private:
            cmd.append("-p")
        for t in trackers:
            cmd += ["-a", t]
        cmd += ["-o", out, src]
        self._ui(self.log.append, "$ " + " ".join(cmd))
        try:
            if os.path.exists(out):
                os.remove(out)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            self._ui(self._done, False, out, src, "", f"Could not start mktorrent: {exc}", private)
            return
        pending = ""
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            pending += chunk.decode(errors="replace")
            parts = re.split(r"[\r\n]", pending)
            pending = parts.pop()
            for part in (p.strip() for p in parts):
                if not part:
                    continue
                m = HASH_RE.search(part)
                if m:
                    self._ui(self._set_progress, int(m.group(1)) / max(int(m.group(2)), 1))
                else:
                    self._ui(self.log.append, part)
        proc.wait()
        ok = proc.returncode == 0 and os.path.exists(out)
        magnet = get_torrent_info(out)[1] if ok else ""
        self._ui(self._done, ok, out, src, magnet, "" if ok else "mktorrent failed, see the log",
                 private)

    def _set_progress(self, frac):
        self._got_progress = True
        self.progress.set_fraction(min(frac, 1.0))

    def _done(self, ok, out, src, magnet, error, private):
        self.create_btn.set_sensitive(True)
        self.progress.set_visible(False)
        if not ok:
            self.status.set_label(error)
            self.win.toast("Creating the torrent failed")
            return
        self.status.set_label("Torrent created.")
        self.last_torrent, self.last_source, self.magnet = out, src, magnet
        self.file_row.set_subtitle(GLib.markup_escape_text(out))
        text = magnet or "no magnet available"
        if private:
            text += "  (private torrent: send the .torrent file instead)"
        self.magnet_row.set_subtitle(GLib.markup_escape_text(text))
        self.copy_magnet_btn.set_sensitive(bool(magnet))
        self.result.set_visible(True)
        self.log.append(f"Created {out}")
        self.win.toast("Torrent created")

    def on_seed_now(self, _btn):
        if self.last_torrent and self.win.seeding.add_seed(self.last_torrent, self.last_source):
            self.win.stack.set_visible_child_name("seeding")


# ----------------------------------------------------------------- tools ---

class TorrentInfoDialog(Adw.Dialog):
    def __init__(self, win, path=""):
        super().__init__(title="Torrent Info & Magnet", content_width=640, content_height=620)
        self.win = win
        self.magnet = ""
        pick = group(description="Read from the .torrent file with aria2c. Private torrents "
                                 "usually need the file itself.")
        self.torrent_row = PathRow("Torrent File", "Choose a .torrent", modes=("file",),
                                   filter_name="Torrent files", filter_glob="*.torrent")
        if path:
            self.torrent_row.set_path(path)
        pick.add(self.torrent_row)
        show = Gtk.Button(label="Show Info and Magnet", halign=Gtk.Align.CENTER,
                          css_classes=["suggested-action", "pill"])
        show.connect("clicked", self.on_show)
        self.mag_group = group("Magnet")
        self.mag_group.set_visible(False)
        self.mag_row = Adw.ActionRow(title="Magnet URI", subtitle_selectable=True,
                                     subtitle_lines=4)
        self.copy_btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="Copy Magnet", css_classes=["flat"])
        self.copy_btn.connect("clicked", self.on_copy)
        self.mag_row.add_suffix(self.copy_btn)
        self.mag_group.add(self.mag_row)
        hint = Gtk.Label(label="Friends download with:\n  aria2c -d /where/to/save "
                               "\"MAGNET_OR_FILE.torrent\"",
                         xalign=0, selectable=True, css_classes=["dim-label"])
        self.log = LogView(220)
        tv = Adw.ToolbarView(content=make_page(pick, show, self.mag_group, hint, self.log))
        tv.add_top_bar(Adw.HeaderBar())
        self.toasts = Adw.ToastOverlay(child=tv)
        self.set_child(self.toasts)
        if path:
            self.on_show(None)

    def on_show(self, _btn):
        path = self.torrent_row.path
        if not path or not os.path.isfile(path):
            self.toasts.add_toast(Adw.Toast(title="Choose the .torrent file"))
            return

        def work():
            text, magnet = get_torrent_info(path)
            GLib.idle_add(self._done, text, magnet)

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text, magnet):
        self.log.clear()
        self.log.append(text)
        self.magnet = magnet
        self.mag_group.set_visible(True)
        self.mag_row.set_subtitle(GLib.markup_escape_text(magnet or "aria2c printed no magnet URI"))
        self.copy_btn.set_sensitive(bool(magnet))
        return False

    def on_copy(self, btn):
        if self.magnet:
            copy_text(btn, self.magnet)
            self.toasts.add_toast(Adw.Toast(title="Magnet copied"))


class TrackerDialog(Adw.Dialog):
    def __init__(self, win):
        super().__init__(title="Tracker Status", content_width=640, content_height=560)
        self.port = str(win.settings.get("tracker_port", "6969")).strip() or "6969"
        info = group(description=f"Checks opentracker.service, port {self.port} and its "
                                 "stats page. The port comes from the Create tab.")
        check = Gtk.Button(label="Check Now", halign=Gtk.Align.CENTER,
                           css_classes=["suggested-action", "pill"])
        check.connect("clicked", lambda _b: self.check())
        self.log = LogView(320)
        tv = Adw.ToolbarView(content=make_page(info, check, self.log))
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)
        self.check()

    def check(self):
        port = self.port
        self.log.clear()
        self.log.append("Checking…")

        def run(cmd):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                return (r.stdout + r.stderr).strip()
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"error: {exc}"

        def work():
            lines = ["--- service ---", run(["systemctl", "is-active", "opentracker.service"]),
                     "", f"--- listening on port {port} ---"]
            hits = [ln for ln in run(["ss", "-tuln"]).splitlines()
                    if f":{port} " in ln or ln.endswith(f":{port}")]
            lines.append("\n".join(hits) if hits else f"nothing listening on {port}")
            lines += ["", "--- stats ---"]
            try:
                url = f"http://127.0.0.1:{port}/stats?mode=everything"
                with LOCAL_OPENER.open(url, timeout=3) as r:
                    lines.append(r.read().decode(errors="replace")[:2000])
            except Exception as exc:  # noqa: BLE001
                lines.append(f"no stats ({exc}). Check access.stats in "
                             "/etc/opentracker/opentracker.conf")
            GLib.idle_add(self._done, "\n".join(lines))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, text):
        self.log.clear()
        self.log.append(text)
        return False


# ------------------------------------------------------------ geolocation ---
# Peer countries from a local MaxMind-format database (DB-IP Country Lite, or MaxMind
# GeoLite2-Country). Lookups never leave this PC. Tiny built-in reader: no dependencies.

GEO_DB = DATA_DIR / "dbip-country-lite.mmdb"
GEO_SYSTEM = [Path("/usr/share/GeoIP/GeoLite2-Country.mmdb"),
              Path("/usr/share/GeoIP/dbip-country-lite.mmdb"),
              Path("/var/lib/GeoIP/GeoLite2-Country.mmdb")]
GEO_URL = "https://download.db-ip.com/free/dbip-country-lite-{month}.mmdb.gz"
GEO_CREDIT = "IP geolocation by DB-IP (db-ip.com), CC BY 4.0"


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


# ----------------------------------------------------------------- peers ---

# Azureus-style peer ids: -XXabcd-  (client code + version digits)
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


class PeersWindow(Adw.Window):
    """Live list of the peers aria2c is connected to for one torrent."""

    def __init__(self, win, gid, name, seed=None):
        size = win.settings.get("peers_window", {})
        super().__init__(title=f"Peers — {name}", transient_for=win,
                         default_width=size.get("width", 860),
                         default_height=size.get("height", 520))
        self.win, self.gid, self.seed, self.busy = win, gid, seed, False
        header = Adw.HeaderBar()
        self.summary = Gtk.Label(css_classes=["dim-label", "numeric"])
        header.pack_start(self.summary)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                   css_classes=["boxed-list"], valign=Gtk.Align.START)
        columns = Gtk.Box(spacing=12, margin_start=14, margin_end=14, margin_top=4)
        self.geo = geo_db()
        for text, width, expand in (("", 2, False), ("", 3 if self.geo else 0, False),
                                    ("Address", 26, False),
                                    ("Client", 20, True), ("Has", 12, False),
                                    ("↓ from it", 11, False), ("↑ to it", 11, False)):
            columns.append(Gtk.Label(label=text, xalign=0, width_chars=width, hexpand=expand,
                                     css_classes=["caption-heading", "dim-label"]))
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6,
                       margin_bottom=12, margin_start=12, margin_end=12)
        body.append(columns)
        body.append(Gtk.ScrolledWindow(child=self.listbox, vexpand=True,
                                       hscrollbar_policy=Gtk.PolicyType.NEVER))
        if self.geo and "dbip" in str(_geo["path"]):
            body.append(Gtk.Label(label=GEO_CREDIT, xalign=1,
                                  css_classes=["dim-label", "caption"]))
        elif not self.geo:
            body.append(Gtk.Label(label="Country flags: download the country database in "
                                        "Preferences → Network.", xalign=1,
                                  css_classes=["dim-label", "caption"]))
        # shown while there are no peers: says what aria2c is doing instead, with a spinner
        self.empty = Adw.StatusPage()
        self.spinner_paint = Adw.SpinnerPaintable.new(self.empty)
        self.check_bar = Gtk.ProgressBar(halign=Gtk.Align.CENTER, width_request=360,
                                         show_text=True, visible=False)
        self.empty.set_child(self.check_bar)
        self.opened = time.monotonic()
        self.stack = Gtk.Stack()
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(body, "list")
        tv = Adw.ToolbarView(content=self.stack)
        tv.add_top_bar(header)
        self.set_content(tv)
        keys = Gtk.ShortcutController()
        keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("Escape"),
                                           Gtk.CallbackAction.new(lambda *_: self.close())))
        self.add_controller(keys)
        self.connect("close-request", self.on_close)
        self.timer = GLib.timeout_add(1000, self.refresh)
        self.refresh()

    def current_gid(self):
        if self.seed is not None:                    # seeds get a new gid when restarted
            return self.seed.get("gid")
        return self.gid

    def refresh(self):
        if self.busy:
            return True
        rpc, gid = self.win.rpc, self.current_gid()
        if not rpc or not gid:
            self.show([], None)
            return True
        self.busy = True

        def work():
            try:
                res = rpc.multicall([
                    ("aria2.getPeers", gid),
                    ("aria2.tellStatus", gid, ["status", "numSeeders", "connections",
                                               "numPieces", "downloadSpeed", "uploadSpeed",
                                               "totalLength", "verifiedLength",
                                               "verifyIntegrityPending", "files"]),
                ])
            except Exception as e:  # noqa: BLE001  aria2c slow/busy: keep what we show
                GLib.idle_add(self.slow, str(e))
                return
            peers = res[0] if not isinstance(res[0], Exception) else []
            st = res[1] if not isinstance(res[1], Exception) else None
            GLib.idle_add(self.show, peers, st)

        threading.Thread(target=work, daemon=True).start()
        return True

    def slow(self, error):
        """A failed refresh (aria2c busy, e.g. writing to a network drive): keep the list."""
        self.busy = False
        self.fails = getattr(self, "fails", 0) + 1
        if self.fails >= 3:
            self.summary.set_label(f"aria2c is slow to answer ({error[:60]}), retrying…")
        return False

    def show(self, peers, st):
        self.busy = False
        self.fails = 0
        if st is None or st.get("status") not in ("active", "waiting"):
            self.summary.set_label("Not running" if st is None else f"Not running ({st['status']})")
            peers = []
        else:
            seeders = sum(1 for p in peers if p.get("seeder") == "true")
            self.summary.set_label(
                f"{len(peers)} connected  ·  {seeders} seeder{'s' if seeders != 1 else ''}"
                f" (swarm reports {st.get('numSeeders', 0)})  ·  "
                f"↓ {fmt_speed(st.get('downloadSpeed', 0))}  ↑ {fmt_speed(st.get('uploadSpeed', 0))}")
        pieces = int((st or {}).get("numPieces", 0) or 0)
        peers = sorted(peers, key=lambda p: (p.get("seeder") != "true",
                                             -int(p.get("downloadSpeed", 0) or 0),
                                             -int(p.get("uploadSpeed", 0) or 0)))
        child = self.listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.listbox.remove(child)
            child = nxt
        for p in peers:
            self.listbox.append(self.peer_row(p, pieces))
        if not peers:
            self.waiting_page(st)
        self.stack.set_visible_child_name("list" if peers else "empty")
        return False

    def waiting_page(self, st):
        """No peers to list: say what aria2c is busy with (spinner), or that it's stopped."""
        page, bar = self.empty, self.check_bar
        bar.set_visible(False)
        if st is None or st.get("status") not in ("active", "waiting"):
            page.set_paintable(None)
            page.set_icon_name("media-playback-pause-symbolic")
            page.set_title("Not Running")
            page.set_description("Start the torrent to see who shares it.")
            return
        page.set_paintable(self.spinner_paint)
        total = int(st.get("totalLength", 0) or 0)
        verified = int(st.get("verifiedLength", 0) or 0)
        files = st.get("files") or []
        waited = fmt_clock(time.monotonic() - self.opened)
        if st.get("verifyIntegrityPending") == "true":
            page.set_title("Waiting to Check Files")
            page.set_description("aria2c checks the data already on disk first, "
                                 "then it connects to peers.")
        elif total and 0 < verified < total:
            page.set_title("Checking Existing Files")
            page.set_description(f"{fmt_size(verified)} of {fmt_size(total)} checked. "
                                 "aria2c connects to peers as soon as this finishes.")
            bar.set_fraction(verified / total)
            bar.set_text(f"{verified * 100 // total}%")
            bar.set_visible(True)
        elif files and os.path.basename(files[0].get("path", "")).startswith("[METADATA]"):
            page.set_title("Getting Torrent Info")
            page.set_description(f"Asking peers for the file list…  {waited}")
        elif st.get("status") == "waiting":
            page.set_title("Queued")
            page.set_description("It starts when another download finishes.")
        else:
            page.set_title("Looking for Peers")
            page.set_description(f"Asking trackers and DHT who shares this torrent…  {waited}")

    def peer_row(self, p, pieces):
        seeder = p.get("seeder") == "true"
        ip = p.get("ip", "?")
        port = str(p.get("port") or "0")
        host = f"[{ip}]" if ":" in ip else ip
        # port 0: the peer connected to us and never told its own listening port
        addr = f"{host} (incoming)" if port == "0" else f"{host}:{port}"
        frac = 1.0 if seeder else bitfield_fraction(p.get("bitfield"), pieces)
        box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=10, margin_end=10)
        icon = Gtk.Image(icon_name="emblem-shared-symbolic" if seeder else "system-users-symbolic",
                         tooltip_text="Seeder (has everything)" if seeder else "Peer")
        box.append(icon)
        if self.geo:
            where = self.geo.country(ip)
            box.append(Gtk.Label(label=flag(where[0]) if where else "🌐", width_chars=3,
                                 tooltip_text=f"{where[1]} ({where[0]})" if where
                                 else "Unknown or private address"))
        box.append(Gtk.Label(label=addr, xalign=0, width_chars=26, selectable=True,
                             ellipsize=Pango.EllipsizeMode.END, css_classes=["numeric"]))
        box.append(Gtk.Label(label=peer_client(p.get("peerId")), xalign=0, width_chars=20,
                             hexpand=True, ellipsize=Pango.EllipsizeMode.END))
        has = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, valign=Gtk.Align.CENTER,
                      width_request=96)
        has.append(Gtk.Label(label=f"{frac:.0%}", xalign=0, css_classes=["caption", "numeric"]))
        has.append(Gtk.ProgressBar(fraction=frac))
        box.append(has)
        for key in ("downloadSpeed", "uploadSpeed"):
            speed = int(p.get(key, 0) or 0)
            if speed > 0:
                cell = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
                cell.append(Gtk.Label(label=f"{fmt_size(speed)}/s", xalign=0, width_chars=11,
                                      css_classes=["numeric"]))
                bits = GLib.format_size_full(speed * 8, GLib.FormatSizeFlags.BITS)
                cell.append(Gtk.Label(label=f"{bits}/s", xalign=0, width_chars=11,
                                      css_classes=["numeric", "caption", "dim-label"]))
                box.append(cell)
            else:
                box.append(Gtk.Label(label="–", xalign=0, width_chars=11,
                                     css_classes=["numeric", "dim-label"]))
        flags = []
        flags.append("it chokes you" if p.get("peerChoking") == "true" else "it sends to you")
        flags.append("you choke it" if p.get("amChoking") == "true" else "you send to it")
        row = Gtk.ListBoxRow(activatable=False, child=box,
                             tooltip_text=f"{addr}\n{', '.join(flags)}")
        return row

    def on_close(self, *_):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        width, height = self.get_default_size()
        self.win.settings["peers_window"] = {"width": width, "height": height}
        save_settings(self.win.settings)
        return False


# ---------------------------------------------------------------- banner ---

def _speed_energy(down, up):
    """0..1 'how busy' value from transfer speed (log scale: 1 KB/s .. ~100 MB/s)."""
    return max(0.0, min(1.0, math.log10(1 + (down + up) / 1024) / 5))


def _wave_y(x, h, i, t, energy):
    amp = (7 + 4 * i) * (0.55 + 0.75 * energy)
    base = h * 0.64 + i * 7
    return (base + amp * math.sin(x / (170 + 55 * i) * 2 * math.pi + t * (1 + 0.35 * i))
            + amp * 0.35 * math.sin(x / (61 + 13 * i) + t * 1.7))


def _layout(cr, text, font, w=None):
    lay = PangoCairo.create_layout(cr)
    lay.set_font_description(Pango.FontDescription.from_string(font))
    lay.set_text(text, -1)
    if w:
        lay.set_width(int(w * Pango.SCALE))
        lay.set_ellipsize(Pango.EllipsizeMode.END)
    return lay


def draw_banner(cr, w, h, t, fg, accent, state):
    """Paint the header banner. fg/accent: (r, g, b) 0..1. state: down, up, history,
    particles, active, seeding. Translucent, so the window's acrylic shows through."""
    ar, ag, ab = accent
    fr, fgc, fb = fg
    energy = _speed_energy(state["down"], state["up"])

    # 1. accent wash, fading out to the right and to the bottom
    wash = cairo.LinearGradient(0, 0, w, 0)
    wash.add_color_stop_rgba(0, ar, ag, ab, 0.30)
    wash.add_color_stop_rgba(0.55, ar, ag, ab, 0.10)
    wash.add_color_stop_rgba(1, ar, ag, ab, 0.16)
    cr.set_source(wash)
    cr.rectangle(0, 0, w, h)
    cr.fill()
    fade = cairo.LinearGradient(0, h * 0.55, 0, h)
    fade.add_color_stop_rgba(0, 0, 0, 0, 0)
    fade.add_color_stop_rgba(1, ar * 0.2, ag * 0.2, ab * 0.2, 0.10)
    cr.set_source(fade)
    cr.rectangle(0, 0, w, h)
    cr.fill()

    # 2. speed history behind the speeds (right side): download as a filled area,
    #    upload as a thin light line on the same scale, so a greedy seed shows at a glance
    hist = list(state["history"])
    uhist = list(state["up_history"])
    gx0, gx1 = max(w - 320, w * 0.55), w - 12
    top = max(hist + uhist) or 1
    step = (gx1 - gx0) / max(len(hist) - 1, 1)
    up_col = (min(1, ar * 0.35 + 0.65), min(1, ag * 0.35 + 0.65), min(1, ab * 0.35 + 0.65))
    peak = None
    if any(hist):
        pts = [(gx0 + i * step, h - 6 - h * 0.46 * (v / top)) for i, v in enumerate(hist)]
        cr.move_to(gx0, h)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(gx1, h)
        cr.close_path()
        area = cairo.LinearGradient(0, h * 0.45, 0, h)
        area.add_color_stop_rgba(0, ar, ag, ab, 0.32)
        area.add_color_stop_rgba(1, ar, ag, ab, 0.0)
        cr.set_source(area)
        cr.fill()
        cr.move_to(*pts[0])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(ar, ag, ab, 0.85)
        cr.set_line_width(1.4)
        cr.stroke()
        i_max = max(range(len(hist)), key=lambda i: (hist[i], i))   # latest of equal peaks
        peak = (*pts[i_max], hist[i_max])
    if any(uhist):
        upts = [(gx0 + i * step, h - 6 - h * 0.46 * (v / top)) for i, v in enumerate(uhist)]
        cr.move_to(*upts[0])
        for x, y in upts[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(*up_col, 0.75)
        cr.set_line_width(1.1)
        cr.set_dash([4, 3])
        cr.stroke()
        cr.set_dash([])

    # 3+4 drawn into a group, then faded behind the title and the speeds so text stays clear
    cr.push_group()

    # 3. flowing waves with a soft glow
    for i in range(3):
        cr.move_to(0, _wave_y(0, h, i, t, energy))
        for x in range(6, int(w) + 7, 6):
            cr.line_to(x, _wave_y(x, h, i, t, energy))
        path = cr.copy_path()
        for width, alpha in ((7, 0.07), (3.5, 0.12), (1.5, 0.55 - 0.14 * i)):
            cr.new_path()
            cr.append_path(path)
            cr.set_source_rgba(ar, ag, ab, alpha)
            cr.set_line_width(width)
            cr.stroke()

    # 4. particles riding the waves: in = accent (left->right), out = light (right->left)
    for p in state["particles"]:
        if p["k"] >= state["visible_particles"]:
            continue
        x = p["x"] * w
        y = _wave_y(x, h, p["wave"], t, energy) + p["dy"]
        glow = 0.45 + 0.55 * (0.5 + 0.5 * math.sin(t * 3 + p["phase"]))
        r = p["size"]
        col = (ar, ag, ab) if p["dir"] > 0 else (min(1, ar * 0.4 + 0.6), min(1, ag * 0.4 + 0.6),
                                                  min(1, ab * 0.4 + 0.6))
        halo = cairo.RadialGradient(x, y, 0, x, y, r * 4)
        halo.add_color_stop_rgba(0, *col, 0.55 * glow)
        halo.add_color_stop_rgba(1, *col, 0)
        cr.set_source(halo)
        cr.arc(x, y, r * 4, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.85 * glow)
        cr.arc(x, y, r * 0.8, 0, 2 * math.pi)
        cr.fill()

    waves = cr.pop_group()
    text_end = min(48 + 25 + 20 + 290, w * 0.5)
    mask = cairo.LinearGradient(0, 0, w, 0)
    mask.add_color_stop_rgba(0, 0, 0, 0, 0.55)
    mask.add_color_stop_rgba(min(0.98, 80 / w), 0, 0, 0, 0.30)
    mask.add_color_stop_rgba(min(0.98, text_end / w), 0, 0, 0, 0.30)
    mask.add_color_stop_rgba(min(0.99, (text_end + 120) / w), 0, 0, 0, 1.0)
    mask.add_color_stop_rgba(max(0.0, (w - 260) / w), 0, 0, 0, 1.0)
    mask.add_color_stop_rgba(1, 0, 0, 0, 0.35)
    cr.set_source(waves)
    cr.mask(mask)

    # 5. emblem: glowing disc with a download arrow
    cx, cy, rad = 48, h / 2, 25
    pulse = 0.5 + 0.5 * math.sin(t * 1.6)
    halo = cairo.RadialGradient(cx, cy, rad * 0.6, cx, cy, rad * 2.6)
    halo.add_color_stop_rgba(0, ar, ag, ab, 0.45 + 0.2 * pulse * energy)
    halo.add_color_stop_rgba(1, ar, ag, ab, 0)
    cr.set_source(halo)
    cr.arc(cx, cy, rad * 2.6, 0, 2 * math.pi)
    cr.fill()
    disc = cairo.RadialGradient(cx - 8, cy - 10, 2, cx, cy, rad)
    disc.add_color_stop_rgb(0, min(1, ar + 0.35), min(1, ag + 0.35), min(1, ab + 0.35))
    disc.add_color_stop_rgb(1, ar * 0.75, ag * 0.75, ab * 0.75)
    cr.set_source(disc)
    cr.arc(cx, cy, rad, 0, 2 * math.pi)
    cr.fill()
    cr.set_source_rgba(1, 1, 1, 0.35)
    cr.set_line_width(1.2)
    cr.arc(cx, cy, rad - 0.6, 0, 2 * math.pi)
    cr.stroke()
    bob = 2.2 * math.sin(t * 2.2) * (0.3 + energy)
    cr.set_source_rgba(1, 1, 1, 0.97)
    cr.set_line_width(3.2)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.move_to(cx, cy - 12 + bob)
    cr.line_to(cx, cy + 5 + bob)
    cr.stroke()
    cr.move_to(cx - 8, cy - 2 + bob)
    cr.line_to(cx, cy + 6 + bob)
    cr.line_to(cx + 8, cy - 2 + bob)
    cr.stroke()
    cr.move_to(cx - 11, cy + 12)
    cr.line_to(cx + 11, cy + 12)
    cr.stroke()

    # 6. title + tagline + counts
    tx = cx + rad + 20
    text_w = max(w * 0.5 - tx, 120)
    cr.set_source_rgba(fr, fgc, fb, 0.96)
    lay = _layout(cr, "Aria2 GTK", "Sans Bold 17", text_w)
    cr.move_to(tx, h / 2 - 28)
    PangoCairo.show_layout(cr, lay)
    cr.set_source_rgba(fr, fgc, fb, 0.62)
    lay = _layout(cr, "downloads  ·  torrents  ·  seeding", "Sans 9.5", text_w)
    cr.move_to(tx, h / 2 - 2)
    PangoCairo.show_layout(cr, lay)
    counts = f"{state['active']} downloading  ·  {state['seeding']} seeding"
    cr.set_source_rgba(ar * 0.5 + fr * 0.5, ag * 0.5 + fgc * 0.5, ab * 0.5 + fb * 0.5, 0.9)
    lay = _layout(cr, counts, "Sans Bold 8.5", text_w)
    cr.move_to(tx, h / 2 + 15)
    PangoCairo.show_layout(cr, lay)

    # 8a. peak marker dot, under the speed text so the numbers stay readable
    if peak and peak[2] > 0:
        px, py, _value = peak
        pulse = 0.5 + 0.5 * math.sin(t * 2.4)
        halo = cairo.RadialGradient(px, py, 0, px, py, 9)
        halo.add_color_stop_rgba(0, ar, ag, ab, 0.55 + 0.25 * pulse)
        halo.add_color_stop_rgba(1, ar, ag, ab, 0)
        cr.set_source(halo)
        cr.arc(px, py, 9, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.arc(px, py, 2.4, 0, 2 * math.pi)
        cr.fill()

    # 7. live speeds, right aligned (upload in the colour of its graph line)
    boxes = []
    for text, font, y, alpha, col in (
            (f"↓ {fmt_speed(state['down'])}", "Sans Bold 15", h / 2 - 24, 0.97, (fr, fgc, fb)),
            (f"↑ {fmt_speed(state['up'])}", "Sans 10.5", h / 2 + 4, 0.75,
             up_col if state["up"] else (fr, fgc, fb))):
        lay = _layout(cr, text, font)
        tw, th = lay.get_pixel_size()
        cr.set_source_rgba(*col, alpha)
        cr.move_to(w - 24 - tw, y)
        PangoCairo.show_layout(cr, lay)
        boxes.append((w - 24 - tw - 6, y - 2, w, y + th + 2))

    # 8b. peak label, next to the dot where it fits, else at the graph's bottom left
    if peak and peak[2] > 0:
        px, py, value = peak
        lay = _layout(cr, f"peak {fmt_size(value)}/s", "Sans Bold 7.5")
        lw, lh = lay.get_pixel_size()

        def free(x, y):
            return (gx0 - 40 <= x and x + lw <= w - 4 and 2 <= y and y + lh <= h - 2 and
                    not any(x < bx1 and x + lw > bx0 and y < by1 and y + lh > by0
                            for bx0, by0, bx1, by1 in boxes))

        spots = [(px + 7, py - lh - 3), (px - lw - 7, py - lh - 3),
                 (px + 7, py + 3), (px - lw - 7, py + 3), (gx0 + 4, h - lh - 4)]
        spot = next((xy for xy in spots if free(*xy)), None)
        if spot:
            cr.set_source_rgba(fr, fgc, fb, 0.8)
            cr.move_to(*spot)
            PangoCairo.show_layout(cr, lay)


class Banner(Gtk.DrawingArea):
    """Animated header banner: accent waves, data particles, emblem and live speeds."""

    HEIGHT = 92
    PARTICLES = 36

    def __init__(self):
        super().__init__(content_height=self.HEIGHT, hexpand=True)
        self.state = {"down": 0, "up": 0, "active": 0, "seeding": 0,
                      "history": deque([0] * 90, maxlen=90),
                      "up_history": deque([0] * 90, maxlen=90), "visible_particles": 6,
                      "particles": []}
        rnd = random.Random(7)
        for k in range(self.PARTICLES):
            self.state["particles"].append({
                "k": k, "x": rnd.random(), "wave": rnd.randrange(3), "dy": rnd.uniform(-3, 3),
                "size": rnd.uniform(1.2, 2.4), "speed": rnd.uniform(0.04, 0.10),
                "phase": rnd.uniform(0, 6.3), "dir": 1 if k % 4 else -1})
        self.t = 0.0
        self.last = None
        self.set_draw_func(self.on_draw)
        self.add_tick_callback(self.on_tick)
        Adw.StyleManager.get_default().connect("notify::accent-color-rgba",
                                               lambda *_: self.queue_draw())

    def push(self, down, up, active, seeding):
        st = self.state
        st.update(down=int(down), up=int(up), active=active, seeding=seeding)
        st["history"].append(int(down))
        st["up_history"].append(int(up))
        energy = _speed_energy(st["down"], st["up"])
        st["visible_particles"] = int(6 + (self.PARTICLES - 6) * energy)
        self.queue_draw()

    def on_tick(self, _widget, clock):
        now = clock.get_frame_time() / 1e6
        if self.last is None:
            self.last = now
        dt = now - self.last
        if dt < 1 / 30:                       # ~30 fps is plenty for this
            return True
        self.last = now
        if not Gtk.Settings.get_default().get_property("gtk-enable-animations"):
            return True
        dt = min(dt, 0.1)
        energy = _speed_energy(self.state["down"], self.state["up"])
        self.t += dt * (0.5 + 2.2 * energy)
        for p in self.state["particles"]:
            p["x"] = (p["x"] + p["dir"] * p["speed"] * dt * (0.4 + 1.6 * energy)) % 1.0
        self.queue_draw()
        return True

    def on_draw(self, _area, cr, w, h):
        sm = Adw.StyleManager.get_default()
        acc = sm.get_accent_color_rgba()
        fg = self.get_color()
        draw_banner(cr, w, h, self.t, (fg.red, fg.green, fg.blue),
                    (acc.red, acc.green, acc.blue), self.state)


# ----------------------------------------------------------------- web ui ---

APP_DIR = Path(os.path.realpath(__file__)).parent
WEB_DATA = DATA_DIR / "web"          # web UI password hash and sign-ins
WEB_PORT = 6880


def on_main(fn, *args, timeout=10):
    """Run fn on the GTK main thread from a web UI thread; returns its result or raises."""
    done, box = threading.Event(), {}

    def run():
        try:
            box["result"] = fn(*args)
        except Exception as e:  # noqa: BLE001
            box["error"] = e
        done.set()
        return False

    GLib.idle_add(run)
    if not done.wait(timeout):
        raise TimeoutError("The app is busy, try again")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def load_server_module():
    """aria2gtk-server.py (next to this file) holds the web UI's server code; the app reuses
    it with its own aria2c, so the browser page and the headless server stay identical."""
    path = APP_DIR / "aria2gtk-server.py"
    spec = importlib.util.spec_from_file_location("aria2gtk_server", path)
    if not spec or not path.is_file():
        raise FileNotFoundError(f"{path.name} is missing next to {Path(__file__).name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.set_data_dir(WEB_DATA)
    mod.GEO_DB = GEO_DB                  # share the app's country database
    mod.DOWNLOADS = DOWNLOADS
    return mod


class WebUI:
    """Serves the browser page from the app's own aria2c while the app runs."""

    def __init__(self, win):
        self.win = win
        self.mod = None
        self.httpd = None
        self.port = None
        self.error = None

    @property
    def running(self):
        return self.httpd is not None

    def module(self):
        if self.mod is None:
            self.mod = load_server_module()
            WEB_DATA.mkdir(parents=True, exist_ok=True)
            os.chmod(WEB_DATA, 0o700)
        return self.mod

    def has_password(self):
        try:
            return bool(self.module().load_config().get("password"))
        except Exception:  # noqa: BLE001
            return False

    def set_password(self, password):
        mod = self.module()
        cfg = mod.load_config()
        cfg["password"] = mod.hash_password(password)
        mod.save_config(cfg)
        (WEB_DATA / "sessions.json").unlink(missing_ok=True)   # every browser signs in again
        if self.running:
            mod.Handler.cfg = cfg
            mod.Handler.engine.cfg = cfg
            mod.Handler.sessions = {}

    def start(self):
        """Start serving; returns None, or an error message."""
        if self.running:
            return None
        try:
            mod = self.module()
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            return self.error
        win = self.win
        cfg = mod.load_config()

        class AppEngine(mod.Engine):
            """The server's Engine, but on the app's aria2c (never starts its own)."""

            def __init__(self):
                super().__init__(cfg, win.settings.get("dir", DOWNLOADS), 0)

            rpc = property(lambda self: self._rpc(), lambda self, _v: None)
            dir = property(lambda self: str(Path(win.settings.get("dir", DOWNLOADS))
                                            .expanduser().resolve()),
                           lambda self, _v: None)
            version = property(lambda self: win.aria2_version, lambda self, _v: None)

            def _rpc(self):
                live = win.rpc
                if not live:
                    raise RuntimeError("aria2c is not running")
                r = mod.Aria2.__new__(mod.Aria2)
                r.url, r.token = live.url, live.token
                return r

            # The page's Seeding and Create tabs work on the app's own Seeding tab.
            torrent_dir = property(lambda self: Path(win.settings.get(
                "torrent_dir", os.path.expanduser("~/torrents"))).expanduser())

            def seed_entries(self):
                return on_main(lambda: [
                    {"torrent": s["torrent"], "source": s["source"], "want": s["want"],
                     "upload_limit": s.get("upload_limit"), "hash": s["hash"] or "",
                     "name": s["name"],
                     "error": s["row"].status_text if s["row"].error else ""}
                    for s in win.seeding.seeds])

            def _gtk_seed(self, info_hash):
                for s in win.seeding.seeds:
                    if s["hash"] == info_hash:
                        return s
                raise KeyError("That seed is not on the list any more")

            def seed_add(self, torrent, source):
                meta = torrent_meta(torrent)
                if not meta:
                    raise ValueError("That is not a valid .torrent file")
                if any(s["hash"] == meta[0] for s in self.seed_entries()):
                    raise ValueError("This torrent is already on the list")
                on_main(win.seeding.add_seed, str(torrent), str(source))

            def seed_toggle(self, info_hash):
                on_main(lambda: win.seeding.toggle(self._gtk_seed(info_hash)))

            def seed_remove(self, info_hash):
                on_main(lambda: win.seeding.remove(self._gtk_seed(info_hash)))

            def seed_limit(self, info_hash, kib):
                on_main(lambda: win.seeding.set_seed_limit(self._gtk_seed(info_hash), kib))

            def seed_all(self, run):
                on_main(win.seeding.start_all if run else win.seeding.pause_all)

            def resume_seeds(self):
                pass                      # the app does this itself

            def create_defaults(self):
                s = win.settings
                return {"host": s.get("tracker_host", ""),
                        "port": str(s.get("tracker_port", "6969")),
                        "extra": s.get("extra_trackers", ""),
                        "private": bool(s.get("private", True)), "piece": int(s.get("piece", 0))}

            def save_create_defaults(self, d):
                def save():
                    win.settings.update(tracker_host=d["host"], tracker_port=d["port"],
                                        extra_trackers=d["extra"], private=d["private"],
                                        piece=d["piece"])
                    save_settings(win.settings)
                on_main(save)

        port = int(win.settings.get("web_port", WEB_PORT))
        try:
            httpd = mod.Server(("0.0.0.0", port), mod.Handler)
        except OSError as e:
            self.error = f"Port {port}: {e.strerror}"
            return self.error
        mod.Handler.engine = AppEngine()
        mod.Handler.cfg = cfg
        mod.Handler.load_sessions()
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.httpd, self.port, self.error = httpd, port, None
        win.console.event(f"Web UI on {', '.join(self.urls())}")
        return None

    def stop(self):
        if self.httpd:
            httpd, self.httpd = self.httpd, None
            httpd.shutdown()
            httpd.server_close()

    def urls(self):
        port = self.port or int(self.win.settings.get("web_port", WEB_PORT))
        hosts = []
        try:
            hosts = [ip for ip in self.module().lan_addresses() if not ip.startswith("<")]
        except Exception:  # noqa: BLE001
            pass
        hosts.append(socket.gethostname())
        return [f"http://{h}:{port}/" for h in hosts]


# --------------------------------------------------------------- console ---

LOG_LEVELS = [("error", "Errors"), ("warn", "Warnings"), ("notice", "Notices"),
              ("info", "Info"), ("debug", "Debug")]
LOG_LINE_RE = re.compile(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d)\.\d+ \[(\w+)\] \[[^\]]*\] ?(.*)$")
LOG_COLORS = {"ERROR": "#e66161", "WARN": "#e5a50a", "APP": "#62a0ea",
              "INFO": "#9a9996", "DEBUG": "#9a9996"}


class ConsolePanel(Gtk.Box):
    """aria2c's log (tailed from its log file) plus the app's own download events."""

    MAX_LINES = 5000

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["console-panel"])
        self.win = win
        self.lines = []          # (time, level, text)
        self.query = ""
        self.partial = ""
        self.offset = LOG.stat().st_size if LOG.exists() else 0  # this session only

        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=10, margin_end=6)
        bar.append(Gtk.Image(icon_name="utilities-terminal-symbolic"))
        bar.append(Gtk.Label(label="Console", css_classes=["heading"]))
        self.search = Gtk.SearchEntry(placeholder_text="Filter", width_chars=14, hexpand=True,
                                      halign=Gtk.Align.END)
        self.search.connect("search-changed", self.on_filter)
        names = [n for n, _l in LOG_LEVELS]
        self.level = Gtk.DropDown.new_from_strings([label for _n, label in LOG_LEVELS])
        self.level.set_tooltip_text("aria2c log level")
        current = win.settings.get("console_level", "notice")
        self.level.set_selected(names.index(current) if current in names else 2)
        self.level.connect("notify::selected", self.on_level)
        self.follow = Gtk.ToggleButton(icon_name="go-bottom-symbolic", active=True,
                                       tooltip_text="Auto-scroll", css_classes=["flat"])
        self.follow.connect("toggled", lambda b: b.get_active() and self.scroll_end())
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic", tooltip_text="Clear",
                           css_classes=["flat"])
        clear.connect("clicked", lambda _b: self.clear())
        menu = Gio.Menu()
        place = Gio.Menu()
        place.append("Dock at Bottom", "win.console-dock::bottom")
        place.append("Dock at Right", "win.console-dock::right")
        place.append("Separate Window", "win.console-dock::window")
        menu.append_section(None, place)
        menu.append("Hide Console", "win.console")
        dock = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                              tooltip_text="Placement", css_classes=["flat"])
        for w in (self.search, self.level, self.follow, clear, dock):
            bar.append(w)

        self.view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                 wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=10,
                                 right_margin=10, top_margin=6, bottom_margin=6)
        self.buf = self.view.get_buffer()
        for level, color in LOG_COLORS.items():
            self.buf.create_tag(level, foreground=color)
        self.buf.create_tag("time", foreground="#9a9996")
        self.scroller = Gtk.ScrolledWindow(child=self.view, vexpand=True, hexpand=True,
                                           css_classes=["card", "links-card"], margin_start=8,
                                           margin_end=8, margin_bottom=8)
        self.append(bar)
        self.append(self.scroller)
        GLib.timeout_add(500, self.tail)

    # -- input

    def tail(self):
        """Read what aria2c appended to its log since last time."""
        try:
            size = LOG.stat().st_size
        except OSError:
            return True
        if size < self.offset:                       # log was truncated / replaced
            self.offset, self.partial = 0, ""
        if size == self.offset:
            return True
        start = max(self.offset, size - (1 << 20))   # never more than 1 MiB per tick
        try:
            with open(LOG, "rb") as f:
                f.seek(start)
                data = f.read(size - start)
        except OSError:
            return True
        self.offset = size
        text = self.partial + data.decode("utf-8", "replace")
        lines = text.split("\n")
        self.partial = lines.pop()
        last_level = "NOTICE"
        for line in lines:
            if not line.strip():
                continue
            m = LOG_LINE_RE.match(line)
            if m:
                last_level = m.group(2).upper()
                self.add(m.group(1), last_level, m.group(3))
            else:                                    # continuation of a multi-line message
                self.add("", last_level, line.strip())
        return True

    def event(self, text, level="APP"):
        self.add(time.strftime("%H:%M:%S"), level, text)

    def add(self, stamp, level, text):
        self.lines.append((stamp, level, text))
        if len(self.lines) > self.MAX_LINES:
            del self.lines[:1000]
            self.render()
            return
        if self.matches(stamp, level, text):
            self.insert(stamp, level, text)
            if self.follow.get_active():
                self.scroll_end()

    # -- output

    def matches(self, stamp, level, text):
        return not self.query or self.query in f"{stamp} {level} {text}".lower()

    def insert(self, stamp, level, text):
        end = self.buf.get_end_iter()
        if stamp:
            self.buf.insert_with_tags_by_name(end, f"{stamp} ", "time")
        label = f"{level:<6} " if stamp else " " * 7
        tag = level if level in LOG_COLORS else None
        if tag:
            self.buf.insert_with_tags_by_name(self.buf.get_end_iter(), label + text + "\n", tag)
        else:
            self.buf.insert(self.buf.get_end_iter(), label + text + "\n")

    def render(self):
        self.buf.set_text("")
        for line in self.lines:
            if self.matches(*line):
                self.insert(*line)
        self.scroll_end()

    def scroll_end(self):
        mark = self.buf.create_mark(None, self.buf.get_end_iter(), False)
        self.view.scroll_mark_onscreen(mark)
        self.buf.delete_mark(mark)

    def clear(self):
        self.lines.clear()
        self.buf.set_text("")

    def on_filter(self, entry):
        self.query = entry.get_text().strip().lower()
        self.render()

    def on_level(self, dropdown, _pspec):
        name = LOG_LEVELS[dropdown.get_selected()][0]
        self.win.settings["console_level"] = name
        save_settings(self.win.settings)
        rpc = self.win.rpc
        if rpc:
            self.win.run(lambda: rpc.call("aria2.changeGlobalOption", {"log-level": name}))
        self.event(f"Log level set to {name}")
        if name in ("info", "debug"):
            self.event("Info and Debug log every piece exchanged with peers: the log can grow "
                       "by gigabytes. Switch back to Notices when you're done.", "WARN")


class ConsoleWindow(Adw.Window):
    """The console popped out into its own resizable window. Closing it docks it back."""

    def __init__(self, win):
        size = win.settings.get("console_window", {})
        super().__init__(title="Console — Aria2 GTK", transient_for=win,
                         default_width=size.get("width", 820),
                         default_height=size.get("height", 420))
        self.win = win
        self.tv = Adw.ToolbarView()
        self.tv.add_top_bar(Adw.HeaderBar())
        self.set_content(self.tv)
        self.connect("close-request", self.on_close)

    def on_close(self, *_):
        width, height = self.get_default_size()
        self.win.settings["console_window"] = {"width": width, "height": height}
        if not self.win.closing_console:    # closed by the user: dock the panel back
            self.tv.set_content(None)
            self.win.console_window = None
            self.win.set_console_dock(self.win.settings.get("console_last_dock", "bottom"))
        return False                         # GTK closes this window itself


# ----------------------------------------------------------------- window ---

class Window(Adw.ApplicationWindow):
    PAGES = [
        ("active", "Downloading", "folder-download-symbolic", "Nothing Downloading",
         "Press + or drop a link here to start a download"),
        ("queued", "Queued", "document-open-recent-symbolic", "Queue Is Empty",
         "Paused and waiting downloads show up here"),
        ("done", "Finished", "emblem-ok-symbolic", "Nothing Finished Yet",
         "Completed and failed downloads show up here"),
    ]

    def __init__(self, app):
        settings = load_settings()
        imported = import_share_torrent(settings)
        if imported:
            save_settings(settings)
        apply_color_scheme(settings.get("color_scheme", "system"))
        super().__init__(application=app, title="Downloads",
                         default_width=settings.get("width", 760),
                         default_height=settings.get("height", 580),
                         maximized=settings.get("maximized", False))
        self.app = app
        self.rpc = None
        self.rows = {}
        self.busy = False
        self.first_poll = True
        self.polling = False
        self.restarting = False
        self.pending_open = None
        self.tidied = set()            # finished magnet-metadata entries already removed
        self.metadata_checked = set()  # gids already checked for pause-metadata
        self.last_status = {}      # gid -> last seen aria2 status, for console events
        self.pending_select = {}   # gid -> {"kind": "torrent"|"follow", "start": bool, "misses": n}
        self.select_dialog = None
        self.started_once = False
        self.aria2_version = ""
        self.web = WebUI(self)
        self.quitting = False
        self.held = False
        self.settings = settings
        self.connect("close-request", self.on_close_request)
        self.tray = Tray(self.toggle_visible, lambda: app.activate_action("quit", None))

        self.toasts = Adw.ToastOverlay()
        self.view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.add_btn = Gtk.Button(icon_name="list-add-symbolic",
                                  tooltip_text="Add Download (Ctrl+N)", sensitive=False)
        self.add_btn.connect("clicked", lambda *_: self.show_add())
        header.pack_start(self.add_btn)
        console_btn = Gtk.Button(icon_name="utilities-terminal-symbolic",
                                 tooltip_text="Console (F12)", action_name="win.console")
        header.pack_end(console_btn)

        menu = Gio.Menu()
        s1 = Gio.Menu()
        s1.append("Resume All", "win.resume-all")
        s1.append("Pause All", "win.pause-all")
        s1.append("Clear Finished", "win.purge")
        tools = Gio.Menu()
        tools.append("Torrent Info & Magnet…", "win.torrent-info")
        tools.append("Tracker Status…", "win.tracker")
        s2 = Gio.Menu()
        s2.append("Open Download Folder", "win.open-dir")
        s2.append("Preferences", "win.preferences")
        s2.append("About Aria2 GTK", "app.about")
        menu.append_section(None, s1)
        menu.append_section(None, tools)
        menu.append_section(None, s2)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Main Menu"))

        self.stack = Adw.ViewStack()
        self.header = header
        self.switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.switcher)
        self.lists = {}
        for name, title, icon, empty, desc in self.PAGES:
            listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE,
                                  valign=Gtk.Align.START,
                                  css_classes=["boxed-list", "glow-list"])
            clamp = Adw.Clamp(child=listbox, maximum_size=900, margin_top=12,
                              margin_bottom=12, margin_start=12, margin_end=12)
            scroller = Gtk.ScrolledWindow(child=clamp, vexpand=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER)
            inner = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            inner.add_named(Adw.StatusPage(title=empty, description=desc, icon_name=icon),
                            "empty")
            inner.add_named(scroller, "list")
            page = self.stack.add_titled_with_icon(inner, name, title, icon)
            self.lists[name] = (listbox, inner, page)

        self.seeding = SeedingPage(self)
        self.seeding_page = self.stack.add_titled_with_icon(
            self.seeding, "seeding", "Seeding", "emblem-shared-symbolic")
        self.create = CreatePage(self)
        self.stack.add_titled_with_icon(self.create, "create", "Create", "document-new-symbolic")

        self.stats = Gtk.Label(css_classes=["dim-label", "caption", "numeric"],
                               margin_top=6, margin_bottom=6, label="Starting aria2c…")
        self.console = ConsolePanel(self)
        self.console_window = None
        self.closing_console = False
        self.split_pending = False
        self.body = Gtk.Paned(start_child=self.stack, resize_start_child=True,
                              shrink_start_child=False, resize_end_child=False,
                              shrink_end_child=False, wide_handle=True)
        self.body.connect("notify::max-position", self.on_split_size)
        self.body.connect("notify::position", self.on_split_moved)

        self.view.add_top_bar(header)
        # banner + tabs row under the header (window buttons stay at the very top)
        self.banner = Banner()
        self.tabs_bar = Gtk.CenterBox(margin_top=4, margin_bottom=4)
        self.view.add_top_bar(self.banner)
        self.view.add_top_bar(self.tabs_bar)
        self.apply_banner(self.settings.get("banner", True))
        self.heartbeat = Gtk.Label(label="●", css_classes=["heartbeat", "caption"],
                                   tooltip_text="Flashes each time aria2c answers")
        self.busy_spin = Adw.Spinner(width_request=14, height_request=14, visible=False)
        status_bar = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        for w in (self.heartbeat, self.busy_spin, self.stats):
            status_bar.append(w)
        self.poll_started = 0.0
        GLib.timeout_add(500, self.watch_busy)
        GLib.timeout_add_seconds(60, self.housekeeping)
        GLib.timeout_add_seconds(8, self.check_big_logs)
        GLib.timeout_add_seconds(30, self.geo_auto_update)
        self.view.add_bottom_bar(status_bar)
        self.view.set_content(self.body)
        self.toasts.set_child(self.view)
        self.toasts.add_css_class("translucent-content")  # dialogs sit outside this
        self.set_content(self.toasts)

        self.opacity_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.opacity_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *_: self.apply_opacity(self.settings.get("opacity", 1.0)))
        self.apply_opacity(self.settings.get("opacity", 1.0))

        for name, cb in [
            ("add", lambda *_: self.show_add()),
            ("preferences", lambda *_: self.show_preferences()),
            ("restart-aria2", self.restart_aria2),
            ("resume-all", lambda *_: self.run(lambda: self.rpc.call("aria2.unpauseAll"))),
            ("pause-all", lambda *_: self.run(lambda: self.rpc.call("aria2.pauseAll"))),
            ("purge", lambda *_: self.run(lambda: self.rpc.call("aria2.purgeDownloadResult"))),
            ("open-dir", self.on_open_dir),
            ("console", lambda *_: self.toggle_console()),
            ("torrent-info", lambda *_: TorrentInfoDialog(self).present(self)),
            ("tracker", lambda *_: TrackerDialog(self).present(self)),
        ]:
            act = Gio.SimpleAction(name=name)
            act.connect("activate", cb)
            self.add_action(act)

        dock = self.settings.get("console_dock", "bottom")
        dock_action = Gio.SimpleAction.new_stateful(
            "console-dock", GLib.VariantType.new("s"), GLib.Variant("s", dock))
        dock_action.connect("activate", lambda _a, v: self.set_console_dock(v.get_string()))
        self.add_action(dock_action)
        self.dock_action = dock_action
        self.place_console()

        drop = Gtk.DropTarget.new(GObject.TYPE_NONE, Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, GObject.TYPE_STRING])
        drop.connect("drop", self.on_drop)
        self.add_controller(drop)
        if imported:
            GLib.timeout_add(800, lambda: self.toast(
                "Imported your Share Torrent settings") and False)

    # -- daemon / polling ---------------------------------------------------

    def start_daemon(self, restart=False):
        ddir = self.settings.get("dir", DOWNLOADS)
        options = dict(self.settings.get("aria2", {}))

        def work():
            try:
                if restart:
                    self.app.daemon.stop()
                ver = self.app.daemon.start(ddir, options,
                                            self.settings.get("console_level", "notice"),
                                            set(self.settings.pop("drop_gids", [])))
                GLib.idle_add(self.on_started, ver)
            except FileNotFoundError:
                GLib.idle_add(self.fatal, "aria2c Not Found",
                              "Install the <tt>aria2</tt> package and restart.")
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.fatal, "Could Not Start aria2c",
                              GLib.markup_escape_text(str(e)), bool(options))

        threading.Thread(target=work, daemon=True).start()

    def restart_aria2(self, *_):
        if self.restarting:
            return
        self.restarting = True
        self.rpc = None
        for seed in self.seeding.seeds:      # their gids belong to the old engine
            seed["gid"], seed["state"], seed["starting"] = None, "", False
        self.add_btn.set_sensitive(False)
        self.stats.set_label("Restarting aria2c…")
        self.start_daemon(restart=True)

    def on_started(self, ver):
        self.rpc = self.app.daemon.rpc
        self.restarting = False
        self.busy = False
        self.body.set_start_child(self.stack)
        self.stats.set_visible(True)
        self.add_btn.set_sensitive(True)
        self.stats.set_label(f"aria2 {ver.get('version', '')}")
        self.aria2_version = ver.get("version", "")
        if self.settings.get("web_ui") and not self.web.running:
            err = self.web.start()
            if err:
                self.toast(f"Web UI not started: {err}")
        self.console.event(f"aria2c {ver.get('version', '')} running "
                           f"(log level {self.settings.get('console_level', 'notice')})")
        self.poll()
        if self.pending_open:
            links, paths = self.pending_open
            self.pending_open = None
            self.open_items(links, paths)
        if self.started_once or self.settings.get("seed_autostart"):
            self.seeding.resume_wanted()   # after a restart, or "start seeds when app opens"
        self.started_once = True
        if not self.polling:
            self.polling = True
            GLib.timeout_add(1000, self.poll)
        return False

    def fatal(self, title, desc, can_reset=False):
        self.console.event(f"{title}: {re.sub(r'<[^>]+>', '', desc)}", "ERROR")
        self.restarting = False
        self.add_btn.set_sensitive(False)
        self.stats.set_visible(False)
        page = Adw.StatusPage(title=title, icon_name="dialog-error-symbolic")
        page.set_description(desc)
        if can_reset:
            btn = Gtk.Button(label="Forget aria2 Options and Retry", halign=Gtk.Align.CENTER,
                             css_classes=["pill", "suggested-action"])

            def reset(_b):
                self.settings.pop("aria2", None)
                save_settings(self.settings)
                self.restart_aria2()

            btn.connect("clicked", reset)
            page.set_child(btn)
        self.body.set_start_child(page)   # the console stays visible: it shows why
        return False

    def open_items(self, links, paths):
        """Show the Add dialog for links/files handed over by the system."""
        if not self.rpc:
            old_links, old_paths = self.pending_open or ([], [])
            self.pending_open = (old_links + links, old_paths + paths)
            return
        AddDialog(self, "\n".join(links), paths).present(self)

    def current_options(self):
        """aria2's live global options, with saved restart-only values on top."""
        opts = {}
        if self.rpc:
            try:
                opts = self.rpc.call("aria2.getGlobalOption", timeout=2)
            except Exception:  # noqa: BLE001
                opts = {}
        saved = self.settings.get("aria2", {})
        if not opts:
            opts = dict(saved)
        else:
            opts.update({k: v for k, v in saved.items() if k in RESTART_KEYS})
        return opts

    def poll(self):
        if self.busy or not self.rpc:
            return True
        self.busy = True
        self.poll_started = time.monotonic()
        rpc = self.rpc

        def work():
            try:
                res = rpc.multicall([
                    ("aria2.tellActive", KEYS),
                    ("aria2.tellWaiting", 0, 1000, KEYS),
                    ("aria2.tellStopped", 0, 1000, KEYS),
                    ("aria2.getGlobalStat",),
                ])
            except Exception as e:  # noqa: BLE001
                res = e
            GLib.idle_add(self.apply, res)

        threading.Thread(target=work, daemon=True).start()
        return True

    def apply(self, res):
        self.busy = False
        if self.restarting:
            return False
        if isinstance(res, Exception):
            if self.app.daemon.proc and self.app.daemon.proc.poll() is not None:
                self.rpc = None
                self.fatal("aria2c Stopped", f"It exited unexpectedly. See <tt>{LOG}</tt>.")
            else:
                self.stats.set_label(f"aria2c not responding: {res}")
            return False
        active, waiting, stopped, stat = [
            r if not isinstance(r, Exception) else ({} if i == 3 else [])
            for i, r in enumerate(res)
        ]
        self.beat()
        seen = set()
        events = []
        seed_hashes = self.seeding.hashes()
        seed_items = {}
        for d in [*active, *waiting, *stopped]:
            if is_metadata_done(d):              # internal magnet step: never list it
                if d["gid"] not in self.tidied:
                    self.tidied.add(d["gid"])
                    self.run(lambda g=d["gid"], r=self.rpc:
                             r.call("aria2.removeDownloadResult", g))
                continue
            if d.get("infoHash") in seed_hashes:
                seed_items.setdefault(d["infoHash"], d)   # newest first: active, waiting, stopped
                continue
            gid = d["gid"]
            seen.add(gid)
            if failed_for_good(d):           # don't bring it back at the next start
                drop = self.settings.setdefault("drop_gids", [])
                if gid not in drop:
                    drop.append(gid)
                    del drop[:-200]
                    save_settings(self.settings)
            row = self.rows.get(gid)
            if row is None:
                row = self.rows[gid] = DownloadRow(self, gid)
            page = PAGE_OF.get(d["status"], "done")
            if row.page != page:
                if row.page:
                    self.lists[row.page][0].remove(row)
                    if d["status"] == "complete":
                        self.notify_done(name_of(d))
                self.lists[page][0].append(row)
                row.page = page
            row.update(d)
            prev = self.last_status.get(gid)
            if prev != d["status"]:
                self.last_status[gid] = d["status"]
                if not self.first_poll:
                    events.append(self.describe_change(prev, d))
        for gid in [g for g in self.last_status if g not in seen]:
            del self.last_status[gid]
        for ev in events:
            if ev:
                self.console.event(*ev)
        if self.first_poll and self.rows:
            self.console.event(f"Session: {len(self.rows)} download"
                               f"{'s' if len(self.rows) != 1 else ''} loaded")
        for gid in [g for g in self.rows if g not in seen]:
            row = self.rows.pop(gid)
            self.lists[row.page][0].remove(row)
        self.find_metadata_paused(waiting)
        if self.pending_select:
            self.check_file_choices({d["gid"]: d for d in [*active, *waiting, *stopped]})

        for name, (_lb, inner, page) in self.lists.items():
            count = sum(1 for r in self.rows.values() if r.page == name)
            inner.set_visible_child_name("list" if count else "empty")
            page.set_badge_number(count if name != "done" else 0)
        seeding, _seed_up = self.seeding.update(seed_items)
        self.seeding_page.set_badge_number(seeding)

        if stat:
            down = fmt_speed(stat.get("downloadSpeed", 0))
            up = fmt_speed(stat.get("uploadSpeed", 0))
            # count by what a torrent does, not by tab: a finished download that keeps
            # seeding on the Downloading tab counts as seeding
            running = [r.data for r in self.rows.values() if r.page == "active" and r.data]
            seeding_here = sum(1 for d in running if is_seeding(d))
            downloading = len(running) - seeding_here
            seeding_all = seeding + seeding_here
            self.tray.set_status(f"↓ {down}   ↑ {up}",
                                 f"{downloading} downloading, {seeding_all} seeding\n"
                                 f"↓ {down}   ↑ {up}")
            self.banner.push(stat.get("downloadSpeed", 0), stat.get("uploadSpeed", 0),
                             downloading, seeding_all)
        if stat:
            bits = [f"↓ {fmt_speed(stat.get('downloadSpeed', 0))}",
                    f"↑ {fmt_speed(stat.get('uploadSpeed', 0))}",
                    f"{stat.get('numActive', 0)} active"]
            if int(stat.get("numWaiting", 0)):
                bits.append(f"{stat['numWaiting']} queued")
            self.stats.set_label("     ".join(bits))
        if self.first_poll:
            self.first_poll = False
            if active or waiting:
                self.stack.set_visible_child_name("active" if active else "queued")
        return False

    # -- banner -------------------------------------------------------------

    def apply_banner(self, show):
        """Banner on: tabs move to their own row under it. Off: tabs back in the header."""
        if show:
            if self.header.get_title_widget() is self.switcher:
                self.header.set_title_widget(Gtk.Box())   # the banner carries the name
                self.tabs_bar.set_center_widget(self.switcher)
        elif self.tabs_bar.get_center_widget() is self.switcher:
            self.tabs_bar.set_center_widget(None)
            self.header.set_title_widget(self.switcher)
        self.banner.set_visible(show)
        self.tabs_bar.set_visible(show)

    # -- liveness -----------------------------------------------------------

    def beat(self):
        self.heartbeat.add_css_class("beat")
        GLib.timeout_add(120, lambda: self.heartbeat.remove_css_class("beat") and False)
        self.busy_spin.set_visible(False)

    def show_preferences(self, page=None):
        dialog = PreferencesDialog(self)
        if page:
            dialog.set_visible_page_name(page)
        dialog.present(self)

    # -- housekeeping: logs and the country database ----------------------------

    def housekeeping(self):
        """Every minute: keep aria2.log small while aria2c runs."""
        if self.settings.get("log_autotrim", True):
            trim_log()
        return True

    def check_big_logs(self):
        big = [(p, s) for p, _k, s in log_files() if s > LOG_LIMIT]
        if big:
            toast = Adw.Toast(title=f"Log files use {fmt_size(sum(s for _p, s in big))}",
                              button_label="Review", timeout=10)
            toast.connect("button-clicked", lambda *_: self.show_preferences("storage"))
            self.toasts.add_toast(toast)
        return False

    def geo_auto_update(self):
        s = self.settings
        if (not s.get("geo_auto", True) or geo_path() != GEO_DB
                or time.time() - s.get("geo_checked", 0) < 86400):
            return False

        def work():
            try:
                latest = geo_latest()
                edition = geo_edition()
                if edition and latest <= edition:
                    GLib.idle_add(done, None)
                    return
                download_geo_db()
                GLib.idle_add(done, f"Country database updated to {geo_edition() or latest}")
            except Exception:  # noqa: BLE001  offline etc.: try again tomorrow
                GLib.idle_add(done, None)

        def done(msg):
            s["geo_checked"] = time.time()
            save_settings(s)
            if msg:
                self.toast(msg)
            return False

        threading.Thread(target=work, daemon=True).start()
        return False

    def watch_busy(self):
        """While aria2c takes long to answer, say so (downloads keep going meanwhile)."""
        if self.busy and self.rpc and not self.restarting:
            waited = time.monotonic() - self.poll_started
            if waited > 2:
                self.busy_spin.set_visible(True)
                self.stats.set_label(f"aria2c is busy ({int(waited)} s)… downloads keep going")
        return True

    # -- choosing torrent files ---------------------------------------------

    def check_file_choices(self, by_gid):
        rpc = self.rpc
        for gid, info in list(self.pending_select.items()):
            d = by_gid.get(gid)
            if d is None:
                info["misses"] += 1
                if info["misses"] > 30:          # gone (removed elsewhere)
                    del self.pending_select[gid]
                continue
            st = d["status"]
            if info["kind"] == "follow":
                # metadata (magnet / .torrent link) done -> the real download, created paused
                if d.get("followedBy"):
                    del self.pending_select[gid]
                    for new in d["followedBy"]:
                        self.pending_select[new] = {"kind": "torrent", "start": info["start"],
                                                    "misses": 0}
                    if st == "complete":         # tidy away the metadata entry
                        self.run(lambda g=gid: rpc.call("aria2.removeDownloadResult", g))
                elif st in ("error", "removed") or (st == "complete" and not d.get("bittorrent")):
                    del self.pending_select[gid]
            elif st == "paused" and d.get("files"):
                info["ready"] = len(d["files"])
            elif st in ("error", "removed", "complete"):
                del self.pending_select[gid]
        self.next_file_choice()

    def next_file_choice(self):
        """Open the Choose Files dialog for the next ready download (one at a time)."""
        self._next_file_choice()

    def find_metadata_paused(self, waiting):
        """Downloads that aria2 created paused because of pause-metadata (e.g. a magnet
        restored from the session after a restart) still wait for their file choice."""
        rpc = self.rpc
        # paused torrents still carrying pause-metadata=true wait for a file choice: either
        # re-created from a magnet ("following") or loaded from saved metadata on restart
        fresh = [d for d in waiting if d["status"] == "paused"
                 and ((d.get("bittorrent") or {}).get("info") or d.get("following"))
                 and d.get("files") and d["gid"] not in self.metadata_checked
                 and d["gid"] not in self.pending_select]
        if not rpc or not fresh:
            return
        for d in fresh:
            self.metadata_checked.add(d["gid"])

        def work():
            found = []
            for d in fresh:
                try:
                    if rpc.call("aria2.getOption", d["gid"]).get("pause-metadata") == "true":
                        found.append((d["gid"], len(d["files"])))
                except Exception:  # noqa: BLE001
                    pass
            GLib.idle_add(self._metadata_paused_found, found)

        threading.Thread(target=work, daemon=True).start()

    def _metadata_paused_found(self, found):
        for gid, count in found:
            self.pending_select[gid] = {"kind": "torrent", "start": True, "misses": 0,
                                        "ready": count}
            self.console.event(f"Waiting for your file choice: “{name_of(self.rows[gid].data)}”"
                               if gid in self.rows else "A torrent waits for your file choice")
        self.next_file_choice()
        return False

    def _next_file_choice(self):
        if self.select_dialog or not self.rpc:
            return
        for gid, info in list(self.pending_select.items()):
            if not info.get("ready"):
                continue
            del self.pending_select[gid]
            if info["ready"] == 1:               # single file: nothing to choose
                rpc, start = self.rpc, info["start"]

                def go(g=gid):
                    rpc.call("aria2.changeOption", g, {"pause-metadata": "false"})
                    if start:
                        rpc.call("aria2.unpause", g)

                self.run(go)
                continue
            if not self.get_visible():
                self.show_window()
            FileSelectDialog(self, gid, info["start"], initial=True).present()
            return

    # -- actions ------------------------------------------------------------

    def toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(msg), timeout=4))
        return False

    def notify_done(self, name):
        self.toast(f"Finished: {name}")
        if not self.is_active():
            n = Gio.Notification.new("Download finished")
            n.set_body(name)
            self.app.send_notification(None, n)

    def run(self, fn, done=None):
        """Run an RPC callable on a worker thread; errors become toasts."""
        if not self.rpc:
            return

        def work():
            try:
                result = fn()
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self.toast, str(e))
                return
            if done:
                GLib.idle_add(lambda: done(result) and False)
            GLib.idle_add(lambda: self.poll() and False)

        threading.Thread(target=work, daemon=True).start()

    def add_calls(self, calls, select=None):
        """select: optional list parallel to calls of None or (kind, start) to choose files."""
        if not calls:
            return

        def done(results):
            for i, r in enumerate(results):
                if select and i < len(select) and select[i] and isinstance(r, str):
                    kind, start = select[i]
                    self.pending_select[r] = {"kind": kind, "start": start, "misses": 0}
            errors = [str(r) for r in results if isinstance(r, Exception)]
            ok = len(results) - len(errors)
            if errors:
                self.toast(f"Could not add: {errors[0]}")
                for err in errors:
                    self.console.event(f"Could not add: {err}", "ERROR")
            if ok:
                self.toast(f"Added {ok} download{'s' if ok > 1 else ''}")
                self.stack.set_visible_child_name("active")

        self.run(lambda: self.rpc.multicall(calls), done)

    def show_add(self, text=None):
        if not self.rpc:
            return
        if text is not None:
            AddDialog(self, text).present(self)
            return

        def got(clip, res):
            try:
                t = (clip.read_text_finish(res) or "").strip()
            except GLib.Error:
                t = ""
            looks = t and all(
                l.strip().startswith(("http://", "https://", "ftp://", "sftp://", "magnet:"))
                for l in t.splitlines() if l.strip())
            AddDialog(self, t if looks else "").present(self)

        self.get_clipboard().read_text_async(None, got)

    def on_drop(self, _target, value, _x, _y):
        if not self.rpc:
            return False
        links, calls, select = [], [], []
        if isinstance(value, Gdk.FileList):
            choose = self.settings.get("choose_files", True)
            for f in value.get_files():
                path = f.get_path()
                if path and method_for_file(path):
                    torrent = method_for_file(path) == "aria2.addTorrent"
                    opts = {"dir": self.settings.get("dir", DOWNLOADS)}
                    if choose and torrent:
                        opts["pause"] = "true"
                    try:
                        calls.append(file_call(path, opts))
                        select.append(("torrent", True) if choose and torrent else None)
                    except OSError as e:
                        self.toast(f"Cannot read file: {e.strerror}")
                elif not f.is_native():
                    links.append(f.get_uri())
        elif isinstance(value, str):
            links = [l.strip() for l in value.splitlines() if l.strip()]
        self.add_calls(calls, select)
        if links:
            self.show_add("\n".join(links))
        return bool(links or calls)

    def confirm_remove(self, row):
        d = row.data
        finished = d.get("status") == "complete"
        dialog = Adw.AlertDialog(
            heading="Remove Download?",
            body=f"“{name_of(d)}” will be removed from the list."
                 + ("" if finished else " Partial data stays on disk unless you delete it."))
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete Files")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("remove")
        dialog.set_close_response("cancel")

        def on_response(_dlg, response):
            if response in ("remove", "delete"):
                self.remove_download(d, delete_files=response == "delete")

        dialog.connect("response", on_response)
        dialog.present(self)

    def remove_download(self, d, delete_files):
        rpc, gid = self.rpc, d["gid"]
        paths = [f["path"] for f in d.get("files") or [] if f.get("path")
                 and not os.path.basename(f["path"]).startswith("[METADATA]")]
        info = (d.get("bittorrent") or {}).get("info") or {}

        def work():
            if d["status"] in ("active", "waiting", "paused"):
                rpc.call("aria2.forceRemove", gid)
                for _ in range(50):
                    if rpc.call("aria2.tellStatus", gid, ["status"])["status"] == "removed":
                        break
                    time.sleep(0.1)
            rpc.call("aria2.removeDownloadResult", gid)
            if delete_files:
                for p in paths:
                    for victim in (p, p + ".aria2"):
                        try:
                            os.remove(victim)
                        except OSError:
                            pass
                if info.get("name") and len(paths) > 1:
                    root = os.path.join(d["dir"], info["name"])
                    for dirpath, _dirs, _files in sorted(os.walk(root), reverse=True):
                        try:
                            os.rmdir(dirpath)  # only removes empty dirs
                        except OSError:
                            pass

        self.run(work)

    def apply_opacity(self, alpha):
        if alpha >= 0.995:
            self.remove_css_class("translucent")
            self.opacity_css.load_from_string("")
            return
        dark = Adw.StyleManager.get_default().get_dark()
        self.opacity_css.load_from_string(translucent_css(alpha, dark))
        self.add_css_class("translucent")

    # -- continue seeding a finished torrent ---------------------------------

    def continue_seeding(self, d):
        """Put a finished torrent back into aria2c as a seed (until you pause it)."""
        rpc, gid = self.rpc, d["gid"]
        info_hash = d.get("infoHash") or ""
        name = name_of(d)
        src = top_path(d).rstrip("/")
        if not rpc or not info_hash:
            return
        if not os.path.exists(src):
            self.toast(f"The files of “{name}” are not there any more")
            return
        # the app's own copy (every added .torrent), else the metadata a magnet saved
        keep = torrent_cache(info_hash)
        saved = Path(d.get("dir") or "") / f"{info_hash}.torrent"
        if not keep.is_file() and saved.is_file():
            try:
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(saved, keep)
            except OSError:
                pass
        meta = torrent_meta(keep) if keep.is_file() else None
        if meta and meta[0] == info_hash:
            try:
                rpc.call("aria2.removeDownloadResult", gid, timeout=3)
            except Exception:  # noqa: BLE001
                pass
            if self.seeding.add_seed(str(keep), src):
                self.console.event(f"Continue seeding “{name}” (Seeding tab)")
                self.toast(f"Seeding “{name}” again. It's on the Seeding tab")
            return
        # no metadata kept (older magnet): re-add by info hash; peers send the metadata,
        # aria2c checks the existing files and seeds
        trackers = [t for tier in (d.get("bittorrent") or {}).get("announceList") or []
                    for t in tier]
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}" + "".join(
            f"&tr={quote(t, safe='')}" for t in trackers)
        opts = {"dir": d.get("dir") or DOWNLOADS, "check-integrity": "true",
                "seed-ratio": "0.0", "seed-time": SEED_FOREVER, "bt-save-metadata": "true"}

        def work():
            try:
                rpc.call("aria2.removeDownloadResult", gid)
            except Exception:  # noqa: BLE001
                pass
            rpc.call("aria2.addUri", [magnet], opts)

        self.run(work)
        self.console.event(f"Continue seeding “{name}”: fetching metadata from peers, "
                           "then checking the files")
        self.toast(f"Getting “{name}” from peers, then it seeds")

    # -- console ------------------------------------------------------------

    def describe_change(self, prev, d):
        name, st = f"“{name_of(d)}”", d["status"]
        if prev is None:
            return (f"Added {name}" + (" (paused)" if st == "paused" else ""),)
        if st == "active":
            return (f"{'Resumed' if prev == 'paused' else 'Started'} {name}",)
        if st == "paused":
            return (f"Paused {name}",)
        if st == "waiting":
            return (f"Queued {name}",)
        if st == "complete":
            return (f"Completed {name}  ({fmt_size(d.get('totalLength', 0))})",)
        if st == "error":
            return (f"Failed {name}: {d.get('errorMessage') or 'error ' + str(d.get('errorCode'))}",
                    "ERROR")
        if st == "removed":
            return (f"Removed {name}",)
        return None

    def toggle_console(self):
        self.settings["console_visible"] = not self.settings.get("console_visible", False)
        self.place_console()

    def set_console_dock(self, where):
        self.settings["console_visible"] = True
        if where in ("bottom", "right"):
            self.settings["console_last_dock"] = where
        self.settings["console_dock"] = where
        self.dock_action.set_state(GLib.Variant("s", where))
        self.place_console()

    def place_console(self):
        s = self.settings
        visible = s.get("console_visible", False)
        dock = s.get("console_dock", "bottom")
        if self.body.get_end_child() is self.console:          # take it out first
            self.body.set_end_child(None)
        if self.console_window and self.console_window.tv.get_content() is self.console:
            self.console_window.tv.set_content(None)
        if not visible or dock != "window":
            self.close_console_window()
        if not visible:
            return
        if dock == "window":
            if not self.console_window:
                self.console_window = ConsoleWindow(self)
            self.console_window.tv.set_content(self.console)
            self.console_window.present()
            return
        self.body.set_orientation(Gtk.Orientation.VERTICAL if dock == "bottom"
                                  else Gtk.Orientation.HORIZONTAL)
        self.body.set_end_child(self.console)
        self.split_pending = True
        self.on_split_size()

    def close_console_window(self):
        if self.console_window:
            width, height = self.console_window.get_default_size()
            self.settings["console_window"] = {"width": width, "height": height}
            self.closing_console = True
            self.console_window.destroy()
            self.closing_console = False
            self.console_window = None

    def on_split_size(self, *_):
        """Restore the saved split once the paned knows its size."""
        if not self.split_pending:
            return
        top = self.body.get_property("max-position")
        if top < 100:
            return
        dock = self.settings.get("console_dock", "bottom")
        ratio = self.settings.get("console_split", {}).get(dock, 0.68 if dock == "bottom"
                                                           else 0.62)
        self.split_pending = False
        self.body.set_position(int(top * ratio))

    def on_split_moved(self, *_):
        if self.split_pending or self.body.get_end_child() is not self.console:
            return
        top = self.body.get_property("max-position")
        if top >= 100:
            dock = self.settings.get("console_dock", "bottom")
            ratio = min(max(self.body.get_position() / top, 0.15), 0.95)
            self.settings.setdefault("console_split", {})[dock] = round(ratio, 3)

    def on_close_request(self, *_):
        # default-size tracks the unmaximized size, so un-maximizing later restores it
        width, height = self.get_default_size()
        self.settings.update(width=width, height=height, maximized=self.is_maximized())
        try:
            save_settings(self.settings)
        except OSError:
            pass
        if not self.quitting and self.settings.get("background", True) and self.tray.available:
            self.set_visible(False)        # keep downloading and seeding in the tray
            if self.console_window:
                self.console_window.set_visible(False)
            self.hold(True)
            return True
        self.close_console_window()
        self.tray.close()
        self.hold(False)
        return False  # let the window close

    def hold(self, flag):
        if flag and not self.held:
            self.app.hold()
            self.held = True
        elif not flag and self.held:
            self.app.release()
            self.held = False

    def toggle_visible(self):
        if self.get_visible():
            self.set_visible(False)
            if self.console_window:
                self.console_window.set_visible(False)
            self.hold(True)
        else:
            self.show_window()

    def show_window(self):
        self.set_visible(True)
        self.present()
        if self.console_window:
            self.console_window.set_visible(True)
        self.hold(False)

    def start_hidden(self):
        """Started with --hidden: stay in the tray; show the window if there is no tray."""
        self.hold(True)

        def check():
            if not self.tray.available:
                self.show_window()
            return False

        GLib.timeout_add_seconds(5, check)

    def on_open_dir(self, *_):
        folder = self.settings.get("dir", DOWNLOADS)
        Gtk.FileLauncher(file=Gio.File.new_for_path(folder)).launch(self, None, None)


# -------------------------------------------------------------------- app ---

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.daemon = Daemon()
        self.win = None
        self.start_hidden = False
        self.add_main_option("hidden", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start hidden in the system tray", None)

    def do_handle_local_options(self, options):
        self.start_hidden = options.contains("hidden")
        return -1  # continue normal startup

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb in [("about", self.on_about), ("quit", self.on_quit)]:
            act = Gio.SimpleAction(name=name)
            act.connect("activate", cb)
            self.add_action(act)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.add", ["<Control>n"])
        self.set_accels_for_action("win.preferences", ["<Control>comma"])
        self.set_accels_for_action("win.console", ["F12"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        if not self.win:
            self.win = Window(self)
            self.win.start_daemon()
            if self.start_hidden:
                self.start_hidden = False
                self.win.start_hidden()
                return
        self.win.show_window()

    def do_open(self, files, _n_files, _hint):
        """Called for `aria2gtk file.torrent` / `aria2gtk magnet:?…`, also from a 2nd launch."""
        self.do_activate()
        links, paths = [], []
        for f in files:
            path = f.get_path() if f.is_native() else None
            if path and method_for_file(path):
                paths.append(path)
            elif not f.is_native():
                uri = fix_magnet(f.get_uri())
                if uri.lower().startswith(LINK_SCHEMES):
                    links.append(uri)
        if links or paths:
            self.win.open_items(links, paths)
        else:
            self.win.toast("Nothing to download in what was opened")

    def do_shutdown(self):
        if self.win:
            self.win.tray.close()
            self.win.web.stop()
        self.daemon.stop()
        Adw.Application.do_shutdown(self)

    def on_quit(self, *_):
        # close the window so its close-request handler saves the size
        if self.win:
            self.win.quitting = True
            self.win.close()
        else:
            self.quit()

    def on_about(self, *_):
        Adw.AboutDialog(
            application_name="Aria2 GTK",
            application_icon="folder-download-symbolic",
            version=VERSION,
            comments="Downloads, torrent creation and seeding with aria2c",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/iceM0nger/aria2-gtkGUI",
        ).present(self.win)


if __name__ == "__main__":
    raise SystemExit(App().run(sys.argv))
