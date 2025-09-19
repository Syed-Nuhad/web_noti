import os, sys, json, argparse, requests, tempfile
from urllib.parse import urljoin, urlparse
from playwright.sync_api import sync_playwright  # <-- make sure playwright is installed

# ENV: WN_BASE_URL, WN_API_KEY
BASE_URL = os.getenv("WN_BASE_URL", "http://127.0.0.1:8000/")
API_KEY  = os.getenv("WN_API_KEY", "")

CREATE_URL  = urljoin(BASE_URL, "api/source/create_key/")
COOKIES_URL = urljoin(BASE_URL, "api/source/import_cookies_key/")

def log(*a):
    print("[link]", *a, flush=True)

def get_cookies_with_playwright(login_url: str) -> dict:
    """
    Opens a visible Chromium window at login_url using a temporary user profile.
    You log in, then COME BACK TO THE TERMINAL and press ENTER. We capture cookies and close.
    """
    with tempfile.TemporaryDirectory() as user_data_dir:
        with sync_playwright() as pw:
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=False,
                args=["--disable-gpu-sandbox", "--no-sandbox"],
            )
            page = ctx.new_page()
            print("[link] Opening Chromium…")
            page.goto(login_url, timeout=60000)
            print("[link] Complete login in the browser window.")
            input("[link] When you are logged in (you can see your inbox), press ENTER here… ")
            cookies_list = ctx.cookies()
            ctx.close()

    jar = {}
    for c in cookies_list:
        jar[c["name"]] = c["value"]
    return jar

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
    ap.add_argument("--login", action="store_true", help="Open Chromium to log in and capture cookies")
    args = ap.parse_args()

    headers = {"Authorization": f"ApiKey {API_KEY}", "Content-Type": "application/json"}

    # 1) Create source on the server
    create_payload = {"key": API_KEY, "name": args.name, "check_url": args.url}
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
