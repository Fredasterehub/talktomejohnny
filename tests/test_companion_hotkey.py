from __future__ import annotations

import queue
import threading
import unittest

from talktomeclaude.companion.hotkey import (
    ReconfigurableHotkeyListener,
    ThreadHotkeyListener,
    normalize_control_hotkey,
    parse_control_hotkey,
)
from talktomeclaude.platform.windows.hotkeys import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    WM_HOTKEY,
)


class _Pump:
    def __init__(self) -> None:
        self.messages: queue.Queue[tuple[int, int] | None] = queue.Queue()
        self.prepared = False
        self.quit_thread: int | None = None

    def prepare(self) -> int:
        self.prepared = True
        return 77

    def next_message(self) -> tuple[int, int] | None:
        return self.messages.get(timeout=1)

    def post_quit(self, thread_id: int) -> bool:
        self.quit_thread = thread_id
        self.messages.put(None)
        return True


class _Facade:
    def __init__(self) -> None:
        self.registered: list[tuple[int, int, int, int]] = []
        self.unregistered: list[tuple[int, int]] = []

    def register_hotkey(
        self, hwnd: int, hotkey_id: int, modifiers: int, vk: int
    ) -> bool:
        self.registered.append((hwnd, hotkey_id, modifiers, vk))
        return True

    def unregister_hotkey(self, hwnd: int, hotkey_id: int) -> bool:
        self.unregistered.append((hwnd, hotkey_id))
        return True


class _KeyState:
    def __init__(self, values: list[bool], *, fallback: bool = False) -> None:
        self.values = values
        self.fallback = fallback
        self.calls = 0

    def is_pressed(self, _virtual_key: int) -> bool:
        self.calls += 1
        if self.values:
            return self.values.pop(0)
        return self.fallback


class _LifecycleListener:
    def __init__(
        self,
        _callback: object,
        *,
        release_callback: object,
        modifiers: int,
        virtual_key: int,
        events: list[tuple[str, int]],
        fail_start: bool = False,
        stop_result: bool = True,
    ) -> None:
        self.binding = (modifiers, virtual_key)
        self.events = events
        self.fail_start = fail_start
        self.stop_result = stop_result

    def start(self) -> None:
        self.events.append(("start", self.binding[1]))
        if self.fail_start:
            raise RuntimeError("registration failed")

    def stop(self) -> bool:
        self.events.append(("stop", self.binding[1]))
        return self.stop_result


class ControlHotkeyBindingTests(unittest.TestCase):
    def test_printable_oem_keys_include_their_physical_shift_modifier(self) -> None:
        self.assertEqual(parse_control_hotkey("~"), (MOD_SHIFT, 0xC0))
        self.assertEqual(parse_control_hotkey("`"), (0, 0xC0))
        self.assertEqual(parse_control_hotkey("?"), (MOD_SHIFT, 0xBF))
        self.assertEqual(
            parse_control_hotkey("ctrl+?"),
            (MOD_CONTROL | MOD_SHIFT, 0xBF),
        )
        self.assertEqual(parse_control_hotkey("!"), (MOD_SHIFT, ord("1")))

    def test_normalizes_aliases_but_preserves_readable_single_keys(self) -> None:
        self.assertEqual(
            normalize_control_hotkey(" Control + ALT + spacebar "),
            "ctrl+alt+space",
        )
        self.assertEqual(normalize_control_hotkey("shift+?"), "?")
        self.assertEqual(normalize_control_hotkey("tilde"), "~")
        self.assertEqual(normalize_control_hotkey("F12"), "f12")

    def test_rejects_empty_modifier_only_and_multiple_primary_keys(self) -> None:
        for binding in ("", "ctrl+alt", "a+b", "ctrl++"):
            with self.subTest(binding=binding):
                with self.assertRaises(ValueError):
                    parse_control_hotkey(binding)

    def test_live_rebind_starts_candidate_before_releasing_previous_key(self) -> None:
        events: list[tuple[str, int]] = []
        created: list[_LifecycleListener] = []

        def factory(callback: object, **options: object) -> _LifecycleListener:
            listener = _LifecycleListener(
                callback,
                release_callback=options["release_callback"],
                modifiers=int(options["modifiers"]),
                virtual_key=int(options["virtual_key"]),
                events=events,
            )
            created.append(listener)
            return listener

        listener = ReconfigurableHotkeyListener(
            lambda: None,
            release_callback=lambda: None,
            modifiers=MOD_CONTROL | MOD_ALT,
            virtual_key=0x20,
            listener_factory=factory,
        )
        listener.start()
        listener.rebind(MOD_SHIFT, 0xC0)

        self.assertEqual(
            events,
            [("start", 0x20), ("start", 0xC0), ("stop", 0x20)],
        )
        self.assertEqual(listener.binding, (MOD_SHIFT, 0xC0))
        self.assertEqual(len(created), 2)
        self.assertTrue(listener.stop())
        self.assertEqual(events[-1], ("stop", 0xC0))

    def test_failed_live_rebind_keeps_the_previous_key_registered(self) -> None:
        events: list[tuple[str, int]] = []

        def factory(callback: object, **options: object) -> _LifecycleListener:
            virtual_key = int(options["virtual_key"])
            return _LifecycleListener(
                callback,
                release_callback=options["release_callback"],
                modifiers=int(options["modifiers"]),
                virtual_key=virtual_key,
                events=events,
                fail_start=virtual_key == 0xBF,
            )

        listener = ReconfigurableHotkeyListener(
            lambda: None,
            release_callback=lambda: None,
            modifiers=MOD_CONTROL | MOD_ALT,
            virtual_key=0x20,
            listener_factory=factory,
        )
        listener.start()

        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            listener.rebind(MOD_SHIFT, 0xBF)

        self.assertEqual(listener.binding, (MOD_CONTROL | MOD_ALT, 0x20))
        self.assertNotIn(("stop", 0x20), events)
        self.assertTrue(listener.stop())

