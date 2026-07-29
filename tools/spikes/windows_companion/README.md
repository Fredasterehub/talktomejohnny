# Windows companion shell spike

This directory is an isolated G2 capability probe. It is not production source.

Candidates:

- `candidate_tk.py`: Python 3.12/Tk 8.6 plus Win32 no-activate and global-hotkey adapters.
- `candidate_native.ps1`: a thin WPF process built with in-box Windows PowerShell/.NET Framework and a current-user-ACL, version-1 named-pipe NDJSON boundary.
- `candidate_headless.py`: the mandatory terminal/headless recovery surface.

Run the binding automated sample from the supported shared venv:

```powershell
& "$env:USERPROFILE\talktomejohnny\.venv\Scripts\python.exe" `
  .\tools\spikes\windows_companion\run_spike.py `
  --resource-seconds 300 --latency-events 250 --lifecycle-cycles 25
```

Shorter resource durations are permitted only for harness debugging. The runner
refuses latency or lifecycle sample sizes below the approved G2 minimums.

The runner automates the binding 100-cycle Notepad/Windows Terminal foreground-owner
alternation and continuously observes foreground transitions with a WinEvent hook.
It also validates semantic, non-color state metadata through UI Automation, but it
does not claim a hands-on assistive-technology usability review. Formal selection
still requires the independent verifier and a clean committed-checkout run.

Protocol messages are UTF-8 NDJSON with `version: 1`, a `kind`, and monotonic timing
fields. Candidate B restricts its local pipe to the current Windows identity. Its
server connection wait is bounded at 15 seconds; the per-connection reader is a
background spike surface and is not a production transport implementation.
