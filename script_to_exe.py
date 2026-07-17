import ctypes
import os
import subprocess
import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "dist" / "hyperagent-client.exe"

subprocess.check_call(
    [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt"), "pyinstaller"],
    cwd=ROOT,
)

subprocess.check_call(
    [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "hyperagent-client",
        "--paths",
        str(ROOT),
        "--distpath",
        str(ROOT / "dist"),
        str(ROOT / "client" / "qt_main.py"),
    ],
    cwd=ROOT,
)

items = {
    r"Software\Classes\Directory\Background\shell\HyperagentClient": "%V",
    r"Software\Classes\Directory\shell\HyperagentClient": "%1",
    r"Software\Classes\Drive\shell\HyperagentClient": "%1",
}

for key_path, work_dir in items.items():
    command = f'"{EXE}" --work-dir "{work_dir}"'

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "Open Hyperagent Client here")
        winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, str(EXE))

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\command") as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, command)

exe_dir = str(EXE.parent)
with winreg.CreateKeyEx(
    winreg.HKEY_CURRENT_USER,
    r"Environment",
    0,
    winreg.KEY_READ | winreg.KEY_WRITE,
) as key:
    try:
        path, value_type = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        path, value_type = "", winreg.REG_EXPAND_SZ

    paths = [item for item in path.split(os.pathsep) if item]
    known_paths = [item.rstrip("\\/").casefold() for item in paths]
    if exe_dir.casefold() not in known_paths:
        paths.append(exe_dir)
        winreg.SetValueEx(key, "Path", 0, value_type, os.pathsep.join(paths))

ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0, 5000, None)

print(f"Created: {EXE}")
print("Context menu item: Open Hyperagent Client here")
print(f"Added to PATH: {EXE.parent}")
