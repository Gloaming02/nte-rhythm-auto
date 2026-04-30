"""简易图形界面：启动/停止、配置路径、调试选项与状态显示。"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from argparse import Namespace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from src.assets import list_song_templates
from src.config_loader import load_config
from src.main import _setup_logging, run_loop

logger = logging.getLogger(__name__)


class RhythmAutoGUI:
    def __init__(self, default_config: Path) -> None:
        self._default_config = default_config
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

        self.root = tk.Tk()
        self.root.title("nte-rhythm-auto")
        self.root.minsize(420, 320)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.var_config = tk.StringVar(value=str(default_config.resolve()))
        self.var_press_delay_sec = tk.StringVar(value="0.000")
        self.var_key_hold_sec = tk.StringVar(value="0.010")
        self.var_capture_interval_sec = tk.StringVar(value="0.011")
        self.var_auto_select = tk.BooleanVar(value=False)
        self.var_song_name = tk.StringVar(value="")
        self.var_auto_repeat = tk.BooleanVar(value=False)
        self.var_repeat_count = tk.StringVar(value="5")
        self.var_status = tk.StringVar(value="就绪。请先启动异环，再点「开始」。")

        self._build()

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        try:
            self.root.call("ttk::style", "theme", "use", "clam")
        except tk.TclError:
            pass
        self.root.minsize(720, 460)
        frm = ttk.Frame(self.root, padding=10)
        frm.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(frm, text="NTE Rhythm Auto", font=("Segoe UI", 16, "bold"))
        title.grid(row=0, column=0, columnspan=3, sticky=tk.W, **pad)
        warning = (
            "1. 准确率只能达到 80-90%，达不到 100%。\n"
            "2. 测试用的是 1280x720p、30 FPS。\n"
            "3. 确保使用管理员权限打开。\n"
            "4. 默认值如果没什么问题不需要调整。\n"
            "5. 保持游戏在前台；WGC 不可用时会 fallback 到前台直截，窗口遮挡会影响识别。"
        )
        tk.Label(frm, text=warning, wraplength=660, justify=tk.LEFT, fg="#c00000").grid(
            row=1, column=0, columnspan=3, sticky=tk.W, **pad
        )

        config_box = ttk.LabelFrame(frm, text="配置")
        config_box.grid(row=2, column=0, columnspan=3, sticky=tk.EW, **pad)
        ent = ttk.Entry(config_box, textvariable=self.var_config, width=60)
        ttk.Label(config_box, text="配置文件").grid(row=0, column=0, sticky=tk.W, **pad)
        ent.grid(row=0, column=1, sticky=tk.EW, **pad)
        ttk.Button(config_box, text="浏览…", command=self._browse_config).grid(row=0, column=2, **pad)
        config_box.columnconfigure(1, weight=1)

        timing_box = ttk.LabelFrame(frm, text="时序 / 设备差异")
        timing_box.grid(row=3, column=0, columnspan=3, sticky=tk.EW, **pad)
        ttk.Label(timing_box, text="按键延迟 s").grid(row=0, column=0, sticky=tk.W, **pad)
        ttk.Entry(timing_box, textvariable=self.var_press_delay_sec, width=8).grid(row=0, column=1, sticky=tk.W, **pad)
        ttk.Label(timing_box, text="按键保持 s").grid(row=0, column=2, sticky=tk.W, **pad)
        ttk.Entry(timing_box, textvariable=self.var_key_hold_sec, width=8).grid(row=0, column=3, sticky=tk.W, **pad)
        ttk.Label(timing_box, text="截图间隔 s").grid(row=0, column=4, sticky=tk.W, **pad)
        ttk.Entry(timing_box, textvariable=self.var_capture_interval_sec, width=8).grid(row=0, column=5, sticky=tk.W, **pad)
        ttk.Label(
            timing_box,
            text=(
                "按键延迟：识别到音符后等待多久再按，按早就加大，按晚就减小。\n"
                "按键保持：每次按键按住多久，游戏不吃键可略加大，太大会影响连续键。\n"
                "截图间隔：程序每隔多久截图/识别一次，不是游戏 FPS；数值越小越不易漏但更吃性能。默认值没问题就不要调整。"
            ),
            wraplength=620,
            justify=tk.LEFT,
        ).grid(row=1, column=0, columnspan=6, sticky=tk.W, **pad)

        song_box = ttk.LabelFrame(frm, text="自动选歌")
        song_box.grid(row=4, column=0, columnspan=3, sticky=tk.EW, **pad)
        ttk.Checkbutton(
            song_box, text="启用自动选歌", variable=self.var_auto_select,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, **pad)
        ttk.Label(song_box, text="选择歌曲").grid(row=1, column=0, sticky=tk.W, **pad)
        available_songs = [name for name, _ in list_song_templates()]
        self._song_combo = ttk.Combobox(
            song_box,
            textvariable=self.var_song_name,
            values=available_songs,
            state="readonly" if available_songs else "disabled",
            width=30,
        )
        self._song_combo.grid(row=1, column=1, sticky=tk.EW, **pad)
        if available_songs:
            self.var_song_name.set(available_songs[0])
        ttk.Label(
            song_box,
            text="模板图片位于 assets/song_templates/ 目录。将目标歌曲的卡片/名称截图放入该目录，重启后即可在下拉框中选择。",
            wraplength=620,
            justify=tk.LEFT,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, **pad)
        song_box.columnconfigure(1, weight=1)

        repeat_box = ttk.LabelFrame(frm, text="自动连打")
        repeat_box.grid(row=5, column=0, columnspan=3, sticky=tk.EW, **pad)
        ttk.Checkbutton(
            repeat_box, text="启用自动连打（选曲→演奏→结算→循环）",
            variable=self.var_auto_repeat,
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, **pad)
        ttk.Label(repeat_box, text="连打次数").grid(row=1, column=0, sticky=tk.W, **pad)
        ttk.Entry(repeat_box, textvariable=self.var_repeat_count, width=10).grid(
            row=1, column=1, sticky=tk.W, **pad
        )
        ttk.Label(
            repeat_box,
            text="结算界面会自动发送 ESC 返回选歌。需要开启自动选歌并指定歌曲。",
            wraplength=620,
            justify=tk.LEFT,
        ).grid(row=2, column=0, columnspan=2, sticky=tk.W, **pad)
        repeat_box.columnconfigure(1, weight=1)

        btn_row = ttk.Frame(frm)
        btn_row.grid(row=6, column=0, columnspan=3, pady=12)
        self.btn_start = ttk.Button(btn_row, text="开始", command=self._start)
        self.btn_start.pack(side=tk.LEFT, padx=6)
        self.btn_stop = ttk.Button(btn_row, text="停止", command=self._stop_worker, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=6)

        status_box = ttk.LabelFrame(frm, text="状态")
        status_box.grid(row=7, column=0, columnspan=3, sticky=tk.NSEW, **pad)
        ttk.Label(status_box, textvariable=self.var_status, wraplength=660, justify=tk.LEFT).pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        ttk.Label(frm, text="提示：不同设备主要调「按键延迟 s / 按键保持 s / 截图间隔 s」。", foreground="#666").grid(
            row=8, column=0, columnspan=3, sticky=tk.W, **pad
        )

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(7, weight=1)

    def _browse_config(self) -> None:
        p = filedialog.askopenfilename(
            title="选择 YAML 配置",
            filetypes=[("YAML", "*.yaml *.yml"), ("全部", "*.*")],
        )
        if p:
            self.var_config.set(p)

    def _parse_sec(self, var: tk.StringVar, default_sec: float, min_sec: float, max_sec: float) -> float:
        try:
            v = float(var.get().strip())
        except ValueError:
            v = default_sec
        return max(min_sec, min(v, max_sec))

    def _parse_int(self, var: tk.StringVar, default: int, min_v: int, max_v: int) -> int:
        try:
            v = int(float(var.get().strip()))
        except ValueError:
            v = default
        return max(min_v, min(v, max_v))

    def _apply_runtime_overrides(self, cfg: dict[str, Any]) -> None:
        keys_cfg = cfg.setdefault("keys", {})
        keys_cfg["mode"] = "foreground"
        keys_cfg["press_delay_sec"] = self._parse_sec(self.var_press_delay_sec, 0.0, 0.0, 0.2)
        keys_cfg["key_hold_sec"] = self._parse_sec(self.var_key_hold_sec, 0.01, 0.001, 0.1)
        run_cfg = cfg.setdefault("run", {})
        interval_sec = self._parse_sec(self.var_capture_interval_sec, 0.011, 0.004, 0.033)
        run_cfg["target_fps"] = max(1, int(round(1 / interval_sec)))

        song_cfg = cfg.setdefault("song_select", {})
        auto_select = self.var_auto_select.get()
        song_cfg["enabled"] = auto_select
        song_name = self.var_song_name.get().strip()
        if auto_select and song_name:
            song_cfg["song_name"] = song_name

        ar_cfg = cfg.setdefault("auto_repeat", {})
        ar_cfg["enabled"] = self.var_auto_repeat.get()
        ar_cfg["count"] = self._parse_int(self.var_repeat_count, 5, 1, 999)

    def _start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            messagebox.showinfo("提示", "已在运行中。")
            return

        cfg_path = Path(self.var_config.get().strip())
        if not cfg_path.is_file():
            messagebox.showerror("错误", f"找不到配置文件:\n{cfg_path}")
            return

        try:
            cfg = load_config(cfg_path)
            self._apply_runtime_overrides(cfg)
        except Exception as e:
            messagebox.showerror("错误", f"读取配置失败:\n{e}")
            return

        self._stop.clear()
        args = Namespace(
            debug=False,
            show=False,
            debug_interval=0.5,
        )

        self.btn_start.configure(state=tk.DISABLED)
        self.btn_stop.configure(state=tk.NORMAL)
        self.var_status.set("正在连接游戏窗口…")

        def worker() -> None:
            try:
                code = run_loop(
                    args,
                    cfg,
                    stop_event=self._stop,
                    status_callback=self._schedule_status,
                    wait_for_window=True,
                )
                if code == 2 and not self._stop.is_set():
                    self._schedule_status({"error": "未找到游戏窗口（HTGame.exe）"})
            except Exception as e:
                logger.exception("运行线程异常")
                self._schedule_status({"error": str(e)})
            finally:
                self.root.after(0, self._on_worker_done)

        self._worker = threading.Thread(target=worker, name="nte-rhythm-worker", daemon=True)
        self._worker.start()

    def _schedule_status(self, data: dict[str, Any]) -> None:
        def apply() -> None:
            self._apply_status(data)

        try:
            self.root.after(0, apply)
        except tk.TclError:
            pass

    def _apply_status(self, data: dict[str, Any]) -> None:
        if data.get("waiting"):
            elapsed = data.get("waiting_sec")
            hint = data.get("long_wait_hint")
            if hint:
                self.var_status.set(hint)
                return
            base = "等待游戏窗口…请启动异环（HTGame.exe）。"
            if isinstance(elapsed, (int, float)) and elapsed >= 1:
                base += f"\n已等待 {int(elapsed)} 秒，仍在自动重试。"
            self.var_status.set(base)
            return
        if err := data.get("error"):
            self.var_status.set(f"错误: {err}")
            return
        if capture_error := data.get("capture_error"):
            self.var_status.set(f"截图连接失败，正在自动重试…\n{capture_error}")
            return
        fps = data.get("ema_fps", 0.0)
        pr = data.get("presses", (0, 0, 0, 0))
        w, h = data.get("size", (0, 0))
        title = data.get("title") or ""
        tr = data.get("triggers")
        tr_s = f" 本帧触发: {list(tr)}" if isinstance(tr, (list, tuple)) else ""
        px = data.get("pixels")
        px_s = ""
        if isinstance(px, (list, tuple)) and len(px) >= 4:
            px_s = f" | 像素 D/F/J/K: {px[0]}/{px[1]}/{px[2]}/{px[3]}"
        armed = data.get("scene_armed")
        scene_state = data.get("scene_state", "")
        scene_s = ""
        if armed is not None:
            po = data.get("scene_per_ok")
            lap = data.get("scene_lap")
            tpl_ss_val = data.get("scene_tpl_ss_val", 0.0)
            tpl_rs_val = data.get("scene_tpl_rs_val", 0.0)
            tpl_pl_val = data.get("scene_tpl_pl_val", 0.0)
            state_label = {
                "other": "其他",
                "song_select": "选歌",
                "playing": "演奏中",
                "results": "结算",
            }.get(scene_state, scene_state)
            if isinstance(po, (list, tuple)) and isinstance(lap, (list, tuple)) and len(po) >= 4:
                scene_s = (
                    f"\n场景: {state_label} | 门控: {'已解锁可按键' if armed else '锁定中(不按键)'} | "
                    f"四鼓位={list(po)} | Lap≈{list(lap)} | 子模块匹配≈{tpl_ss_val:.2f}/{tpl_rs_val:.2f}/{tpl_pl_val:.2f}"
                )
            else:
                scene_s = f"\n场景: {state_label} | 门控: {'已解锁可按键' if armed else '锁定中(不按键)'}"

        sel_state = data.get("song_sel_state", "n/a")
        sel_action = data.get("song_sel_action", "")
        sel_s = ""
        if sel_state not in ("n/a", "disabled", "idle"):
            sel_s = f"\n选歌: 状态={sel_state} | 动作={sel_action}"
        capture_s = ""
        capture = data.get("capture")
        if isinstance(capture, dict) and capture.get("fallback_active"):
            reason = capture.get("fallback_reason") or "WGC 连接失败"
            capture_s = (
                "\n截图: WGC 连接失败，已切换到前台截图。"
                "请保持游戏在最前面，不能被其它窗口遮挡。"
                f"\n原因: {reason}"
            )
        ar_s = ""
        if data.get("ar_enabled") and data.get("ar_total", 0) > 0:
            ar_s = f"\n连打: {data.get('ar_completed', 0)}/{data.get('ar_total', 0)}"
        self.var_status.set(
            f"运行中 | ~{fps:.0f} FPS | 画面 {w}x{h} | D/F/J/K 累计: {pr[0]}/{pr[1]}/{pr[2]}/{pr[3]}"
            f"\n窗口: {title}{tr_s}{px_s}{scene_s}{sel_s}{ar_s}{capture_s}"
        )

    def _stop_worker(self) -> None:
        self._stop.set()
        self.var_status.set("正在停止…")

    def _on_worker_done(self) -> None:
        self.btn_start.configure(state=tk.NORMAL)
        self.btn_stop.configure(state=tk.DISABLED)
        if self._stop.is_set():
            self.var_status.set("已停止。")
        self._worker = None

    def _on_close(self) -> None:
        self._stop.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=2.5)
        self.root.destroy()

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(default_config: Path) -> int:
    _setup_logging(debug=False)
    app = RhythmAutoGUI(default_config)
    return app.run()
