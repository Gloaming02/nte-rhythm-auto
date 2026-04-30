"""按键输出：前台 pynput / 后台 Win32 PostMessage 或 SendMessage（消息进目标 HWND，不占全局键盘焦点）。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

import win32api
import win32con
import win32gui

logger = logging.getLogger(__name__)

_LANES: tuple[str, str, str, str] = ("d", "f", "j", "k")
_VK: dict[str, int] = {"d": 0x44, "f": 0x46, "j": 0x4A, "k": 0x4B}
_VK_ESCAPE = 0x1B


def _lparam_keydown(vk: int) -> int:
    scan = win32api.MapVirtualKey(vk, 0) & 0xFF
    return 1 | (scan << 16)


def _lparam_keyup(vk: int) -> int:
    scan = win32api.MapVirtualKey(vk, 0) & 0xFF
    return 1 | (scan << 16) | (1 << 30) | (1 << 31)


class KeySender:
    def __init__(self, cfg: dict[str, Any], hwnd: int | None) -> None:
        keys_cfg = cfg.get("keys") or {}
        self._lanes: list[str] = list(_LANES)
        self._mode = str(keys_cfg.get("mode", "foreground")).lower()
        self._hold = float(keys_cfg.get("key_hold_sec", 0.02))
        self._delay = max(0.0, float(keys_cfg.get("press_delay_sec", 0.0)))
        raw_delay_by = keys_cfg.get("press_delay_sec_by_lane")
        if isinstance(raw_delay_by, list) and len(raw_delay_by) == 4:
            self._delay_by_lane = [max(0.0, float(v)) if v is not None else self._delay for v in raw_delay_by]
        else:
            self._delay_by_lane = [self._delay, self._delay, self._delay, self._delay]
        self._hwnd = hwnd
        self._win32_dispatch = str(keys_cfg.get("win32_dispatch", "post")).lower()
        self._fake_activate = bool(keys_cfg.get("fake_activate", True))
        self._kb: Any = None
        self._kb_lock = threading.Lock()

    def maybe_fake_activate(self) -> None:
        if self._mode != "background" or not self._hwnd or not self._fake_activate:
            return
        try:
            win32gui.SendMessage(self._hwnd, win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0)
        except Exception as e:
            logger.debug("fake_activate: %s", e)

    def _ensure_kb(self) -> Any:
        if self._kb is not None:
            return self._kb
        with self._kb_lock:
            if self._kb is not None:
                return self._kb
            from pynput.keyboard import Controller
            self._kb = Controller()
            return self._kb

    def lane_key_name(self, lane_index: int) -> str:
        return self._lanes[lane_index]

    def send_keydown(self, lane_index: int) -> None:
        key = self._lanes[lane_index]
        vk = _VK[key]
        if self._mode == "background":
            if not self._hwnd:
                logger.error("后台模式需要有效 hwnd")
                return
            l_down = _lparam_keydown(vk)
            try:
                if self._win32_dispatch == "send":
                    win32gui.SendMessage(self._hwnd, win32con.WM_KEYDOWN, vk, l_down)
                else:
                    win32gui.PostMessage(self._hwnd, win32con.WM_KEYDOWN, vk, l_down)
            except Exception as e:
                logger.error("后台 KEYDOWN 失败: %s", e)
            return
        try:
            self._ensure_kb().press(key)
        except Exception as e:
            logger.error("pynput KEYDOWN 失败: %s", e)

    def send_keyup(self, lane_index: int) -> None:
        key = self._lanes[lane_index]
        vk = _VK[key]
        if self._mode == "background":
            if not self._hwnd:
                return
            l_up = _lparam_keyup(vk)
            try:
                if self._win32_dispatch == "send":
                    win32gui.SendMessage(self._hwnd, win32con.WM_KEYUP, vk, l_up)
                else:
                    win32gui.PostMessage(self._hwnd, win32con.WM_KEYUP, vk, l_up)
            except Exception as e:
                logger.error("后台 KEYUP 失败: %s", e)
            return
        try:
            self._ensure_kb().release(key)
        except Exception as e:
            logger.error("pynput KEYUP 失败: %s", e)

    def press_lane(self, lane_index: int) -> None:
        key = self._lanes[lane_index]
        vk = _VK[key]
        if self._delay > 0:
            time.sleep(self._delay)
        self.send_keydown(lane_index)
        time.sleep(self._hold)
        self.send_keyup(lane_index)
        logger.debug(
            "按键 lane=%d key=%s vk=0x%02X hold=%.3fs mode=%s",
            lane_index,
            key,
            vk,
            self._hold,
            self._mode,
        )

    def send_escape(self) -> None:
        vk = _VK_ESCAPE
        if self._mode == "background":
            if not self._hwnd:
                logger.error("后台模式需要有效 hwnd")
                return
            l_down = _lparam_keydown(vk)
            l_up = _lparam_keyup(vk)
            try:
                if self._win32_dispatch == "send":
                    win32gui.SendMessage(self._hwnd, win32con.WM_KEYDOWN, vk, l_down)
                    time.sleep(self._hold)
                    win32gui.SendMessage(self._hwnd, win32con.WM_KEYUP, vk, l_up)
                else:
                    win32gui.PostMessage(self._hwnd, win32con.WM_KEYDOWN, vk, l_down)
                    time.sleep(self._hold)
                    win32gui.PostMessage(self._hwnd, win32con.WM_KEYUP, vk, l_up)
            except Exception as e:
                logger.error("后台 ESC 失败: %s", e)
            return
        try:
            kb = self._ensure_kb()
            from pynput.keyboard import Key
            kb.press(Key.esc)
            time.sleep(self._hold)
            kb.release(Key.esc)
        except Exception as e:
            logger.error("pynput ESC 失败: %s", e)


class _BatchItem:
    __slots__ = ("lanes", "target_time")

    def __init__(self, lanes: set[int], target_time: float) -> None:
        self.lanes = lanes
        self.target_time = target_time


_EXPIRY_TOLERANCE_SEC = 0.05


class AsyncKeyDispatcher:
    def __init__(self, sender: KeySender) -> None:
        self._sender = sender
        self._deque: deque[_BatchItem] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stopped = False
        self._fire_time_queue: deque[dict[int, float]] = deque()
        self._thread = threading.Thread(target=self._worker, name="nte-key-dispatcher", daemon=True)
        self._thread.start()

    def dispatch(self, lane_indices: list[int], target_time: float | None = None) -> None:
        if target_time is None:
            target_time = time.perf_counter() + self._sender._delay
        lanes_set = set(lane_indices)
        with self._lock:
            if self._deque:
                last = self._deque[-1]
                if last.target_time == target_time:
                    last.lanes.update(lanes_set)
                else:
                    self._deque.append(_BatchItem(lanes_set, target_time))
            else:
                self._deque.append(_BatchItem(lanes_set, target_time))
        self._wake.set()

    def drain_fire_times(self) -> dict[int, float]:
        items: list[dict[int, float]] = []
        with self._lock:
            while self._fire_time_queue:
                items.append(self._fire_time_queue.popleft())
        merged: dict[int, float] = {}
        for ft in items:
            for lane, t in ft.items():
                if lane not in merged or t > merged[lane]:
                    merged[lane] = t
        return merged

    def clear(self) -> None:
        with self._lock:
            self._deque.clear()
            self._fire_time_queue.clear()

    def stop(self) -> None:
        self._stopped = True
        self._wake.set()

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout=timeout)

    def _worker(self) -> None:
        if self._sender._mode == "foreground":
            self._sender._ensure_kb()

        while True:
            self._wake.wait()
            self._wake.clear()
            if self._stopped:
                break
            while True:
                with self._lock:
                    if not self._deque:
                        break
                    item = self._deque.popleft()
                self._execute_batch(item)

    def _execute_batch(self, item: _BatchItem) -> None:
        sender = self._sender
        now = time.perf_counter()
        if now - item.target_time > _EXPIRY_TOLERANCE_SEC:
            logger.debug("丢弃过期 batch: %d 轨, 延迟 %.3fs", len(item.lanes), now - item.target_time)
            return
        wait = item.target_time - now
        if wait > 0:
            time.sleep(wait)
        sorted_lanes = sorted(item.lanes)
        fire_times: dict[int, float] = {}
        for i in sorted_lanes:
            lane_delay = sender._delay_by_lane[i] if i < len(sender._delay_by_lane) else sender._delay
            if lane_delay > 0:
                time.sleep(lane_delay)
            sender.send_keydown(i)
            fire_times[i] = time.perf_counter()
            time.sleep(sender._hold)
            sender.send_keyup(i)
        with self._lock:
            self._fire_time_queue.append(fire_times)
        logger.debug(
            "批量按键 %d 轨: %s hold=%.3fs",
            len(sorted_lanes),
            [sender.lane_key_name(i) for i in sorted_lanes],
            sender._hold,
        )