class ThreadHotkeyListenerTests(unittest.TestCase):
    def test_registers_dispatches_off_pump_thread_and_unregisters(self) -> None:
        pump = _Pump()
        facade = _Facade()
        called = threading.Event()
        callback_threads: list[int] = []

        def callback() -> None:
            callback_threads.append(threading.get_ident())
            called.set()

        listener = ThreadHotkeyListener(
            callback,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
        )
        listener.start()
        pump.messages.put((WM_HOTKEY, 1))

        self.assertTrue(called.wait(1))
        self.assertTrue(listener.stop())
        self.assertEqual(pump.quit_thread, 77)
        self.assertEqual(facade.unregistered, [(0, 1)])
        self.assertTrue(facade.registered[0][2] & MOD_NOREPEAT)
        self.assertNotEqual(callback_threads[0], listener._owner.ident)

    def test_start_and_stop_are_idempotent(self) -> None:
        pump = _Pump()
        facade = _Facade()
        listener = ThreadHotkeyListener(
            lambda: None,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
        )
        listener.start()
        listener.start()
        self.assertTrue(listener.stop())
        self.assertTrue(listener.stop())
        self.assertEqual(len(facade.registered), 1)

    def test_registration_failure_is_reported(self) -> None:
        pump = _Pump()

        class Failing(_Facade):
            def register_hotkey(
                self, hwnd: int, hotkey_id: int, modifiers: int, vk: int
            ) -> bool:
                return False

        listener = ThreadHotkeyListener(
            lambda: None,
            pump_factory=lambda: pump,
            facade_factory=Failing,
        )
        with self.assertRaisesRegex(RuntimeError, "registration failed"):
            listener.start()

    def test_hold_activation_dispatches_release_after_key_up(self) -> None:
        pump = _Pump()
        facade = _Facade()
        key_state = _KeyState([True, True, False])
        pressed = threading.Event()
        released = threading.Event()

        def activate() -> bool:
            pressed.set()
            return True

        listener = ThreadHotkeyListener(
            activate,
            release_callback=released.set,
            release_poll_seconds=0.001,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
            key_state_factory=lambda: key_state,
        )
        listener.start()
        pump.messages.put((WM_HOTKEY, 1))

        self.assertTrue(pressed.wait(1))
        self.assertTrue(released.wait(1))
        self.assertGreaterEqual(key_state.calls, 3)
        self.assertTrue(listener.stop())

    def test_toggle_activation_never_polls_or_dispatches_release(self) -> None:
        pump = _Pump()
        facade = _Facade()
        released = threading.Event()
        key_state_created = False

        def key_state_factory() -> _KeyState:
            nonlocal key_state_created
            key_state_created = True
            return _KeyState([])

        listener = ThreadHotkeyListener(
            lambda: False,
            release_callback=released.set,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
            key_state_factory=key_state_factory,
        )
        listener.start()
        pump.messages.put((WM_HOTKEY, 1))
        self.assertTrue(listener.stop())

        self.assertFalse(released.is_set())
        self.assertFalse(key_state_created)

    def test_stop_while_hold_key_is_down_suppresses_late_release(self) -> None:
        pump = _Pump()
        facade = _Facade()
        pressed = threading.Event()
        released = threading.Event()
        listener = ThreadHotkeyListener(
            lambda: pressed.set() or True,
            release_callback=released.set,
            release_poll_seconds=0.001,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
            key_state_factory=lambda: _KeyState([], fallback=True),
        )
        listener.start()
        pump.messages.put((WM_HOTKEY, 1))

        self.assertTrue(pressed.wait(1))
        self.assertTrue(listener.stop())
        self.assertFalse(released.is_set())

    def test_saturated_hold_queue_still_stops_both_owned_threads(self) -> None:
        pump = _Pump()
        facade = _Facade()
        pressed = threading.Event()
        listener = ThreadHotkeyListener(
            lambda: pressed.set() or True,
            release_callback=lambda: None,
            release_poll_seconds=0.001,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
            key_state_factory=lambda: _KeyState([], fallback=True),
        )
        listener.start()
        for _ in range(32):
            pump.messages.put((WM_HOTKEY, 1))
        self.assertTrue(pressed.wait(1))

        self.assertTrue(listener.stop())
        self.assertFalse(listener._owner.is_alive())
        self.assertFalse(listener._dispatcher.is_alive())

    def test_blocked_user_callback_makes_shutdown_failure_explicit(self) -> None:
        pump = _Pump()
        facade = _Facade()
        entered = threading.Event()
        release = threading.Event()

        def callback() -> None:
            entered.set()
            release.wait(1)

        listener = ThreadHotkeyListener(
            callback,
            shutdown_deadline_seconds=0.02,
            pump_factory=lambda: pump,
            facade_factory=lambda: facade,
        )
        listener.start()
        pump.messages.put((WM_HOTKEY, 1))
        self.assertTrue(entered.wait(1))

        self.assertFalse(listener.stop())
        release.set()
        listener._dispatcher.join(1)
        self.assertFalse(listener._dispatcher.is_alive())


if __name__ == "__main__":
    unittest.main()
