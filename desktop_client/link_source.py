import os, sys, json, time, argparse, requests, tempfile
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout  # make sure playwright is installed

# ENV: WN_BASE_URL, WN_API_KEY
BASE_URL = os.getenv("WN_BASE_URL", "http://127.0.0.1:8000/")
API_KEY  = os.getenv("WN_API_KEY", "")

CREATE_URL  = urljoin(BASE_URL, "api/source/create_key/")
COOKIES_URL = urljoin(BASE_URL, "api/source/import_cookies_key/")

def log(*a):
    print("[link]", *a, flush=True)


def get_cookies_with_playwright(login_url: str) -> dict:
    """
    Opens a real Chrome/Edge/Chromium with a persistent profile to let you log in.
    After you press ENTER in the terminal, we read cookies from storage_state().
    If you accidentally closed the window, we relaunch the same profile just to read cookies.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Error as PWError
    import time
    import json

    profile_dir = os.path.join(os.path.dirname(__file__), ".pw_profile")
    os.makedirs(profile_dir, exist_ok=True)

    def launch_ctx(pw, channel):
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-extensions",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-renderer-backgrounding",
            "--disable-features=IsolateOrigins,site-per-process",
            "--password-store=basic",
        ]
        return pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            channel=channel,           # "chrome", "msedge", or None
            args=args,
            viewport={"width": 1280, "height": 800},
        )

    def read_cookies_from_ctx(ctx) -> dict:
        # storage_state returns {"cookies":[...], "origins":[...]}
        state = ctx.storage_state()
        cookies_list = (state or {}).get("cookies", [])
        jar = {}
        for c in cookies_list:
            # keep simple name -> value map; server doesn’t need full cookie objects
            jar[c.get("name")] = c.get("value")
        return jar

    with sync_playwright() as pw:
        ctx = None
        # Try Chrome → Edge → bundled Chromium
        for channel in ("chrome", "msedge", None):
            try:
                ctx = launch_ctx(pw, channel)
                break
            except Exception:
                ctx = None
        if ctx is None:
            raise RuntimeError("Could not launch Chrome/Edge/Chromium")

        # Open a page and guide the login
        page = ctx.new_page()
        try:
            page.set_extra_http_headers({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
                )
            })
        except PWError:
            pass

        print("[link] Opening browser…")
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=90_000)
        except PWTimeout:
            print("[link] Initial load timed out; you can still log in manually in that window.")

        # If Google SSO pops a window and it’s blank, manual hint:
        print("[link] Log in in the browser window. If a popup is blank, refresh it or open https://accounts.google.com/")
        print("[link] IMPORTANT: Do NOT close the browser entirely. Just finish login and come back here.")
        input("[link] When you can see you’re logged in, press ENTER here to capture cookies… ")

        # Try to read cookies without closing the context
        try:
            jar = read_cookies_from_ctx(ctx)
        except PWError:
            jar = {}

        # If the context was closed by accident, relaunch profile and read cookies
        if not jar:
            try:
                ctx.close()
            except Exception:
                pass
            try:
                ctx = launch_ctx(pw, "chrome")
            except Exception:
                try:
                    ctx = launch_ctx(pw, "msedge")
                except Exception:
                    ctx = launch_ctx(pw, None)
            # No navigation needed; just read storage_state from persisted profile
            try:
                jar = read_cookies_from_ctx(ctx)
            except PWError:
                jar = {}

        try:
            ctx.close()
        except Exception:
            pass

    return jar or {}


def get_cookies_for_domain(domain: str) -> dict:
    """
    Try to read cookies from local Chrome/Edge/Firefox stores.
    1) exact host (e.g. www.fiverr.com)
    2) parent domain (e.g. fiverr.com)
    3) ALL cookies, filter by root domain
    """
    try:
        import browser_cookie3
    except ImportError:
        return {}

    def _collect(jar):
        out = {}
        for c in jar:
            out[c.name] = c.value
        return out

    cookies = {}
    tried = []

    host = (domain or "").lstrip(".").strip().lower()
    root = host.split(":", 1)[0]
    parts = root.split(".")
    parent = ".".join(parts[1:]) if len(parts) > 2 else root  # e.g. fiverr.com

    # 1) exact host
    for getter in (browser_cookie3.chrome, browser_cookie3.edge, browser_cookie3.firefox):
        try:
            jar = getter(domain_name=host)
            tried.append(f"{getter.__name__}({host})")
            cookies.update(_collect(jar))
        except Exception:
            pass

    # 2) parent domain
    if not cookies and parent and parent != host:
        for getter in (browser_cookie3.chrome, browser_cookie3.edge, browser_cookie3.firefox):
            try:
                jar = getter(domain_name=parent)
                tried.append(f"{getter.__name__}({parent})")
                cookies.update(_collect(jar))
            except Exception:
                pass

    # 3) ALL, filtered
    if not cookies and parent:
        for getter in (browser_cookie3.chrome, browser_cookie3.edge, browser_cookie3.firefox):
            try:
                jar = getter()  # all cookies
                tried.append(f"{getter.__name__}(ALL)")
                for c in jar:
                    if getattr(c, "domain", None) and parent in c.domain.lower():
                        cookies[c.name] = c.value
            except Exception:
                pass

    log("cookie tried:", ", ".join(tried) or "none")
    return cookies


def main():
    if not API_KEY:
        raise SystemExit("Set WN_API_KEY to your real key.")

    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="App name (e.g., Fiverr)")
    ap.add_argument("--url",  required=True, help="Inbox URL (e.g., https://www.fiverr.com/inbox)")
    ap.add_argument("--rendered", action="store_true",
                    help="Use headless browser rendering for this source (for dynamic pages).")

    ap.add_argument("--login", action="store_true", help="Open browser to log in and capture cookies")
    args = ap.parse_args()

    headers = {"Authorization": f"ApiKey {API_KEY}", "Content-Type": "application/json"}

    # 1) Create source on the server
    create_payload = {"key": API_KEY, "name": args.name, "check_url": args.url}
    if args.rendered:
        create_payload["rendered"] = True
    r = requests.post(CREATE_URL, headers=headers, data=json.dumps(create_payload), timeout=20)
    if r.status_code != 200:
        print("[link] create failed:", r.status_code, r.text, file=sys.stderr)
        r.raise_for_status()
    sid = r.json()["id"]
    log("created source id:", sid)

    # 2) Cookies: local stores first, then Playwright fallback (or force with --login)
    domain = urlparse(args.url).hostname

    if args.login:
        cookies = get_cookies_with_playwright(args.url)
        if cookies:
            print(f"[link] captured {len(cookies)} cookies via Playwright")
    else:
        cookies = get_cookies_for_domain(domain)
        if cookies:
            print(f"[link] captured {len(cookies)} cookies from local browser store")
        else:
            print("[link] no local cookies; opening temporary browser to log in…")
            cookies = get_cookies_with_playwright(args.url)
            if cookies:
                print(f"[link] captured {len(cookies)} cookies via Playwright")

    # 3) Upload cookies (if any)
    if cookies:
        payload = {"key": API_KEY, "source_id": sid, "cookies": cookies}
        r2 = requests.post(COOKIES_URL, headers=headers, data=json.dumps(payload), timeout=30)
        if r2.status_code != 200:
            print("[link] cookie upload failed:", r2.status_code, r2.text, file=sys.stderr)
            r2.raise_for_status()
        log("cookies uploaded")
    else:
        log("continuing without cookies (public pages only)")

    log("done.")


if __name__ == "__main__":
    main()
