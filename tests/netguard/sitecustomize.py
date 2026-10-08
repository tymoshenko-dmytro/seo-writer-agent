"""Loaded by the tests (tests/conftest.py) and into every test subprocess (PYTHONPATH): no
connection may leave this machine. The tests stub every provider; a call that slips past a stub
must fail loudly instead of spending money or reaching a live WordPress. Loopback stays open for the local browser and the
Flask test server.
"""
import ipaddress
import socket

_connect = socket.socket.connect
_create = socket.create_connection
_getaddrinfo = socket.getaddrinfo


def _loopback(host) -> bool:
    if host in ("localhost", "", None):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


class NetworkBlocked(RuntimeError):
    pass


def _guard(host):
    if not _loopback(host):
        raise NetworkBlocked(f"tests may not reach the network (tried {host})")


def connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _guard(address[0])
    return _connect(self, address)


def create_connection(address, *a, **kw):
    _guard(address[0])
    return _create(address, *a, **kw)


def getaddrinfo(host, *a, **kw):
    _guard(host)
    return _getaddrinfo(host, *a, **kw)


socket.socket.connect = connect
socket.create_connection = create_connection
socket.getaddrinfo = getaddrinfo


# Inside Codex's sandbox (macOS Seatbelt, CODEX_SANDBOX=seatbelt) Chromium cannot start its own
# sandbox and closes at once; in one process, without its zygote, it runs. Only there: everywhere
# else the browser tests launch Chromium as the app does. Measured with `codex sandbox` on
# 2026-10-06: the plain launch failed, this one rendered a page.
if __import__("os").environ.get("CODEX_SANDBOX"):
    try:
        from playwright.sync_api import BrowserType as _BrowserType

        _IN_SANDBOX = ["--no-sandbox", "--single-process", "--no-zygote"]
        _launch = _BrowserType.launch

        def _launch_in_sandbox(self, *a, **kw):
            kw["args"] = list(kw.get("args") or []) + [x for x in _IN_SANDBOX if x not in (kw.get("args") or [])]
            kw["chromium_sandbox"] = False
            return _launch(self, *a, **kw)

        _BrowserType.launch = _launch_in_sandbox

        # single-process Chromium renders one page: the next page (or context) comes from a
        # browser of its own, closed with the first
        from playwright.sync_api import Browser as _Browser

        _new_page, _new_context, _close = _Browser.new_page, _Browser.new_context, _Browser.close

        def _spare(self):
            if not self.contexts:
                return self
            other = self.browser_type.launch()
            self.__dict__.setdefault("_spares", []).append(other)
            return other

        def _close_all(self, *a, **kw):
            for other in self.__dict__.get("_spares", []):
                try:
                    _close(other)
                except Exception:  # noqa: BLE001
                    pass
            return _close(self, *a, **kw)

        _Browser.new_page = lambda self, *a, **kw: _new_page(_spare(self), *a, **kw)
        _Browser.new_context = lambda self, *a, **kw: _new_context(_spare(self), *a, **kw)
        _Browser.close = _close_all
    except Exception:  # noqa: BLE001 — no Playwright: the browser tests skip as before
        pass
