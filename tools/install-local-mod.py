"""Compile a Windhawk .wh.cpp mod and register it as a local mod.

Mirrors what the Windhawk 1.7.x editor does when you press "Compile":
  * compiles with the bundled clang into Engine\\Mods\\<arch>\\
  * writes HKLM\\SOFTWARE\\Windhawk\\Engine\\Mods\\local@<id> (+ default settings)
  * stores the source in ModsSource\\local@<id>.wh.cpp so it shows up in the UI

Usage (elevated):  python install-local-mod.py <mod.wh.cpp> [--disabled] [--logging]
"""
import os
import random
import re
import subprocess
import sys
import time
import winreg

import yaml

WINDHAWK_DIR = r"C:\Program Files\Windhawk"
APPDATA_DIR = r"C:\ProgramData\Windhawk"
REG_MODS = r"SOFTWARE\Windhawk\Engine\Mods"


def read_ini_engine_path():
    raw = open(os.path.join(WINDHAWK_DIR, "windhawk.ini"), "rb").read()
    ini = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    m = re.search(r"^EnginePath=(.+)$", ini, re.M)
    return os.path.join(WINDHAWK_DIR, m.group(1).strip())


def extract_block(src, start, end):
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), src, re.S)
    return m.group(1) if m else None


def parse_metadata(src):
    block = extract_block(src, "// ==WindhawkMod==", "// ==/WindhawkMod==")
    meta = {}
    for line in block.splitlines():
        m = re.match(r"\s*//\s*@(\S+)\s+(.*?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key in ("include", "exclude", "architecture"):
            meta.setdefault(key, []).append(val)
        else:
            meta[key] = val
    return meta


def parse_initial_settings(src):
    block = extract_block(src, "// ==WindhawkModSettings==", "// ==/WindhawkModSettings==")
    if not block:
        return {}
    block = block.strip()
    block = re.sub(r"^/\*", "", block)
    block = re.sub(r"\*/$", "", block)
    settings = yaml.safe_load(block) or []
    parsed = {}

    def parse_list(items, prefix=""):
        for item in items:
            keys = [k for k in item if not str(k).startswith("$")]
            assert len(keys) == 1, f"bad settings entry: {item}"
            key = (prefix + "." if prefix else "") + keys[0]
            parse_value(item[keys[0]], key)

    def parse_value(value, key):
        if isinstance(value, bool):
            parsed[key] = 1 if value else 0
        elif isinstance(value, (int, str)):
            parsed[key] = value
        elif isinstance(value, float):
            raise ValueError(f"float setting not supported: {key}")
        elif value and isinstance(value[0], (int, str)) and not isinstance(value[0], bool):
            for i, v in enumerate(value):
                parse_value(v, f"{key}[{i}]")
        elif value and isinstance(value[0], list):
            for i, v in enumerate(value):
                parse_list(v, f"{key}[{i}]")
        else:
            parse_list(value, key)

    parse_list(settings)
    return parsed


def compile_mod(src, mod_id, version, compiler_options, engine_path):
    compiler = os.path.join(WINDHAWK_DIR, "Compiler")
    target, sub = "x86_64-w64-mingw32", "64"
    out_dir = os.path.join(APPDATA_DIR, "Engine", "Mods", sub)
    while True:
        dll_name = f"{mod_id}_{version}_{random.randint(100000, 999999)}.dll"
        if not os.path.exists(os.path.join(out_dir, dll_name)):
            break
    args = [
        os.path.join(compiler, "bin", "clang++.exe"),
        "-std=c++23", "-O2", "-shared", "-DUNICODE", "-D_UNICODE",
        "-DWINVER=0x0A00", "-D_WIN32_WINNT=0x0A00", "-D_WIN32_IE=0x0A00",
        "-DNTDDI_VERSION=0x0A000008", "-D__USE_MINGW_ANSI_STDIO=0", "-DWH_MOD",
        f'-DWH_MOD_ID=L"{mod_id}"', f'-DWH_MOD_VERSION=L"{version}"',
        os.path.join(engine_path, sub, "windhawk.lib"),
        "-x", "c++", "-", "-include", "windhawk_api.h",
        "-target", target, "-Wl,--export-all-symbols",
        "-o", os.path.join(out_dir, dll_name),
    ] + (compiler_options.split() if compiler_options else [])
    t = time.time()
    p = subprocess.run(args, input=src.encode("utf-8"), cwd=compiler, capture_output=True)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    print(f"clang exit={p.returncode} in {time.time() - t:.1f}s")
    if out.strip():
        print(out[-6000:])
    if p.returncode != 0:
        sys.exit(1)
    return dll_name, out_dir


def main():
    path = sys.argv[1]
    disabled = "--disabled" in sys.argv
    logging = "--logging" in sys.argv
    src = open(path, encoding="utf-8").read()
    meta = parse_metadata(src)
    mod_id = "local@" + meta["id"]
    version = meta.get("version", "")
    settings = parse_initial_settings(src)
    engine_path = read_ini_engine_path()

    dll_name, out_dir = compile_mod(src, mod_id, version, meta.get("compilerOptions"), engine_path)

    view = winreg.KEY_WOW64_64KEY
    key = winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, REG_MODS + "\\" + mod_id, 0,
                             winreg.KEY_ALL_ACCESS | view)
    winreg.SetValueEx(key, "Disabled", 0, winreg.REG_DWORD, 1 if disabled else 0)
    winreg.SetValueEx(key, "LoggingEnabled", 0, winreg.REG_DWORD, 1 if logging else 0)
    winreg.SetValueEx(key, "Include", 0, winreg.REG_SZ, "|".join(meta.get("include", [])))
    winreg.SetValueEx(key, "Exclude", 0, winreg.REG_SZ, "|".join(meta.get("exclude", [])))
    winreg.SetValueEx(key, "Architecture", 0, winreg.REG_SZ, "|".join(meta.get("architecture", [])))
    winreg.SetValueEx(key, "Version", 0, winreg.REG_SZ, version)

    skey = winreg.CreateKeyEx(key, "Settings", 0, winreg.KEY_ALL_ACCESS | view)
    existing = set()
    i = 0
    while True:
        try:
            existing.add(winreg.EnumValue(skey, i)[0])
            i += 1
        except OSError:
            break
    added = 0
    for name, value in settings.items():
        if name in existing:
            continue
        if isinstance(value, int):
            winreg.SetValueEx(skey, name, 0, winreg.REG_DWORD, value & 0xFFFFFFFF)
        else:
            winreg.SetValueEx(skey, name, 0, winreg.REG_SZ, value)
        added += 1
    winreg.SetValueEx(key, "SettingsChangeTime", 0, winreg.REG_DWORD, int(time.time()) & 0x7FFFFFFF)
    # Setting the library file name last makes the engine (re)load the new DLL.
    winreg.SetValueEx(key, "LibraryFileName", 0, winreg.REG_SZ, dll_name)
    print(f"registered {mod_id} -> {dll_name} (settings: {added} new, {len(existing)} kept)")

    with open(os.path.join(APPDATA_DIR, "ModsSource", mod_id + ".wh.cpp"), "w", encoding="utf-8", newline="") as f:
        f.write(src)

    # Clean up older builds of this mod (skip ones still loaded by a process).
    for name in os.listdir(out_dir):
        if name.startswith(mod_id + "_") and name != dll_name:
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass


if __name__ == "__main__":
    main()
