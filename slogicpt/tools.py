"""External binaries the test needs (sigrok-cli, openFPGALoader, wdi-simple):
which ones this platform needs, whether they are in place, and fetching them.

resources/tools.toml lists each one with a pinned sha256 and its download
URLs.  A download goes to a temporary file next to its destination and only
replaces it after the sha256 matches -- nothing unverified is ever left where
the tool would run it.  When no URL works (offline, or the Sipeed download
site, which wants a captcha for files over 10 MB), the caller tells the
operator where to download it by hand and where to put it.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import sys
import threading
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .i18n import t
from .profiles import PLATFORM_KEY, RESOURCES_DIR

MANIFEST = RESOURCES_DIR / "tools.toml"
CHUNK = 256 * 1024


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    dest: Path                       # absolute
    sha256: str
    urls: tuple[str, ...]
    manual: str
    required: bool


def load(path: Path = MANIFEST, platform: str = PLATFORM_KEY) -> list[Tool]:
    """The tools this platform needs; [] when there is no manifest."""
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    root = path.parent
    return [Tool(id=str(e["id"]), name=str(e.get("name", e["id"])),
                 dest=(root / str(e["dest"])).resolve(),
                 sha256=str(e["sha256"]).lower(),
                 urls=tuple(str(u) for u in e.get("urls", [])),
                 manual=str(e.get("manual", "")),
                 required=bool(e.get("required", True)))
            for e in doc.get("tool", []) if e.get("platform") == platform]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def status(tool: Tool) -> str:
    """'ok' (present, sha256 matches), 'other' (present, another build -- it
    may still work, e.g. an older sigrok-cli placed by hand) or 'missing'."""
    if not tool.dest.is_file():
        return "missing"
    try:
        return "ok" if sha256_of(tool.dest) == tool.sha256 else "other"
    except OSError:
        return "missing"


def order_urls(urls: tuple[str, ...], prefer: str) -> list[str]:
    """`prefer` 'github' / 'sipeed' moves that host's URLs to the front;
    anything else keeps the manifest's order."""
    key = {"github": "github.com", "sipeed": "sipeed.com"}.get(prefer)
    if not key:
        return list(urls)
    return sorted(urls, key=lambda u: key not in u)


class DownloadError(Exception):
    pass


# CA bundles of the common distributions.  The frozen Linux build carries its
# own OpenSSL, whose default CA path is the one of the machine that built it
# (the CI's Ubuntu) and may not exist on the bench's distribution.
_CA_FILES = ("/etc/ssl/certs/ca-certificates.crt",      # Debian, Ubuntu, Arch
             "/etc/pki/tls/certs/ca-bundle.crt",        # Fedora, RHEL
             "/etc/ssl/ca-bundle.pem",                  # openSUSE
             "/etc/ssl/cert.pem")                       # Alpine, macOS


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if PLATFORM_KEY != "windows" and not ctx.get_ca_certs():
        for f in _CA_FILES:
            if os.path.isfile(f):
                ctx.load_verify_locations(cafile=f)
                break
    return ctx


