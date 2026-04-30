"""鼠标点击与滚动：前台 pynput / 后台 Win32 SendMessage。"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class MouseClicker:
    def __init__(self, cfg: dict[str, Any], hwnd: int | None = None) -> None:
        keys_cfg = cfg.get("keys") or {}
        self._mode = str(keys_cfg.get("mode", "foreground")).lower()
        self._hwnd = hwnd
        self._click_delay = float(keys_cfg.get("mouse_click_delay_sec", 0.05))
        self._mouse: Any = None

    def _ensure_mouse(self) -> Any:
        if self._mouse is not None:
            return self._mouse
        from pynput.mouse import Controller

        self._mouse = Controller()
        return self._mouse

    def click(
        self,
        client_x: int,
        client_y: int,
        *,
        client_origin: tuple[int, int] | None = None,
    ) -> None:
        if self._mode == "background" and self._hwnd:
            self._click_background(client_x, client_y)
        else:
            self._click_foreground(client_x, client_y, client_origin)

    def _click_foreground(
        self,
        client_x: int,
        client_y: int,
        client_origin: tuple[int, int] | None,
    ) -> None:
        if client_origin is None:
            logger.warning("前台鼠标点击需要 client_origin")
            return
        sx = client_origin[0] + client_x
        sy = client_origin[1] + client_y
        m = self._ensure_mouse()
        m.position = (sx, sy)
        time.sleep(self._click_delay)
        from pynput.mouse import Button

        m.click(Button.left, 1)

    def _click_background(self, client_x: int, client_y: int) -> None:
        if not self._hwnd:
            return
        import win32con
        import win32gui

        lparam = (client_y << 16) | (client_x & 0xFFFF)
        win32gui.PostMessage(
            self._hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam
        )
        time.sleep(self._click_delay)
        win32gui.PostMessage(self._hwnd, win32con.WM_LBUTTONUP, 0, lparam)

    def scroll(
        self,
        client_x: int,
        client_y: int,
        delta: int,
        *,
        client_origin: tuple[int, int] | None = None,
    ) -> None:
        if self._mode == "background" and self._hwnd:
            self._scroll_background(client_x, client_y, delta)
        else:
            self._scroll_foreground(client_x, client_y, delta, client_origin)

    def _scroll_foreground(
        self,
        client_x: int,
        client_y: int,
        delta: int,
        client_origin: tuple[int, int] | None,
    ) -> None:
        if client_origin is None:
            return
        sx = client_origin[0] + client_x
        sy = client_origin[1] + client_y
        m = self._ensure_mouse()
        m.position = (sx, sy)
        time.sleep(self._click_delay)
        m.scroll(0, delta)

    def _scroll_background(
        self, client_x: int, client_y: int, delta: int
    ) -> None:
        if not self._hwnd:
            return
        import win32con
        import win32gui

        wparam = (delta << 16) & 0xFFFF0000
        lparam = (client_y << 16) | (client_x & 0xFFFF)
        win32gui.PostMessage(self._hwnd, win32con.WM_MOUSEWHEEL, wparam, lparam)
