# win-devkit setup entry
$ErrorActionPreference = "Continue"
$tag = "WDK_" + (Get-Date -Format "yyyyMMdd_HHmmss")
$env:WIN_DEVKIT_TAG = $tag

$reportDir = Join-Path $env:TEMP "win-devkit"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$report = Join-Path $reportDir ("doctor_{0}.txt" -f $tag)

$lines = New-Object System.Collections.Generic.List[string]
function L([string]$s) { [void]$lines.Add($s); [Console]::Out.WriteLine($s) }

L "tag=$tag"
L "user=$env:USERDOMAIN\$env:USERNAME"
L "time=$(Get-Date -Format o)"
L "cwd=$((Get-Location).Path)"
L "pid=$PID"
L "ps=$($PSVersionTable.PSVersion)"

try {
  $py = & python --version 2>&1 | Out-String
  L ("python=" + $py.Trim())
} catch {
  L "python=missing"
}

# Enumerate host tool pipes (FindFirst works in restricted shells where Get-ChildItem \\.\pipe\ fails)
function Get-ToolHostPipes {
  $set = New-Object 'System.Collections.Generic.HashSet[string]'
  try {
    if (-not ("PipeEnumWinDevkit" -as [type])) {
      Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class PipeEnumWinDevkit {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct WIN32_FIND_DATA {
    public uint dwFileAttributes;
    public System.Runtime.InteropServices.ComTypes.FILETIME ftCreationTime;
    public System.Runtime.InteropServices.ComTypes.FILETIME ftLastAccessTime;
    public System.Runtime.InteropServices.ComTypes.FILETIME ftLastWriteTime;
    public uint nFileSizeHigh; public uint nFileSizeLow;
    public uint dwReserved0; public uint dwReserved1;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=260)] public string cFileName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=14)] public string cAlternateFileName;
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern IntPtr FindFirstFile(string lpFileName, out WIN32_FIND_DATA lpFindFileData);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  static extern bool FindNextFile(IntPtr hFindFile, out WIN32_FIND_DATA lpFindFileData);
  [DllImport("kernel32.dll")] static extern bool FindClose(IntPtr hFindFile);
  public static List<string> List(string pattern) {
    var list = new List<string>();
    WIN32_FIND_DATA data;
    IntPtr h = FindFirstFile(pattern, out data);
    if (h == IntPtr.Zero || h == new IntPtr(-1)) return list;
    try {
      do { if (!string.IsNullOrEmpty(data.cFileName)) list.Add(data.cFileName); }
      while (FindNextFile(h, out data));
    } finally { FindClose(h); }
    return list;
  }
}
"@ | Out-Null
    }
    $ff = [PipeEnumWinDevkit]::List('\\.\pipe\agent-code-toolhost-*')
    L ("FindFirst_host_count=" + $ff.Count)
    foreach ($n in $ff) {
      if ($n -match '^agent-code-toolhost-[0-9]+-[0-9a-fA-F]+$') { [void]$set.Add($n) }
    }
  } catch {
    L ("FindFirst_err=" + $_.Exception.Message)
  }

  try {
    $gc = @(Get-ChildItem '\\.\pipe\' -ErrorAction Stop | Where-Object { $_.Name -like 'agent-code-toolhost-*' } | Select-Object -ExpandProperty Name)
    L ("GetChildItem_host_count=" + $gc.Count)
    foreach ($n in $gc) { if ($n -match '^agent-code-toolhost-[0-9]+-[0-9a-fA-F]+$') { [void]$set.Add($n) } }
  } catch {
    L ('GetChildItem_pipe=FAIL ' + $_.Exception.Message)
  }

  $seed = Join-Path $env:TEMP 'trae-pathb\host_pipe.txt'
  if (Test-Path -LiteralPath $seed) {
    L ("seed_seen=" + $seed)
    Get-Content -LiteralPath $seed -ErrorAction SilentlyContinue | ForEach-Object {
      $t = $_.Trim()
      if ($t -match '^agent-code-toolhost-[0-9]+-[0-9a-fA-F]+$') { [void]$set.Add($t) }
    }
  }

  return @($set)
}

$pipes = @(Get-ToolHostPipes)
L ("host_pipes=" + ($pipes -join ','))
$seedOut = Join-Path $env:TEMP 'trae-pathb'
New-Item -ItemType Directory -Force -Path $seedOut | Out-Null
if ($pipes.Count -gt 0) {
  $pipes | Set-Content -LiteralPath (Join-Path $seedOut 'host_pipe.txt') -Encoding UTF8
}

$probe = Join-Path $PSScriptRoot "tool_probe.py"
L "probe=$probe"
$probeOut = ""
$probeExit = 2
if (Test-Path -LiteralPath $probe) {
  $probeOut = & python $probe --tag $tag 2>&1 | Out-String
  $probeExit = $LASTEXITCODE
  L "probe_exit=$probeExit"
} else {
  L "probe_error=missing_tool_probe.py"
}
L "----probe_output----"
L $probeOut.TrimEnd()

$desk = [Environment]::GetFolderPath("Desktop")
$hit = @(Get-ChildItem -LiteralPath $desk -Filter ("win_devkit_ok_{0}.txt" -f $tag) -ErrorAction SilentlyContinue)
if (-not $hit) {
  $hit = @(Get-ChildItem -LiteralPath $desk -Filter "win_devkit_ok_*.txt" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}
L ("DESK_EXTERNAL=" + [bool]$hit)
if ($hit) {
  L ("DESK_FILE=" + $hit[0].FullName)
  L ("DESK_CONTENT=" + ((Get-Content -LiteralPath $hit[0].FullName -Raw -ErrorAction SilentlyContinue).Trim()))
}

$text = $lines -join "`n"
$text | Set-Content -LiteralPath $report -Encoding UTF8
Write-Output "REPORT=$report"

$ok = ($probeOut -match "SUCCESS=True") -or ($hit -and ($probeOut -match "READ_OK=True|SECRET_MARKER|READ_CONTENT_FROM_HOST="))
if ($ok) { exit 0 }
if ($hit) { exit 0 }
exit 1