def _fetch(url: str, tmp: Path, expect: str, progress, cancel,
           timeout: float) -> None:
    """Stream `url` into `tmp`, checking the sha256 on the way."""
    req = urllib.request.Request(url, headers={"User-Agent": "slogic-pt"})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())
    except urllib.error.HTTPError as e:
        raise DownloadError("HTTP %d" % e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError(str(getattr(e, "reason", e))) from None
    with resp:
        total = int(resp.headers.get("Content-Length") or 0)
        if "json" in (resp.headers.get("Content-Type") or ""):
            # the Sipeed site answers a large file with a JSON error, not the file
            raise DownloadError(t("the site asks for a captcha; download it by hand"))
        h = hashlib.sha256()
        got = 0
        with open(tmp, "wb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadError(t("Cancelled"))
                try:
                    block = resp.read(CHUNK)
                except OSError as e:
                    raise DownloadError(str(e)) from None
                if not block:
                    break
                f.write(block)
                h.update(block)
                got += len(block)
                if progress:
                    progress(got, total)
    if total and got != total:
        raise DownloadError(t("connection dropped at {got} of {total} bytes")
                            .format(got=got, total=total))
    if h.hexdigest() != expect:
        raise DownloadError(t("sha256 does not match the manifest (got {h}…)")
                            .format(h=h.hexdigest()[:12]))


def download(tool: Tool, log: Callable[[str], None], *, prefer: str = "auto",
             progress: Callable[[int, int], None] | None = None,
             cancel: threading.Event | None = None,
             timeout: float = 15) -> bool:
    """Fetch `tool` from its URLs in turn; True once a verified copy is in
    place.  Each failure is logged with its URL."""
    tool.dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tool.dest.with_name(tool.dest.name + ".part")
    for url in order_urls(tool.urls, prefer):
        if cancel is not None and cancel.is_set():
            break
        log(t("Downloading {name} from {url}").format(name=tool.name, url=url))
        try:
            _fetch(url, tmp, tool.sha256, progress, cancel, timeout)
        except DownloadError as e:
            log("   " + t("failed: {e}").format(e=e))
            tmp.unlink(missing_ok=True)
            continue
        if PLATFORM_KEY != "windows":
            tmp.chmod(0o755)
        os.replace(tmp, tool.dest)
        log("   " + t("verified (sha256 {h}…) -> {dest}").format(
            h=tool.sha256[:12], dest=tool.dest))
        return True
    tmp.unlink(missing_ok=True)
    return False


def manual_hint(tool: Tool) -> str:
    """Where to get `tool` by hand and where to put it."""
    name = tool.urls[0].rsplit("/", 1)[-1] if tool.urls else tool.name
    return t("Download {file} from {page} and save it as {dest}").format(
        file=name, page=tool.manual or "-", dest=tool.dest)


# ---------------------------------------------------------------- self-test

def _selftest() -> int:
    """Against a local HTTP server: a good file, a source that is down
    followed by a good one, a wrong sha256, a JSON 'captcha' reply."""
    import functools
    import http.server
    import tempfile

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    fails = 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "www").mkdir()
        body = os.urandom(300_000)
        (root / "www" / "tool.bin").write_bytes(body)
        good = hashlib.sha256(body).hexdigest()

        class H(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/captcha":
                    data = b'{"code":-1,"msg":"This is a large file"}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                super().do_GET()

        srv = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), functools.partial(H, directory=str(root / "www")))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        dead = "http://127.0.0.1:9/tool.bin"          # discard port: refused

        def case(name, urls, sha, want_ok, prefer="auto"):
            nonlocal fails
            tool = Tool("x", name, root / "out" / "tool.bin", sha, tuple(urls), "", True)
            tool.dest.unlink(missing_ok=True)
            lines = []
            ok = download(tool, lines.append, prefer=prefer, timeout=3)
            placed = tool.dest.is_file()
            leftover = tool.dest.with_name("tool.bin.part").exists()
            right = ok is want_ok and placed is want_ok and not leftover
            print("  %-30s -> %s %s" % (name, "ok" if ok else "failed",
                                        "PASS" if right else "FAIL"))
            if not right:
                print("\n".join("     " + ln for ln in lines))
                fails += 1

        case("good source", [base + "/tool.bin"], good, True)
        case("first source down", [dead, base + "/tool.bin"], good, True)
        case("wrong sha256", [base + "/tool.bin"], "0" * 64, False)
        case("captcha (JSON) reply", [base + "/captcha"], good, False)
        case("captcha, then good", [base + "/captcha", base + "/tool.bin"], good, True)
        srv.shutdown()
    print("order_urls sipeed first:",
          order_urls(("https://github.com/a", "https://dl.sipeed.com/b"), "sipeed"))
    print("manifest for %s:" % PLATFORM_KEY)
    for tool in load():
        print("  %-28s %-8s %s" % (tool.name, status(tool), tool.dest))
    print("RESULT:", "OK" if not fails else "FAIL")
    return fails


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--hash":
        print(sha256_of(Path(sys.argv[2])))      # maintainer: sha256 for the manifest
        sys.exit(0)
    sys.exit(_selftest())
