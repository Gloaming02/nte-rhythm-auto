"""按键输出：前台 pynput / 后台 Win32 PostMessage 或 SendMessage（消息进目标 HWND，不占全局键盘焦点）。"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import win32api
import win32con
import win32gui

logger = logging.getLogger(__name__)

_LANES: tuple[str, str, str, str] = ("d", "f", "j", "k")
_VK: dict[str, int] = {"d": 0x44, "f": 0x46, "j": 0x4A, "k": 0x4B}


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
        self._hwnd = hwnd
        self._win32_dispatch = str(keys_cfg.get("win32_dispatch", "post")).lower()
        self._fake_activate = bool(keys_cfg.get("fake_activate", True))

        if self._mode == "foreground":
            from pynput.keyboard import Controller

            self._kb = Controller()
        else:
            self._kb = None

    def maybe_fake_activate(self) -> None:
        if self._mode != "background" or not self._hwnd or not self._fake_activate:
            return
        try:
            win32gui.SendMessage(self._hwnd, win32con.WM_ACTIVATE, win32con.WA_ACTIVE, 0)
        except Exception as e:
            logger.debug("fake_activate: %s", e)

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
            self._kb.press(key)
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
            self._kb.release(key)
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


class AsyncKeyDispatcher:
    _SENTINEL = None

    def __init__(self, sender: KeySender) -> None:
        self._sender = sender
        self._queue: queue.Queue[list[int] | None] = queue.Queue(maxsize=8)
        self._thread = threading.Thread(target=self._worker, name="nte-key-dispatcher", daemon=True)
        self._thread.start()

    def dispatch(self, lane_indices: list[int]) -> None:
        try:
            self._queue.put_nowait(lane_indices)
        except queue.Full:
            logger.warning("按键队列已满（%d 轨道触发被丢弃），检测帧可能跑在按键前面", len(lane_indices))

    def stop(self) -> None:
        try:
            self._queue.put_nowait(self._SENTINEL)
        except queue.Full:
            pass

    def join(self, timeout: float = 2.0) -> None:
        self._thread.join(timeout=timeout)

    def _worker(self) -> None:
        if self._sender._mode == "foreground":
            from pynput.keyboard import Controller
            self._sender._kb = Controller()

        while True:
            batch = self._queue.get()
            if batch is self._SENTINEL:
                break
            self._execute_batch(batch)

    def _execute_batch(self, lane_indices: list[int]) -> None:
        sender = self._sender
        if sender._delay > 0:
            time.sleep(sender._delay)
        for i in lane_indices:
            sender.send_keydown(i)
        time.sleep(sender._hold)
        for i in lane_indices:
            sender.send_keyup(i)
        logger.debug(
            "批量按键 %d 轨: %s hold=%.3fs",
            len(lane_indices),
            [sender.lane_key_name(i) for i in lane_indices],
            sender._hold,
        )
