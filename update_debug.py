#!/usr/bin/env python3
"""CSB Update Diagnose-Tool - auf dem Problem-PC ausfuehren."""
import os
import subprocess
import sys

TEMP = os.environ.get("TEMP", "")
LAD = os.environ.get("LOCALAPPDATA", "")

def check(label, path):
    if os.path.exists(path):
        stat = os.stat(path)
        size = stat.st_size
        from datetime import datetime
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[OK] {label}: {path}")
        print(f"     Size: {size:,} bytes, modified: {mtime}")
        return True
    else:
        print(f"[--] {label}: NOT FOUND")
        return False

def read_file(path, label):
    if os.path.exists(path):
        print(f"\n=== {label} ===")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            print(f.read()[:2000])
    else:
        print(f"\n=== {label}: file does not exist ===")

print("=" * 60)
print("CSB update diagnostics")
print("=" * 60)

# 1. Installierte Version
exe = os.path.join(LAD, "Programs", "ClaudeSessionBrowser", "ClaudeSessionBrowser.exe")
print("\n[1] Installed app:")
check("ClaudeSessionBrowser.exe", exe)

# 2. Temp-Dateien
print("\n[2] Update files in TEMP:")
setup = os.path.join(TEMP, "ClaudeSessionBrowser_setup.exe")
check("Setup.exe (Installer)", setup)

bat = os.path.join(TEMP, "csb_installer.bat")
check("Batch file", bat)

log = os.path.join(TEMP, "csb_update.log")
check("Update log", log)

fail = os.path.join(TEMP, "csb_update_failed.marker")
if check("Failure marker", fail):
    read_file(fail, "Failure marker contents")

lock = os.path.join(LAD, "ClaudeSessionBrowser.instance.lock")
check("Lock-File (Single-Instance)", lock)

# 3. Batch-Inhalt
read_file(bat, "Batch file contents")

# 4. Update-Log
read_file(log, "Update log contents")

# 5. Laufende Prozesse
print("\n[3] Running CSB processes:")
try:
    out = subprocess.check_output(
        'tasklist /FI "IMAGENAME eq ClaudeSessionBrowser*" /FO CSV',
        shell=True, text=True, stderr=subprocess.DEVNULL
    )
    lines = [l for l in out.strip().split("\n") if "ClaudeSessionBrowser" in l]
    if lines:
        for l in lines:
            print(f"     {l}")
    else:
        print("     No CSB processes running")
except Exception as e:
    print(f"     Error: {e}")

# 6. Installer manuell testen?
print("\n" + "=" * 60)
if os.path.exists(setup):
    print("Setup.exe exists! Want to test the installer by hand?")
    print("Run:")
    print(f'  "{setup}" /VERYSILENT /LOG="{TEMP}\\inno_test.log"')
    print(f'  type "{TEMP}\\inno_test.log"')
else:
    print("Setup.exe not found - the update was not started, or")
    print("the installer already ran and was deleted.")

print("\n[ENTER] to exit...")
input()
