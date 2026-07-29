"""Run the approved G2 A/B/C Windows-shell capability measurements.

The script intentionally keeps physically interactive focus-owner alternation and
assistive-technology usability separate from automated evidence.  It never turns a
missing physical observation into a pass.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import platform
import queue
import statistics
import subprocess
import sys
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
ARTIFACT_ROOT = REPO / ".omx" / "artifacts" / "windows-companion-shell-spike"
PROTOCOL_VERSION = 1
CREATE_NO_WINDOW = 0x08000000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
KEYEVENTF_KEYUP = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_VM_READ = 0x0010
SW_RESTORE = 9
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000

REQUIRED_STATES = [
    "idle",
    "recording",
    "transcribing",
    "awaiting confirmation",
    "delivering",
    "waiting for assistant",
    "planning",
    "speaking",
    "paused",
    "stopping",
    "disconnected",
    "recoverable error",
]

CANDIDATES = {
    "A": {"name": "Tk + Win32", "vk": 0x85},
    "B": {"name": "WPF + named-pipe NDJSON", "vk": 0x86},
    "C": {"name": "headless recovery", "vk": 0x87},
}


def ensure_native_bridge() -> Path:
    output = ARTIFACT_ROOT / "native_pipe_bridge.exe"
    source = ROOT / "native_pipe_bridge.cs"
    if output.exists() and output.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    command = f"Add-Type -Path '{source}' -OutputAssembly '{output}' -OutputType ConsoleApplication"
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], check=True, timeout=20, creationflags=CREATE_NO_WINDOW)
    return output


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


class FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def create_kill_job() -> int:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    handle = kernel32.CreateJobObjectW(None, None)
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not handle or not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        raise OSError("unable to create kill-on-close Job Object")
    return int(handle)


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def foreground() -> dict[str, Any]:
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow())
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return {"hwnd": hwnd, "pid": int(pid.value), "title": buffer.value[:120]}


def top_level_windows() -> list[dict[str, Any]]:
    user32 = ctypes.windll.user32
    windows: list[dict[str, Any]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))
        windows.append({"hwnd": int(hwnd), "pid": int(pid.value), "class": class_buffer.value})
        return True

    user32.EnumWindows(callback, 0)
    return windows


def process_image(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def wait_new_window(before: set[int], images: set[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for item in top_level_windows():
            if item["hwnd"] not in before and process_image(item["pid"]) in images:
                return item
        time.sleep(0.05)
    raise TimeoutError(f"no new top-level window for {sorted(images)}")


def force_foreground(hwnd: int, timeout: float = 2.0) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    current_thread = kernel32.GetCurrentThreadId()
    foreground_hwnd = user32.GetForegroundWindow()
    foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    attached_foreground = bool(foreground_thread and foreground_thread != current_thread and user32.AttachThreadInput(current_thread, foreground_thread, True))
    attached_target = bool(target_thread and target_thread != current_thread and user32.AttachThreadInput(current_thread, target_thread, True))
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached_target:
            user32.AttachThreadInput(current_thread, target_thread, False)
        if attached_foreground:
            user32.AttachThreadInput(current_thread, foreground_thread, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if int(user32.GetForegroundWindow()) == hwnd:
            return True
        time.sleep(0.02)
    return False


class DisposableFocusOwners:
    def __init__(self) -> None:
        self.notepad: subprocess.Popen[str] | None = None
        self.terminal_client: subprocess.Popen[str] | None = None
        self.windows: dict[str, dict[str, Any]] = {}
        self.owner_shell_marker = ""

    def start(self) -> None:
        before = {item["hwnd"] for item in top_level_windows()}
        self.notepad = subprocess.Popen(["notepad.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        self.windows["notepad"] = wait_new_window(before, {"notepad.exe"})
        before = {item["hwnd"] for item in top_level_windows()}
        name = "ttmc-g2-" + uuid.uuid4().hex[:10]
        self.owner_shell_marker = "TTMC_G2_OWNER_" + uuid.uuid4().hex[:12]
        self.terminal_client = subprocess.Popen(
            ["wt.exe", "-w", name, "new-tab", "cmd.exe", "/k", "title", self.owner_shell_marker],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.windows["windows_terminal"] = wait_new_window(before, {"windowsterminal.exe"}, timeout=15)

    def focus(self, label: str) -> bool:
        return force_foreground(int(self.windows[label]["hwnd"]))

    def is_foreground(self, label: str) -> bool:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(self.windows[label]["hwnd"])

    def close(self) -> dict[str, Any]:
        user32 = ctypes.windll.user32
        for item in self.windows.values():
            user32.PostMessageW(int(item["hwnd"]), WM_CLOSE, 0, 0)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(user32.IsWindow(int(item["hwnd"])) for item in self.windows.values()):
            time.sleep(0.05)
        if self.notepad and self.notepad.poll() is None and any(
            user32.IsWindow(int(item["hwnd"])) for label, item in self.windows.items() if label == "notepad"
        ):
            self.notepad.terminate()
        if self.owner_shell_marker:
            def marker_pids() -> list[int]:
                query = f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{self.owner_shell_marker}*' -and $_.ProcessId -ne $PID }} | Select-Object -ExpandProperty ProcessId"
                found = subprocess.run(["powershell.exe", "-NoProfile", "-Command", query], capture_output=True, text=True, timeout=10)
                return [int(value) for value in found.stdout.split() if value.isdigit()]
            for value in marker_pids():
                subprocess.run(["powershell.exe", "-NoProfile", "-Command", f"Stop-Process -Id {value} -Force"], capture_output=True, timeout=10)
            marker_deadline = time.monotonic() + 5
            remaining = marker_pids()
            while remaining and time.monotonic() < marker_deadline:
                time.sleep(0.05)
                remaining = marker_pids()
        else:
            remaining = []
        result = {label: not bool(user32.IsWindow(int(item["hwnd"]))) for label, item in self.windows.items()}
        result["terminal_shell_marker_clear"] = not remaining
        return result


class ForegroundMonitor:
    def __init__(self) -> None:
        self.expected = 0
        self.events: list[int] = []
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.thread_id = 0
        self.hook = 0
        self.hook_active = False
        self.callback = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise TimeoutError("foreground WinEvent monitor did not start")
        if not self.hook_active:
            raise RuntimeError("SetWinEventHook(EVENT_SYSTEM_FOREGROUND) failed")

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        callback_type = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, wintypes.DWORD, wintypes.HWND, wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
        @callback_type
        def callback(_hook, _event, hwnd, _object, _child, _thread, _time):
            with self.lock:
                if self.expected:
                    self.events.append(int(hwnd))
        self.callback = callback
        self.thread_id = kernel32.GetCurrentThreadId()
        user32.SetWinEventHook.restype = ctypes.c_void_p
        self.hook = user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, 0, callback, 0, 0, WINEVENT_OUTOFCONTEXT)
        self.hook_active = bool(self.hook)
        self.ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), 0, 0, 0) > 0:
            pass
        if self.hook:
            user32.UnhookWinEvent(self.hook)

    def arm(self, expected: int) -> None:
        with self.lock:
            self.expected = expected
            self.events.clear()

    def result(self) -> dict[str, int]:
        time.sleep(0.01)
        with self.lock:
            unexpected = sum(1 for hwnd in self.events if hwnd != self.expected)
            return {"transitions": len(self.events), "unexpected": unexpected}

    def disarm(self) -> None:
        with self.lock:
            self.expected = 0
            self.events.clear()

    def close(self) -> None:
        ctypes.windll.user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        self.thread.join(2)


def window_title(hwnd: int) -> str:
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def send_hotkey(virtual_key: int) -> int:
    keys = [(0x11, 0), (0x12, 0), (virtual_key, 0), (virtual_key, KEYEVENTF_KEYUP), (0x12, KEYEVENTF_KEYUP), (0x11, KEYEVENTF_KEYUP)]
    inputs = (INPUT * len(keys))()
    for index, (key, flags) in enumerate(keys):
        inputs[index].type = 1
        inputs[index].union.ki = KEYBDINPUT(key, 0, flags, 0, 0)
    return int(ctypes.windll.user32.SendInput(len(inputs), ctypes.byref(inputs), ctypes.sizeof(INPUT)))


def hotkey_is_free(virtual_key: int) -> bool:
    user32 = ctypes.windll.user32
    identifier = 0x6F00 + (virtual_key & 0xFF)
    ok = bool(user32.RegisterHotKey(None, identifier, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, virtual_key))
    if ok:
        user32.UnregisterHotKey(None, identifier)
    return ok


def uia_probe(hwnd: int) -> dict[str, Any]:
    if not hwnd:
        return {"found": False, "reason": "headless"}
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "uia_probe.ps1"),
            "-Hwnd",
            str(hwnd),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        return json.loads(completed.stdout.lstrip("\ufeff").strip())
    except json.JSONDecodeError:
        return {"found": False, "exit": completed.returncode, "stderr": completed.stderr[-500:]}


def capture_candidate_window(key: str, hwnd: int) -> dict[str, Any]:
    time.sleep(0.25)
    relative = Path(".omx") / "artifacts" / "windows-companion-shell-spike" / "screenshots" / f"{key}-recording.png"
    output = REPO / relative
    completed = subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(ROOT / "capture_window.ps1"), "-Hwnd", str(hwnd), "-OutputPath", str(output),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=CREATE_NO_WINDOW,
    )
    if completed.returncode:
        return {"captured": False, "relative_path": str(relative), "error": completed.stderr[-500:]}
    metadata = json.loads(completed.stdout.lstrip("\ufeff").strip())
    return {"captured": True, "relative_path": str(relative).replace("\\", "/"), "width": metadata["width"], "height": metadata["height"], "strict_hwnd_crop": True}


class Candidate:
    def __init__(self, key: str) -> None:
        self.key = key
        self.definition = CANDIDATES[key]
        self.process: subprocess.Popen[str] | None = None
        self.bridge: subprocess.Popen[str] | None = None
        self.channel: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr: list[str] = []
        self.ready: dict[str, Any] = {}
        self.job = create_kill_job()
        self.job_assignments = 0

    @property
    def pid(self) -> int:
        assert self.process is not None
        return self.process.pid

    @property
    def resource_pids(self) -> list[int]:
        return [process.pid for process in (self.process, self.bridge) if process is not None]

    def start(self, timeout: float = 20.0) -> dict[str, Any]:
        # Launch the base interpreter directly.  A uv/venv launcher otherwise
        # creates a shim+child tree that invalidates lifecycle/resource evidence.
        python = getattr(sys, "_base_executable", sys.executable)
        if self.key == "A":
            command = [python, str(ROOT / "candidate_tk.py"), "--vk", hex(self.definition["vk"])]
            self.process = self._popen(command)
            self.channel = self.process
        elif self.key == "C":
            command = [python, str(ROOT / "candidate_headless.py"), "--vk", hex(self.definition["vk"])]
            self.process = self._popen(command)
            self.channel = self.process
        else:
            pipe_name = "ttmc_spike_" + uuid.uuid4().hex
            native = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                str(ROOT / "candidate_native.ps1"), "-PipeName", pipe_name,
                "-VirtualKey", str(self.definition["vk"]),
            ]
            bridge = [str(ensure_native_bridge()), pipe_name]
            self.process = self._popen(native, no_window=True)
            self.bridge = self._popen(bridge, no_window=True)
            self.channel = self.bridge
        assert self.channel is not None
        threading.Thread(target=self._read_stdout, args=(self.channel,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(self.channel,), daemon=True).start()
        if self.process is not self.channel:
            threading.Thread(target=self._read_stderr, args=(self.process,), daemon=True).start()
        self.ready = self.wait_for("ready", timeout=timeout)
        return self.ready

    def _popen(self, command: list[str], no_window: bool = False) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW if no_window else 0,
        )
        if not ctypes.windll.kernel32.AssignProcessToJobObject(self.job, int(process._handle)):
            process.kill()
            raise OSError("AssignProcessToJobObject failed")
        self.job_assignments += 1
        return process

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            try:
                message = json.loads(line.lstrip("\ufeff"))
                if message.get("version") != PROTOCOL_VERSION:
                    self.stderr.append("rejected unsupported protocol version")
                    continue
                self.messages.put(message)
            except json.JSONDecodeError:
                self.stderr.append("non-json stdout: " + line.rstrip()[:500])

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self.stderr.append(line.rstrip()[:1000])

    def send(self, payload: dict[str, Any]) -> None:
        assert self.channel is not None and self.channel.stdin is not None
        if payload.get("version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
            raise ValueError("unsupported outgoing protocol version")
        self.channel.stdin.write(json.dumps({**payload, "version": PROTOCOL_VERSION}, ensure_ascii=False) + "\n")
        self.channel.stdin.flush()

    def wait_for(self, kind: str, seq: int | None = None, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        held: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{self.key} did not emit {kind} seq={seq}; stderr={self.stderr[-5:]}")
                try:
                    message = self.messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError(f"{self.key} did not emit {kind} seq={seq}; stderr={self.stderr[-5:]}") from exc
                if message.get("kind") == kind and (seq is None or message.get("seq") == seq):
                    return message
                held.append(message)
        finally:
            for message in held:
                self.messages.put(message)

    def state(self, state: str, seq: int) -> tuple[dict[str, Any], float]:
        sent_ns = time.perf_counter_ns()
        self.send({"kind": "state", "seq": seq, "state": state, "sent_ns": sent_ns})
        reply = self.wait_for("state_ack", seq=seq)
        return reply, (time.perf_counter_ns() - sent_ns) / 1_000_000.0

    def shutdown(self, timeout: float = 3.0) -> float:
        if self.process is None:
            return 0.0
        started = time.perf_counter()
        if self.process.poll() is None:
            stamp = time.perf_counter_ns()
            self.send({"kind": "shutdown", "sent_ns": stamp})
            self.wait_for("shutdown_ack", timeout=timeout)
        if self.channel and self.channel.stdin:
            self.channel.stdin.close()
        self.process.wait(timeout=timeout)
        if self.bridge:
            self.bridge.wait(timeout=timeout)
        return time.perf_counter() - started

    def kill(self) -> None:
        for process in (self.bridge, self.process):
            if process and process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        if self.job:
            ctypes.windll.kernel32.CloseHandle(self.job)
            self.job = 0


def functional_measurement(key: str, latency_events: int) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate = Candidate(key)
    owners = DisposableFocusOwners()
    monitor: ForegroundMonitor | None = None
    result: dict[str, Any] = {"candidate": CANDIDATES[key]["name"]}
    raw: dict[str, Any] = {}
    try:
        owners.start()
        monitor = ForegroundMonitor()
        if not owners.focus("notepad"):
            raise RuntimeError("could not settle disposable Notepad as foreground owner before candidate launch")
        launch_owner_preserved_before = owners.is_foreground("notepad")
        ready = candidate.start()
        launch_owner_preserved_after = owners.is_foreground("notepad")
        result["runtime"] = {key: value for key, value in ready.items() if key not in {"pid", "hwnd"}}
        latencies: list[float] = []
        focus_cycles: list[dict[str, Any]] = []
        state_replies: list[dict[str, Any]] = []
        for seq in range(100):
            owner = "notepad" if seq % 2 == 0 else "windows_terminal"
            settled = owners.focus(owner)
            expected = int(owners.windows[owner]["hwnd"])
            monitor.arm(expected)
            candidate.send({"kind": "cycle", "seq": seq})
            candidate.wait_for("cycle_ack", seq=seq)
            observed = monitor.result()
            preserved = owners.is_foreground(owner)
            monitor.disarm()
            focus_cycles.append({"seq": seq, "owner": owner, "settled": settled, "preserved": preserved, **observed})
        for seq in range(latency_events):
            state = REQUIRED_STATES[seq % len(REQUIRED_STATES)]
            reply, latency = candidate.state(state, seq)
            latencies.append(latency)
            if seq < len(REQUIRED_STATES):
                state_replies.append(reply)
        result["state_latency"] = {
            "count": len(latencies), "median_ms": statistics.median(latencies),
            "p95_ms": percentile(latencies, 0.95), "max_ms": max(latencies),
            "threshold_ms": 100, "pass": percentile(latencies, 0.95) <= 100,
        }
        result["focus_automated_same_owner"] = {
            "count": len(focus_cycles),
            "user_initiated_process_launch_owner_preserved_advisory": launch_owner_preserved_before and launch_owner_preserved_after,
            "settle_failures": sum(1 for item in focus_cycles if not item["settled"]),
            "focus_changes": sum(1 for item in focus_cycles if not item["preserved"]),
            "unexpected_foreground_transitions": sum(item["unexpected"] for item in focus_cycles),
            "owners": ["notepad", "windows_terminal"],
            "hook_active": monitor.hook_active,
            "pass": monitor.hook_active and all(item["settled"] and item["preserved"] and item["unexpected"] == 0 for item in focus_cycles),
            "hard_gate": "pass" if monitor.hook_active and all(item["settled"] and item["preserved"] and item["unexpected"] == 0 for item in focus_cycles) else "fail",
        }

        hotkeys: list[dict[str, Any]] = []
        send_counts: list[int] = []
        for seq in range(100):
            send_counts.append(send_hotkey(CANDIDATES[key]["vk"]))
            candidate.send({"kind": "state", "seq": 10000 + seq, "state": REQUIRED_STATES[seq % len(REQUIRED_STATES)], "sent_ns": time.perf_counter_ns()})
            hotkeys.append(candidate.wait_for("hotkey", timeout=2))
            candidate.wait_for("state_ack", seq=10000 + seq, timeout=2)
        sequences = [int(item["sequence"]) for item in hotkeys]
        result["global_hotkey"] = {
            "sent": 100,
            "sendinput_full_calls": sum(1 for count in send_counts if count == 6),
            "received": len(sequences),
            "duplicates": len(sequences) - len(set(sequences)),
            "sequence": sequences,
            "pass": len(sequences) == 100 and len(set(sequences)) == 100 and all(count == 6 for count in send_counts),
        }

        activation: list[dict[str, Any]] = []
        for seq in range(25):
            owner = "notepad" if seq % 2 == 0 else "windows_terminal"
            settled = owners.focus(owner)
            expected = int(owners.windows[owner]["hwnd"])
            monitor.arm(expected)
            candidate.send({"kind": "auxiliary", "seq": seq, "surface": "settings" if seq % 2 == 0 else "voice_preview", "trigger": "runtime"})
            ack = candidate.wait_for("auxiliary_ack", seq=seq)
            observed = monitor.result()
            monitor.disarm()
            activation.append({"seq": seq, "owner": owner, "settled": settled, "preserved": owners.is_foreground(owner), "opened": ack.get("opened"), "noactivate": ack.get("noactivate"), **observed})
        result["unsolicited_activation"] = {
            "count": 25, "settle_failures": sum(1 for item in activation if not item["settled"]),
            "focus_changes": sum(1 for item in activation if not item["preserved"]),
            "unexpected_foreground_transitions": sum(item["unexpected"] for item in activation),
            "actual_surface_opens": sum(1 for item in activation if item["opened"]),
            "pass": (key == "C") or all(item["settled"] and item["preserved"] and item["unexpected"] == 0 and item["opened"] and item["noactivate"] for item in activation),
        }

        accessibility: list[dict[str, Any]] = []
        for seq, state in enumerate(REQUIRED_STATES, start=latency_events + 1000):
            reply, _ = candidate.state(state, seq)
            title = window_title(int(candidate.ready.get("hwnd", 0))) if key != "C" else reply["display_text"]
            probe = uia_probe(int(candidate.ready.get("hwnd", 0))) if key != "C" else {"found": True, "root_name": reply["accessibility_name"], "channel": "terminal stdout"}
            state_visible = state.lower() in title.lower()
            cue_visible = str(reply.get("cue", "")) in title
            accessible_blob = json.dumps(probe, ensure_ascii=False).lower()
            state_accessible = state.lower() in accessible_blob
            accessibility.append({
                "state": state, "display_text": reply.get("display_text"), "cue": reply.get("cue"),
                "window_or_terminal_text": title, "uia": probe, "state_visible": state_visible,
                "cue_visible": cue_visible, "state_accessible": state_accessible,
            })
        automated_accessible = all(item["state_visible"] and item["cue_visible"] and item["state_accessible"] for item in accessibility)
        result["accessibility"] = {
            "states": len(accessibility), "automated_semantic_and_non_color": automated_accessible,
            "independent_assistive_technology_review": "not_run_advisory",
            "hard_gate": "pass" if automated_accessible else "fail",
        }
        if key in ("A", "B"):
            screenshot_seq = latency_events + 2000
            candidate.state("recording", screenshot_seq)
            result["screenshot"] = capture_candidate_window(key, int(candidate.ready["hwnd"]))
        raw.update({"latencies_ms": latencies, "focus_cycles": focus_cycles, "state_replies": state_replies, "activation_cycles": activation, "accessibility_states": accessibility})
        result["initial_close_seconds"] = candidate.shutdown(timeout=3)
        result["initial_close_pass"] = result["initial_close_seconds"] <= 2
    finally:
        candidate.kill()
        if monitor:
            monitor.close()
        result["focus_owner_cleanup"] = owners.close()
        result["focus_owner_cleanup_pass"] = all(result["focus_owner_cleanup"].values())
    return result, raw


def lifecycle_measurement(key: str, cycles: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    samples: list[dict[str, Any]] = []
    for cycle in range(cycles):
        candidate = Candidate(key)
        sample: dict[str, Any] = {"cycle": cycle}
        try:
            ready_at = time.perf_counter()
            candidate.start()
            sample["startup_seconds"] = time.perf_counter() - ready_at
            sample["close_seconds"] = candidate.shutdown(timeout=3)
            sample["process_exited"] = candidate.process is not None and candidate.process.poll() is not None
            sample["bridge_exited"] = candidate.bridge is None or candidate.bridge.poll() is not None
            sample["hotkey_free_after_exit"] = hotkey_is_free(CANDIDATES[key]["vk"])
        except Exception as exc:
            sample["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            candidate.kill()
        samples.append(sample)
    passed = all(
        sample.get("close_seconds", 999) <= 2
        and sample.get("process_exited")
        and sample.get("bridge_exited")
        and sample.get("hotkey_free_after_exit")
        and "error" not in sample
        for sample in samples
    )
    return {
        "cycles": cycles,
        "max_close_seconds": max(float(sample.get("close_seconds", 999)) for sample in samples),
        "orphans": sum(1 for sample in samples if not sample.get("process_exited") or not sample.get("bridge_exited")),
        "hotkey_leaks": sum(1 for sample in samples if not sample.get("hotkey_free_after_exit")),
        "pass": passed,
    }, samples


def process_sample(pid: int) -> tuple[float, int]:
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise OSError(f"OpenProcess failed for {pid}")
    try:
        creation = FILETIME()
        exit_time = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
            raise OSError("GetProcessTimes failed")
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        kernel_ticks = (kernel.high << 32) | kernel.low
        user_ticks = (user.high << 32) | user.low
        return (kernel_ticks + user_ticks) / 10_000_000.0, int(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def resource_measurement(seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = {key: Candidate(key) for key in CANDIDATES}
    raw: dict[str, list[dict[str, Any]]] = {key: [] for key in CANDIDATES}
    try:
        for candidate in candidates.values():
            candidate.start()
        logical = os.cpu_count() or 1
        def aggregate(candidate: Candidate) -> tuple[float, int]:
            samples = [process_sample(pid) for pid in candidate.resource_pids]
            return sum(item[0] for item in samples), sum(item[1] for item in samples)
        prior = {key: (*aggregate(candidate), time.perf_counter()) for key, candidate in candidates.items()}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))
            for key, candidate in candidates.items():
                cpu, working_set = aggregate(candidate)
                now = time.perf_counter()
                old_cpu, _old_ws, old_time = prior[key]
                cpu_percent = max(0.0, (cpu - old_cpu) / max(now - old_time, 0.001) / logical * 100.0)
                raw[key].append({"elapsed_seconds": seconds - max(0.0, deadline - time.monotonic()), "cpu_percent_total_machine": cpu_percent, "working_set_bytes": working_set})
                prior[key] = (cpu, working_set, now)
        summary: dict[str, Any] = {}
        for key, samples in raw.items():
            cpu_values = [item["cpu_percent_total_machine"] for item in samples]
            ws_values = [item["working_set_bytes"] for item in samples]
            median_cpu = statistics.median(cpu_values)
            median_ws = statistics.median(ws_values)
            summary[key] = {
                "duration_seconds": seconds,
                "samples": len(samples),
                "median_cpu_percent_total_machine": median_cpu,
                "median_working_set_mib": median_ws / (1024 * 1024),
                "absolute_working_set_used_as_conservative_ui_upper_bound": True,
                "aggregated_process_count": len(candidates[key].resource_pids),
                "cpu_pass": median_cpu < 1,
                "working_set_pass": median_ws < 150 * 1024 * 1024,
                "pass": median_cpu < 1 and median_ws < 150 * 1024 * 1024,
            }
        return summary, raw
    finally:
        for candidate in candidates.values():
            try:
                candidate.shutdown(timeout=3)
            except Exception:
                pass
            candidate.kill()


def environment_record() -> dict[str, Any]:
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": platform.platform(),
        "python": sys.version,
        "python_executable": r"%USERPROFILE%\talktomejohnny\.venv\Scripts\python.exe",
        "logical_processors": os.cpu_count(),
        "repository": r"%USERPROFILE%\talktomejohnny",
        "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
        "git_branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=REPO, text=True).strip(),
        "pyproject_sha256": __import__("hashlib").sha256((REPO / "pyproject.toml").read_bytes()).hexdigest(),
        "sources": [
            "https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-registerhotkey",
            "https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-unregisterhotkey",
            "https://learn.microsoft.com/dotnet/api/system.windows.window.showactivated",
            "https://learn.microsoft.com/dotnet/standard/io/how-to-use-named-pipes-for-network-interprocess-communication",
            "https://docs.python.org/3.12/library/tkinter.html",
        ],
    }


def git_venv_evidence(key: str) -> dict[str, Any]:
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=REPO, text=True).splitlines()
    pyproject_diff = subprocess.check_output(["git", "diff", "--", "pyproject.toml"], cwd=REPO, text=True)
    tracked = subprocess.run(["git", "ls-files", "--error-unmatch", "tools/spikes/windows_companion/run_spike.py"], cwd=REPO, capture_output=True).returncode == 0
    facts = {
        "shared_venv_python_exists": Path(sys.executable).is_file(),
        "candidate_runtime": "python-tk-win32" if key == "A" else ("powershell-dotnet-framework-wpf" if key == "B" else "python-headless"),
        "dependency_install_performed": False,
        "pyproject_dependency_diff_empty": not bool(pyproject_diff),
        "worktree_clean": not bool(status),
        "spike_runner_committed": tracked,
        "dirty_entry_count": len(status),
    }
    facts["pass"] = all([facts["shared_venv_python_exists"], facts["pyproject_dependency_diff_empty"], facts["worktree_clean"], facts["spike_runner_committed"]])
    facts["status"] = "pass" if facts["pass"] else "pending_clean_committed_checkout_rerun"
    return facts


def score_primary(key: str, summary: dict[str, Any]) -> dict[str, Any]:
    focus_accessibility = 30.0 if (
        summary["focus_automated_same_owner"]["pass"]
        and summary["accessibility"]["hard_gate"] == "pass"
        and summary["unsolicited_activation"]["pass"]
    ) else 0.0
    lifecycle_recovery = 20.0 if summary["lifecycle"]["pass"] and summary["initial_close_pass"] else 0.0
    # Predeclared surface rubric: one Python/Tk process and one language is the
    # smallest surface (20); the native two-process/two-language/versioned-IPC
    # candidate earns 12.  This intentionally counts B's real isolation cost.
    implementation_test_surface = 20.0 if key == "A" else 12.0
    startup_resource = 15.0 if summary["state_latency"]["pass"] and summary["resource"]["pass"] else 0.0
    # Both install without dependencies.  A uses the supported Python/Tk runtime
    # only (15); B additionally depends on Windows PowerShell/.NET Framework and
    # ships an IPC boundary (12).
    packaging = 15.0 if key == "A" else 12.0
    total = focus_accessibility + lifecycle_recovery + implementation_test_surface + startup_resource + packaging
    return {
        "focus_accessibility_30": focus_accessibility,
        "lifecycle_recovery_20": lifecycle_recovery,
        "implementation_test_surface_20": implementation_test_surface,
        "startup_resource_15": startup_resource,
        "packaging_15": packaging,
        "total_100": total,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--resource-seconds", type=int, default=300, help="Use 300 for the binding G2 run; shorter values are debug-only.")
    parser.add_argument("--latency-events", type=int, default=250)
    parser.add_argument("--lifecycle-cycles", type=int, default=25)
    parser.add_argument("--output", type=Path, default=ARTIFACT_ROOT / "automated-measurements.json")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--supervisor-probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.worker:
        worker_command = [getattr(sys, "_base_executable", sys.executable), str(Path(__file__).resolve()), *sys.argv[1:], "--worker"]
        timeout_seconds = max(600, args.resource_seconds + 300)
        process = subprocess.Popen(worker_command, cwd=REPO)
        try:
            return process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            print(f"spike supervisor timeout after {timeout_seconds}s; worker terminated and Job Object handles closed", file=sys.stderr)
            return 124
    if args.supervisor_probe:
        return 0
    if args.latency_events < 200:
        raise SystemExit("G2 requires at least 200 latency events")
    if args.lifecycle_cycles < 25:
        raise SystemExit("G2 requires at least 25 lifecycle cycles")
    if args.resource_seconds <= 0:
        raise SystemExit("resource duration must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    evidence: dict[str, Any] = {"schema_version": 1, "environment": environment_record(), "candidates": {}, "raw": {}}
    for key in CANDIDATES:
        print(f"functional {key}", flush=True)
        summary, raw = functional_measurement(key, args.latency_events)
        print(f"lifecycle {key}", flush=True)
        lifecycle, lifecycle_raw = lifecycle_measurement(key, args.lifecycle_cycles)
        summary["lifecycle"] = lifecycle
        summary["supported_git_venv_start"] = git_venv_evidence(key)
        evidence["candidates"][key] = summary
        evidence["raw"][key] = {**raw, "lifecycle": lifecycle_raw}
    print(f"resource sample {args.resource_seconds}s (A/B/C concurrent)", flush=True)
    resources, resource_raw = resource_measurement(args.resource_seconds)
    evidence["resource"] = resources
    evidence["raw"]["resource"] = resource_raw
    for key in CANDIDATES:
        evidence["candidates"][key]["resource"] = resources[key]
    scores = {key: score_primary(key, evidence["candidates"][key]) for key in ("A", "B")}
    evidence["scores"] = scores
    hard_pass = {
        key: all([
            evidence["candidates"][key]["focus_automated_same_owner"]["pass"],
            evidence["candidates"][key]["global_hotkey"]["pass"],
            evidence["candidates"][key]["state_latency"]["pass"],
            evidence["candidates"][key]["initial_close_pass"],
            evidence["candidates"][key]["lifecycle"]["pass"],
            evidence["candidates"][key]["resource"]["pass"],
            evidence["candidates"][key]["accessibility"]["hard_gate"] == "pass",
            evidence["candidates"][key]["unsolicited_activation"]["pass"],
            evidence["candidates"][key]["supported_git_venv_start"]["pass"],
            evidence["candidates"][key]["focus_owner_cleanup_pass"],
        ])
        for key in ("A", "B")
    }
    eligible = [key for key in ("A", "B") if hard_pass[key]]
    selected = max(eligible, key=lambda item: scores[item]["total_100"]) if eligible else None
    evidence["selection_status"] = {
        "selected": selected,
        "status": "selected_pending_independent_verifier" if selected else "blocked_no_primary_passed",
        "hard_gate_pass": hard_pass,
        "headless_retained": True,
        "operator_status": "automated operator-machine probes executed",
        "independent_verifier_status": "pending",
        "reason": "Highest weighted score among hard-gate passers; tie within three points would select the smaller surface.",
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
