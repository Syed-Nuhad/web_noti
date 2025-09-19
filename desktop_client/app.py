# desktop_client/app.py
import os, time, json, threading, requests, tkinter as tk
from urllib.parse import urljoin
from django.views.decorators.csrf import csrf_exempt

BASE_URL = os.getenv("WN_BASE_URL", "http://127.0.0.1:8000/")
API_KEY  = os.getenv("WN_API_KEY", "")
APP_NAME = os.getenv("WN_APP_NAME", "WebNotify")
POLL_SEC = int(os.getenv("WN_POLL_SEC", "10"))

ACTIVE_URL = urljoin(BASE_URL, "api/active_notification/")
MARK_URL   = urljoin(BASE_URL, "api/mark_notifications_read/")
SOUND_URL  = urljoin(BASE_URL, "api/sound/")

import pygame
pygame.mixer.init()

def log(msg):
    print(f"[client] {msg}", flush=True)

def fetch_json(url):
    headers = {"Authorization": f"ApiKey {API_KEY}"} if API_KEY else {}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()

def post_json(url):
    headers = {"Authorization": f"ApiKey {API_KEY}"} if API_KEY else {}
    r = requests.post(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json() if r.content else {}

def download_sound(path):
    headers = {"Authorization": f"ApiKey {API_KEY}"} if API_KEY else {}
    r = requests.get(SOUND_URL, headers=headers, timeout=20)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path



def show_native_popup(title_text, message_text, stop_event):
    """A centered, native-looking dialog window (Windows-style)."""
    root = tk.Tk()
    root.title(APP_NAME)
    root.configure(bg="#f3f3f3")           # light system-ish background
    root.resizable(False, False)
    root.attributes("-topmost", True)

    # Try to set the WINDOW ICON (titlebar) if bell.ico exists
    ico_path = os.path.join(os.path.dirname(__file__), "bell.ico")
    if os.path.exists(ico_path):
        try:
            root.iconbitmap(ico_path)
        except Exception:
            pass

    # Size like a Windows dialog and center it
    W, H = 440, 240
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    x = int((sw - W) / 2)
    y = int((sh - H) / 2)
    root.geometry(f"{W}x{H}+{x}+{y}")

    # Outer container
    outer = tk.Frame(root, bg="#f3f3f3", padx=20, pady=16)
    outer.pack(fill="both", expand=True)

    # Top row: icon + title
    top = tk.Frame(outer, bg="#f3f3f3")
    top.pack(fill="x")

    # In-dialog ICON: prefer PNG bell_48.png, else emoji fallback
    icon_lbl = None
    png_path = os.path.join(os.path.dirname(__file__), "bell_48.png")
    if os.path.exists(png_path):
        try:
            bell_img = tk.PhotoImage(file=png_path)
            icon_lbl = tk.Label(top, image=bell_img, bg="#f3f3f3")
            icon_lbl.image = bell_img  # keep reference
        except Exception:
            icon_lbl = None
    if icon_lbl is None:
        icon_lbl = tk.Label(top, text="🔔", bg="#f3f3f3", fg="#0d6efd",
                            font=("Segoe UI Emoji", 28))
    icon_lbl.pack(side="left")

    title_lbl = tk.Label(top, text=title_text, bg="#f3f3f3", fg="#111",
                         font=("Segoe UI", 14, "bold"), anchor="w", justify="left")
    title_lbl.pack(side="left", padx=10)

    # Message body
    body = tk.Label(outer, text=message_text, bg="#f3f3f3", fg="#333",
                    font=("Segoe UI", 11), wraplength=W-40, justify="left")
    body.pack(fill="x", pady=(10, 12))

    # Button row (right-aligned)
    btn_row = tk.Frame(outer, bg="#f3f3f3")
    btn_row.pack(fill="x", pady=(6, 0))

    def do_close():
        stop_event.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        root.destroy()

    close_btn = tk.Button(btn_row, text="Close (Esc)",
                          font=("Segoe UI", 10, "bold"), padx=14, pady=6,
                          command=do_close)
    close_btn.pack(side="right")

    # Key bindings
    root.bind("<Escape>", lambda e: do_close())
    root.protocol("WM_DELETE_WINDOW", do_close)

    # Keep on top even if focus changes
    root.lift()
    root.after(100, lambda: root.attributes("-topmost", True))

    root.mainloop()




def show_fullscreen(title_text, message_text, stop_event):
    root = tk.Tk()
    root.title(APP_NAME)
    root.attributes("-topmost", True)
    root.attributes("-fullscreen", True)
    root.configure(bg="black")

    # keep it on top even if some desktops steal focus
    root.lift()
    root.after(100, lambda: root.attributes("-topmost", True))

    w = root.winfo_screenwidth() - 200

    title_lbl = tk.Label(
        root, text=title_text, fg="white", bg="black",
        font=("Segoe UI", 42, "bold"), wraplength=w, justify="center"
    )
    title_lbl.pack(pady=30)

    msg_lbl = tk.Label(
        root, text=message_text, fg="#dddddd", bg="black",
        font=("Segoe UI", 24), wraplength=w, justify="center"
    )
    msg_lbl.pack(pady=10)

    def do_close():
        stop_event.set()
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        root.destroy()

    close_btn = tk.Button(
        root, text="Close (Esc)", font=("Segoe UI", 16, "bold"),
        command=do_close, relief="raised", padx=20, pady=10
    )
    close_btn.pack(pady=40)

    root.bind("<Escape>", lambda e: do_close())
    root.protocol("WM_DELETE_WINDOW", do_close)
    root.mainloop()

def play_sound_loop(sound_path, stop_event, times=None):
    try:
        loop_forever = (times is None)
        played = 0
        while loop_forever or played < max(0, int(times)):
            if stop_event.is_set():
                break
            log(f"playing sound ({'∞' if loop_forever else f'{played+1}/{times}'})")
            pygame.mixer.music.load(sound_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                if stop_event.is_set():
                    pygame.mixer.music.stop()
                    break
                pygame.time.wait(100)
            played += 1
        pygame.mixer.music.stop()
    except Exception as e:
        log(f"audio error: {e}")


def run_once():
    data = fetch_json(ACTIVE_URL)  # expects {"has": bool, ...}
    if not data.get("has"):
        log("no new notification")
        return False

    # Build texts
    title_text   = f"You have a message from {data.get('source_name') or APP_NAME}"
    message_text = data.get("title") or data.get("message") or "New activity detected"
    ring_count   = int(data.get("ring_count", 1))  # kept for future use if needed

    # 1) MARK AS READ **BEFORE** SHOWING to prevent re-pops on the next poll
    try:
        post_json(MARK_URL)
        log("marked notification(s) read to avoid re-trigger")
    except Exception as e:
        log(f"mark-read pre-show failed (will still show): {e}")

    # 2) pick ringtone: prefer local test.wav, else test.mp3, else server ringtone
    dir_here = os.path.dirname(__file__)
    wav_path = os.path.join(dir_here, "test.wav")
    mp3_path = os.path.join(dir_here, "test.mp3")
    sound_path = None

    if os.path.exists(wav_path):
        sound_path = wav_path
        log(f"using local ringtone: {sound_path}")
    elif os.path.exists(mp3_path):
        sound_path = mp3_path
        log(f"using local ringtone: {sound_path}")
    else:
        temp = os.path.join(dir_here, "_ringtone.bin")
        try:
            download_sound(temp)
            sound_path = temp
            log("downloaded ringtone from server")
        except Exception as e:
            log(f"download sound failed: {e}")
            sound_path = None

    # 3) create a stop flag shared between popup and audio thread
    stop_event = threading.Event()

    # 4) Start audio loop: INFINITE until the popup is closed
    if sound_path:
        threading.Thread(
            target=play_sound_loop,
            args=(sound_path, stop_event, None),  # None => infinite loop until stop_event is set
            daemon=True
        ).start()
    else:
        log("no sound file available; skipping audio")

    # 5) block the loop with a fullscreen window until *you* close it
    show_native_popup(title_text, message_text, stop_event)

    # 6) safety: ensure audio is stopped after window closes
    stop_event.set()
    try:
        pygame.mixer.music.stop()
    except Exception:
        pass

    return True


def main():
    log(f"BASE_URL={BASE_URL}")
    log(f"APP_NAME={APP_NAME}")
    if not API_KEY or API_KEY.strip().startswith("<PASTE"):
        log("ERROR: Set WN_API_KEY to a REAL user API key.")
        return

    while True:
        try:
            changed = run_once()
        except Exception as e:
            log(f"poll error: {e}")
        time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()

