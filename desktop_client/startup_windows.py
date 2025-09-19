import os, sys, winshell
from win32com.client import Dispatch

# Requires: pip install pypiwin32 winshell
def add_to_startup(script_path, name="WebNotify.lnk"):
    startup = winshell.startup()
    shell = Dispatch('WScript.Shell')
    shortcut = shell.CreateShortCut(os.path.join(startup, name))
    shortcut.Targetpath = sys.executable
    shortcut.Arguments  = f'"{script_path}"'
    shortcut.WorkingDirectory = os.path.dirname(script_path)
    shortcut.IconLocation = sys.executable
    shortcut.save()

if __name__ == "__main__":
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), "app.py"))
    add_to_startup(here)
    print("Added to startup.")
