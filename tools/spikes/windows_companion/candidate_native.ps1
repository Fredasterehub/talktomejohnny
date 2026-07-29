param(
    [Parameter(Mandatory = $true)][string]$PipeName,
    [int]$VirtualKey = 0x86
)

$ErrorActionPreference = 'Stop'

$source = @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;

public sealed class WindowsCompanionSpike
{
    private const int ProtocolVersion = 1;
    private const int WmHotkey = 0x0312;
    private const int HotkeyId = 1;
    private const uint ModAlt = 0x0001;
    private const uint ModControl = 0x0002;
    private const uint ModNoRepeat = 0x4000;
    private const int GwlExstyle = -20;
    private const long WsExToolwindow = 0x00000080L;
    private const long WsExAppwindow = 0x00040000L;
    private const long WsExNoactivate = 0x08000000L;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint virtualKey);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW", SetLastError = true)]
    private static extern IntPtr GetWindowLongPtr64(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW", SetLastError = true)]
    private static extern IntPtr SetWindowLongPtr64(IntPtr hwnd, int index, IntPtr value);

    private readonly string pipeName;
    private readonly uint virtualKey;
    private readonly object writeLock = new object();
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private readonly ManualResetEventSlim windowReady = new ManualResetEventSlim(false);
    private Application application;
    private Window window;
    private TextBlock stateText;
    private TextBlock detailText;
    private HwndSource source;
    private IntPtr hwnd;
    private StreamWriter writer;
    private NamedPipeServerStream pipe;
    private Window auxiliaryWindow;
    private int hotkeySequence;

    private static readonly Dictionary<string, string> Cues = new Dictionary<string, string>(StringComparer.Ordinal) {
        { "idle", "[.]" }, { "recording", "[~]" }, { "transcribing", "[>]" },
        { "awaiting confirmation", "[?]" }, { "delivering", "[>>]" },
        { "waiting for assistant", "[...]" }, { "planning", "[::]" },
        { "speaking", "[))]" }, { "paused", "[||]" }, { "stopping", "[x]" },
        { "disconnected", "[//]" }, { "recoverable error", "[!]" }
    };

    public WindowsCompanionSpike(string pipeName, int virtualKey)
    {
        this.pipeName = pipeName;
        this.virtualKey = unchecked((uint)virtualKey);
    }

    public int Run()
    {
        RenderOptions.ProcessRenderMode = RenderMode.SoftwareOnly;
        Thread pipeThread = new Thread(PipeLoop);
        pipeThread.IsBackground = true;
        pipeThread.Name = "spike-named-pipe";
        pipeThread.Start();

        application = new Application();
        application.ShutdownMode = ShutdownMode.OnExplicitShutdown;
        window = BuildWindow();
        window.SourceInitialized += OnSourceInitialized;
        window.Closed += OnClosed;
        window.Show();
        application.Run();
        return 0;
    }

    private Window BuildWindow()
    {
        Window result = new Window {
            Title = "TalkToMeJohnny Native Spike - [.] Idle",
            Width = 500,
            Height = 152,
            Left = 408,
            Top = 24,
            ResizeMode = ResizeMode.NoResize,
            WindowStyle = WindowStyle.ToolWindow,
            ShowActivated = false,
            ShowInTaskbar = false,
            Topmost = true,
            Focusable = false,
            Background = new SolidColorBrush(Color.FromRgb(21, 25, 34))
        };
        AutomationProperties.SetName(result, "TalkToMeJohnny idle");
        StackPanel panel = new StackPanel { Margin = new Thickness(14, 10, 14, 8) };
        stateText = new TextBlock {
            Text = "[.] Idle",
            FontFamily = new FontFamily("Segoe UI"),
            FontSize = 21,
            FontWeight = FontWeights.SemiBold,
            Foreground = Brushes.White
        };
        AutomationProperties.SetName(stateText, "TalkToMeJohnny state idle");
        detailText = new TextBlock {
            Text = "Ready; named-pipe core connected on demand",
            FontFamily = new FontFamily("Segoe UI"),
            FontSize = 12,
            Foreground = new SolidColorBrush(Color.FromRgb(199, 206, 222)),
            Margin = new Thickness(0, 8, 0, 0),
            TextWrapping = TextWrapping.Wrap,
            MaxWidth = 460
        };
        AutomationProperties.SetName(detailText, "TalkToMeJohnny state detail");
        panel.Children.Add(stateText);
        panel.Children.Add(detailText);
        result.Content = panel;
        return result;
    }

    private void OnSourceInitialized(object sender, EventArgs args)
    {
        hwnd = new WindowInteropHelper(window).Handle;
        long style = GetWindowLongPtr64(hwnd, GwlExstyle).ToInt64();
        style = (style | WsExToolwindow | WsExNoactivate) & ~WsExAppwindow;
        SetWindowLongPtr64(hwnd, GwlExstyle, new IntPtr(style));
        source = HwndSource.FromHwnd(hwnd);
        source.AddHook(WndProc);
        if (!RegisterHotKey(hwnd, HotkeyId, ModControl | ModAlt | ModNoRepeat, virtualKey))
            throw new InvalidOperationException("RegisterHotKey failed: " + Marshal.GetLastWin32Error());
        windowReady.Set();
    }

    private IntPtr WndProc(IntPtr handle, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (message == WmHotkey && wParam.ToInt32() == HotkeyId) {
            int sequence = Interlocked.Increment(ref hotkeySequence);
            Emit(new Dictionary<string, object> {
                { "version", ProtocolVersion }, { "kind", "hotkey" },
                { "sequence", sequence }, { "received_ns", MonotonicNanoseconds() }
            });
            handled = true;
        }
        return IntPtr.Zero;
    }

    private void OnClosed(object sender, EventArgs args)
    {
        if (hwnd != IntPtr.Zero) UnregisterHotKey(hwnd, HotkeyId);
        if (source != null) source.RemoveHook(WndProc);
        try { if (pipe != null) pipe.Dispose(); } catch { }
    }

    private void PipeLoop()
    {
        PipeSecurity security = new PipeSecurity();
        SecurityIdentifier sid = WindowsIdentity.GetCurrent().User;
        security.SetOwner(sid);
        security.AddAccessRule(new PipeAccessRule(sid, PipeAccessRights.FullControl, AccessControlType.Allow));
        pipe = new NamedPipeServerStream(
            pipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous, 4096, 4096, security, HandleInheritability.None);
        IAsyncResult pendingConnection = pipe.BeginWaitForConnection(null, null);
        if (!pendingConnection.AsyncWaitHandle.WaitOne(TimeSpan.FromSeconds(15))) {
            pipe.Dispose();
            return;
        }
        pipe.EndWaitForConnection(pendingConnection);
        StreamReader reader = new StreamReader(pipe, new UTF8Encoding(false), false, 4096, true);
        writer = new StreamWriter(pipe, new UTF8Encoding(false), 4096, true) { AutoFlush = true };
        windowReady.Wait(TimeSpan.FromSeconds(5));
        Emit(new Dictionary<string, object> {
            { "version", ProtocolVersion }, { "kind", "ready" }, { "candidate", "B-wpf-named-pipe" },
            { "pid", Process.GetCurrentProcess().Id }, { "hwnd", hwnd.ToInt64() },
            { "runtime", ".NET Framework " + Environment.Version },
            { "transport", "named-pipe-ndjson-v1-current-user-acl" }, { "virtual_key", virtualKey }
        });
        string line;
        while ((line = reader.ReadLine()) != null) {
            Dictionary<string, object> message;
            try {
                message = json.Deserialize<Dictionary<string, object>>(line);
                if (Convert.ToInt32(message["version"]) != ProtocolVersion)
                    throw new InvalidDataException("unsupported protocol version");
            } catch (Exception exception) {
                EmitError(exception.GetType().Name);
                continue;
            }
            HandleMessage(message);
        }
    }

    private void HandleMessage(Dictionary<string, object> message)
    {
        string kind = Convert.ToString(message["kind"]);
        if (kind == "state") {
            string state = Convert.ToString(message["state"]);
            string cue = Cues.ContainsKey(state) ? Cues[state] : "[?]";
            string display = cue + " " + state;
            window.Dispatcher.Invoke(new Action(delegate { ApplyState(state, display); }));
            Emit(new Dictionary<string, object> {
                { "version", ProtocolVersion }, { "kind", "state_ack" }, { "seq", message["seq"] },
                { "state", state }, { "sent_ns", message["sent_ns"] },
                { "applied_ns", MonotonicNanoseconds() }, { "display_text", display },
                { "cue", cue }, { "accessibility_name", "TalkToMeJohnny " + state }
            });
        } else if (kind == "cycle") {
            window.Dispatcher.Invoke(new Action(delegate {
                foreach (string state in new [] { "recording", "idle", "planning", "speaking" }) {
                    string cue = Cues[state]; ApplyState(state, cue + " " + state);
                }
            }));
            Emit(new Dictionary<string, object> {
                { "version", ProtocolVersion }, { "kind", "cycle_ack" }, { "seq", message["seq"] },
                { "phases", new [] { "start", "stop", "state", "reply" } },
                { "applied_ns", MonotonicNanoseconds() }
            });
        } else if (kind == "auxiliary") {
            string surface = Convert.ToString(message["surface"]);
            window.Dispatcher.Invoke(new Action(delegate { ShowAuxiliary(surface); }));
            Emit(new Dictionary<string, object> {
                { "version", ProtocolVersion }, { "kind", "auxiliary_ack" }, { "seq", message["seq"] },
                { "surface", surface }, { "opened", true }, { "noactivate", true }, { "applied_ns", MonotonicNanoseconds() }
            });
        } else if (kind == "shutdown") {
            Emit(new Dictionary<string, object> {
                { "version", ProtocolVersion }, { "kind", "shutdown_ack" },
                { "requested_ns", message["sent_ns"] }, { "applied_ns", MonotonicNanoseconds() }
            });
            window.Dispatcher.BeginInvoke(new Action(delegate {
                window.Close();
                application.Shutdown(0);
            }));
        } else {
            EmitError("unsupported_kind");
        }
    }

    private void ApplyState(string state, string display)
    {
        stateText.Text = display;
        detailText.Text = "State supplied by versioned named-pipe core probe";
        window.Title = "TalkToMeJohnny Native Spike - " + display;
        AutomationProperties.SetName(window, "TalkToMeJohnny " + state);
        AutomationProperties.SetName(stateText, "TalkToMeJohnny state " + state);
    }

    private void ShowAuxiliary(string surface)
    {
        if (auxiliaryWindow != null) auxiliaryWindow.Close();
        TextBlock text = new TextBlock { Text = surface.Replace('_', ' ') + " ready", FontSize = 18, Margin = new Thickness(16) };
        AutomationProperties.SetName(text, "TalkToMeJohnny " + surface.Replace('_', ' '));
        auxiliaryWindow = new Window {
            Title = "TalkToMeJohnny " + surface.Replace('_', ' '), Width = 320, Height = 100,
            ShowActivated = false, ShowInTaskbar = false, WindowStyle = WindowStyle.ToolWindow,
            Topmost = true, Content = text, Focusable = false
        };
        auxiliaryWindow.Show();
    }

    private void EmitError(string error)
    {
        Emit(new Dictionary<string, object> {
            { "version", ProtocolVersion }, { "kind", "protocol_error" }, { "error", error }
        });
    }

    private void Emit(Dictionary<string, object> message)
    {
        lock (writeLock) {
            if (writer == null) return;
            writer.WriteLine(json.Serialize(message));
            writer.Flush();
        }
    }

    private static long MonotonicNanoseconds()
    {
        return (long)((double)Stopwatch.GetTimestamp() * 1000000000.0 / Stopwatch.Frequency);
    }
}
'@

Add-Type -TypeDefinition $source -Language CSharp -ReferencedAssemblies @(
    'System',
    'System.Core',
    'System.Web.Extensions',
    'System.Xaml',
    'System.Security',
    'WindowsBase',
    'PresentationCore',
    'PresentationFramework',
    'UIAutomationTypes'
)

$runner = [WindowsCompanionSpike]::new($PipeName, $VirtualKey)
exit $runner.Run()
