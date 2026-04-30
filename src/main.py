"""CLI 入口：截图、检测、按键。"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.capture import capture_runtime_status, grab_game_client_bgr, stop_wgc_grabber
from src.config_loader import default_config_path, load_config
from src.detector import RhythmDetector
from src.keys import AsyncKeyDispatcher, KeySender
from src.lanes import build_lane_layout, lane_full_roi_slice, lane_roi_quad
from src.mouse import MouseClicker
from src.presence import STATE_OTHER, STATE_PLAYING, STATE_RESULTS, STATE_SONG_SELECT, SceneGate
from src.song_selector import SongSelector
from src.window import client_rect_screen, find_unreal_game_window, window_rect_screen

logger = logging.getLogger(__name__)


def _setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _draw_overlay(
    frame: np.ndarray,
    layout,
    masks: list,
    triggers: list[bool],
) -> np.ndarray:
    vis = frame.copy()
    h, w = vis.shape[:2]
    for i in range(4):
        x0, x1, y0, y1 = lane_full_roi_slice(layout, i)
        color = (0, 255, 0) if triggers[i] else (80, 80, 255)
        quad = np.array(lane_roi_quad(layout, i, y0, y1), dtype=np.int32)
        cv2.polylines(vis, [quad], isClosed=True, color=color, thickness=2)
        # 判定带
        jy0, jy1 = layout.judge_y0_by_lane[i], layout.judge_y1_by_lane[i]
        judge_quad = np.array(lane_roi_quad(layout, i, jy0, jy1), dtype=np.int32)
        cv2.polylines(vis, [judge_quad], isClosed=True, color=(255, 200, 0), thickness=1)
        if i < len(masks) and masks[i].size > 1:
            m = masks[i]
            mh, mw = m.shape[:2]
            if mh > 0 and mw > 0:
                small = cv2.resize(m, (min(80, mw * 4), min(120, mh * 4)), interpolation=cv2.INTER_NEAREST)
                small_bgr = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
                sx = min(w - 85, 10 + i * 90)
                sy = 10
                sh, sw = small_bgr.shape[:2]
                if sy + sh < h and sx + sw < w:
                    vis[sy : sy + sh, sx : sx + sw] = cv2.addWeighted(
                        vis[sy : sy + sh, sx : sx + sw], 0.65, small_bgr, 0.35, 0
                    )
    return vis


def run_loop(
    args: argparse.Namespace,
    cfg: dict[str, Any],
    *,
    stop_event: threading.Event | None = None,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
    wait_for_window: bool = False,
) -> int:
    win_cfg = cfg.get("window") or {}
    exe = str(win_cfg.get("exe_name", "HTGame.exe"))
    cls = str(win_cfg.get("class_name", "UnrealWindow"))
    cap_cfg = cfg.get("capture") or {}
    use_client = bool(cap_cfg.get("use_client_area", True))
    cap_method = str(cap_cfg.get("method", "win32")).lower()
    if cap_method == "mss":
        logger.info("截图方式: mss（按屏幕矩形，若有窗口叠在游戏上会截到叠层）")
    elif cap_method == "wgc":
        if bool(cap_cfg.get("fallback_to_mss", True)):
            logger.info("截图方式: wgc（失败时自动 fallback 到前台直截；fallback 会吃遮挡）")
        else:
            logger.info("截图方式: wgc（Windows Graphics Capture，按游戏窗口抓帧，不吃其它 app 遮挡）")
    else:
        logger.info("截图方式: win32（GDI 窗口截图；异环/UE 场景可能吃遮挡）")

    info = None
    waiting_started = time.perf_counter()
    waiting_warned_long = False
    long_wait_threshold_sec = 30.0
    while info is None:
        if stop_event is not None and stop_event.is_set():
            return 1
        info = find_unreal_game_window(exe_name=exe, class_name=cls)
        if not info:
            if wait_for_window:
                elapsed = time.perf_counter() - waiting_started
                if status_callback:
                    status_callback({"waiting": True, "waiting_sec": elapsed})
                if not waiting_warned_long and elapsed >= long_wait_threshold_sec:
                    waiting_warned_long = True
                    msg = (
                        f"已等待 {int(elapsed)} 秒仍未找到游戏窗口。请确认：\n"
                        f"  1) 异环 ({exe}) 已启动并完成登录；\n"
                        "  2) 游戏使用「无边框窗口」模式；\n"
                        "  3) 工具与游戏运行在同一台 Windows 用户下。\n"
                        "工具会继续自动重试。"
                    )
                    logger.warning(msg)
                    if status_callback:
                        status_callback({"waiting": True, "waiting_sec": elapsed, "long_wait_hint": msg})
                else:
                    logger.warning("未找到游戏窗口，0.5s 后重试…（已等 %.1fs）", elapsed)
                time.sleep(0.5)
                continue
            logger.error("未找到游戏窗口，请确认异环已启动。")
            return 2

    hwnd = info.hwnd
    keys_cfg = cfg.get("keys") or {}
    mode = str(keys_cfg.get("mode", "foreground")).lower()
    sender = KeySender(cfg, hwnd if mode == "background" else None)
    dispatcher = AsyncKeyDispatcher(sender)
    if mode == "background":
        sender.maybe_fake_activate()
        dispatch = str(keys_cfg.get("win32_dispatch", "post")).lower()
        logger.info("按键模式: background（%s WM_KEYDOWN/UP 发往游戏 HWND，不占用全局键盘）", dispatch)
    else:
        logger.info("按键模式: foreground（pynput，需游戏能接收前台键盘）")

    detector = RhythmDetector(cfg)
    scene_gate = SceneGate(cfg)
    song_selector = SongSelector(cfg)
    mouse_clicker = MouseClicker(cfg, hwnd if mode == "background" else None)
    run_cfg = cfg.get("run") or {}
    target_fps = max(1, int(run_cfg.get("target_fps", 60)))
    frame_dt = 1.0 / target_fps

    det_cfg = cfg.get("detection") or {}
    px_log_iv = float(det_cfg.get("log_pixels_interval_sec", 0))
    last_px_log = -1e9

    hsv_ranges = list(cfg.get("hsv_ranges") or [])

    debug_dir = Path("debug_frames")
    if args.debug:
        debug_dir.mkdir(parents=True, exist_ok=True)

    save_idx = 0
    next_save = time.perf_counter()

    press_counts = [0, 0, 0, 0]
    ema_fps = 0.0
    last_status_t = time.perf_counter()
    suppress_hint_logged = False
    consecutive_grab_failures = 0
    max_grab_failures_before_relookup = 30

    logger.info("按 Ctrl+C 停止。窗口: %s", info.title or "(无标题)")
    if (cfg.get("presence") or {}).get("enabled", True):
        logger.info(
            "已启用场景门控：进入节奏界面并稳定 %d 帧后才按键；离开界面约 %d 帧后自动停止。",
            scene_gate.arm_after_good_frames,
            scene_gate.disarm_after_bad_frames,
        )
    if song_selector.enabled:
        logger.info("已启用自动选歌：进入选歌界面后将自动搜索并选择目标歌曲。")

    ar_cfg = cfg.get("auto_repeat") or {}
    ar_enabled = bool(ar_cfg.get("enabled", False))
    ar_total = max(0, int(ar_cfg.get("count", 0)))
    ar_remaining = ar_total
    ar_dismiss_delay = float(ar_cfg.get("dismiss_delay_sec", 0.8))
    ar_dismiss_stage = 0
    ar_dismiss_start = 0.0
    ar_completed_count = 0
    if ar_enabled and ar_total > 0:
        logger.info("已启用自动连打: 共 %d 次", ar_total)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            t0 = time.perf_counter()
            try:
                cr = client_rect_screen(hwnd)
                wr = window_rect_screen(hwnd)
                sx, sy, ex, ey = cr
                w, h = ex - sx, ey - sy
            except Exception as e:
                consecutive_grab_failures += 1
                logger.warning("读取窗口矩形失败：%s（连续 %d 次）", e, consecutive_grab_failures)
                w = h = 0
            if w <= 0 or h <= 0:
                consecutive_grab_failures += 1
                logger.warning("窗口尺寸异常（连续 %d 次），等待…", consecutive_grab_failures)
                if consecutive_grab_failures >= max_grab_failures_before_relookup:
                    logger.warning("游戏窗口疑似已失效，重新查找 HTGame.exe…")
                    stop_wgc_grabber()
                    info = None
                    while info is None:
                        if stop_event is not None and stop_event.is_set():
                            return 1
                        info = find_unreal_game_window(exe_name=exe, class_name=cls)
                        if info is None:
                            time.sleep(0.5)
                    hwnd = info.hwnd
                    consecutive_grab_failures = 0
                time.sleep(0.5)
                continue

            try:
                frame = grab_game_client_bgr(
                    hwnd,
                    cap_cfg,
                    client_rect=cr,
                    window_rect=wr,
                    use_client=use_client,
                )
                consecutive_grab_failures = 0
            except RuntimeError as e:
                consecutive_grab_failures += 1
                logger.warning("%s；本帧跳过（连续 %d 次失败）", e, consecutive_grab_failures)
                if status_callback:
                    status_callback({"capture_error": str(e)})
                if consecutive_grab_failures >= max_grab_failures_before_relookup:
                    logger.warning("截图持续失败，重置 WGC 会话并重新查找窗口…")
                    stop_wgc_grabber()
                    info = None
                    while info is None:
                        if stop_event is not None and stop_event.is_set():
                            return 1
                        info = find_unreal_game_window(exe_name=exe, class_name=cls)
                        if info is None:
                            time.sleep(0.5)
                    hwnd = info.hwnd
                    consecutive_grab_failures = 0
                time.sleep(0.2)
                continue
            fh, fw = frame.shape[:2]
            layout = build_lane_layout(cfg, fw, fh)
            state, gate_info = scene_gate.step(frame, layout)

            if gate_info.get("state_transitioned"):
                tpl_ss = gate_info.get("song_select_tpl_ok", False)
                tpl_rs = gate_info.get("results_tpl_ok", False)
                tpl_pl = gate_info.get("playing_tpl_ok", False)
                logger.info(
                    "场景切换 -> %s | 四鼓位通过=%s | 子模块匹配 ss=%s rs=%s pl=%s",
                    state,
                    gate_info.get("per_ok"),
                    tpl_ss,
                    tpl_rs,
                    tpl_pl,
                )

            if state == STATE_SONG_SELECT and song_selector.enabled:
                cr = client_rect_screen(hwnd)
                client_origin = (cr[0], cr[1])
                sel_info = song_selector.step(
                    frame, layout, mouse_clicker, client_origin=client_origin,
                )
            elif state == STATE_SONG_SELECT and not song_selector.enabled:
                sel_info = {"state": "disabled"}
            else:
                sel_info = {"state": "n/a"}
                if state == STATE_PLAYING:
                    song_selector.reset()

            if ar_enabled and ar_remaining > 0:
                now_ar = time.perf_counter()
                if state == STATE_RESULTS:
                    if ar_dismiss_stage == 0:
                        ar_dismiss_stage = 1
                        ar_dismiss_start = now_ar
                        logger.info(
                            "自动连打: 检测到结算界面, %.1fs 后退出 (第 %d/%d 次)",
                            ar_dismiss_delay, ar_completed_count + 1, ar_total,
                        )
                    elif ar_dismiss_stage == 1 and now_ar - ar_dismiss_start >= ar_dismiss_delay:
                        sender.send_escape()
                        ar_dismiss_stage = 2
                        ar_dismiss_start = now_ar
                        logger.info("自动连打: 已发送 ESC")
                elif ar_dismiss_stage >= 1 and state != STATE_RESULTS:
                    ar_remaining -= 1
                    ar_completed_count += 1
                    ar_dismiss_stage = 0
                    ar_dismiss_start = 0.0
                    song_selector.reset()
                    if ar_remaining > 0:
                        logger.info(
                            "自动连打: 已回到选歌界面, 剩余 %d 次 (已完成 %d/%d)",
                            ar_remaining, ar_completed_count, ar_total,
                        )
                    else:
                        logger.info("自动连打完成: 共 %d 次", ar_completed_count)
                        if stop_event is not None:
                            stop_event.set()
                        else:
                            break

            armed = bool(gate_info.get("armed", state == STATE_PLAYING))
            triggers, masks, pixels = detector.analyze(frame, layout)

            fire_times = dispatcher.drain_fire_times()
            if fire_times:
                detector.update_fire_times(fire_times)

            log_now = time.perf_counter()
            if px_log_iv > 0 and (log_now - last_px_log) >= px_log_iv:
                last_px_log = log_now
                logger.debug(
                    "各轨判定带 HSV 匹配像素 D=%d F=%d J=%d K=%d | 各轨阈值 D/F/J/K=%d/%d/%d/%d",
                    pixels[0],
                    pixels[1],
                    pixels[2],
                    pixels[3],
                    detector.min_pixels_for_lane(0),
                    detector.min_pixels_for_lane(1),
                    detector.min_pixels_for_lane(2),
                    detector.min_pixels_for_lane(3),
                )

            if any(triggers) and not armed:
                if not suppress_hint_logged:
                    logger.info(
                        "已抑制按键：场景门控未解锁（当前状态=%s）。"
                        "进入四键节奏页后会自动开始；若已进入仍不按键，可调 configs/default.yaml -> presence。",
                        state,
                    )
                    suppress_hint_logged = True
                dispatcher.clear()
            elif armed:
                suppress_hint_logged = False

            triggered = [i for i, t in enumerate(triggers) if t and armed]
            for i in triggered:
                meta = hsv_ranges[i] if i < len(hsv_ranges) else {}
                range_name = meta.get("name", "?")
                key_name = sender.lane_key_name(i)
                logger.info(
                    "识别触发 -> 按键 %s | 轨道=%d | HSV配置名=%s | 判定带像素=%d (阈值=%d) | 输入模式=%s",
                    key_name.upper(),
                    i + 1,
                    range_name,
                    pixels[i],
                    detector.min_pixels_for_lane(i),
                    mode,
                )
                press_counts[i] += 1
            if triggered:
                target_time = time.perf_counter() + sender._delay
                detector.reserve_fire_times(triggered, target_time)
                dispatcher.dispatch(triggered, target_time)

            frame_elapsed = time.perf_counter() - t0
            inst_fps = 1.0 / max(frame_elapsed, 1e-6)
            ema_fps = 0.9 * ema_fps + 0.1 * inst_fps if ema_fps > 0 else inst_fps

            now = time.perf_counter()
            if status_callback is not None and (now - last_status_t) >= 0.2:
                last_status_t = now
                status_callback(
                    {
                        "ema_fps": ema_fps,
                        "presses": tuple(press_counts),
                        "size": (fw, fh),
                        "title": info.title,
                        "triggers": tuple(triggers),
                        "pixels": tuple(pixels),
                        "scene_state": state,
                        "scene_armed": armed,
                        "scene_per_ok": tuple(gate_info.get("per_ok", ())),
                        "scene_lap": tuple(
                            round(float(x), 0) for x in gate_info.get("lap_vars", ())
                        ),
                        "scene_tpl_ss_val": gate_info.get("song_select_tpl_val", 0.0),
                        "scene_tpl_rs_val": gate_info.get("results_tpl_val", 0.0),
                        "scene_tpl_pl_val": gate_info.get("playing_tpl_val", 0.0),
                        "song_sel_state": sel_info.get("state", "n/a"),
                        "song_sel_action": sel_info.get("action", ""),
                        "ar_enabled": ar_enabled,
                        "ar_total": ar_total,
                        "ar_remaining": ar_remaining,
                        "ar_completed": ar_completed_count,
                        "capture": capture_runtime_status(),
                    }
                )

            if args.debug:
                vis = _draw_overlay(frame, layout, masks, triggers)
                if args.show:
                    cv2.imshow("nte-rhythm-auto", vis)
                    cv2.waitKey(1)
                if time.perf_counter() >= next_save:
                    path = debug_dir / f"frame_{save_idx:05d}.png"
                    cv2.imwrite(str(path), vis)
                    save_idx += 1
                    next_save = time.perf_counter() + float(args.debug_interval)

            elapsed = time.perf_counter() - t0
            sleep_t = frame_dt - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)
    except KeyboardInterrupt:
        logger.info("已停止。")
    finally:
        dispatcher.stop()
        dispatcher.join()
        stop_wgc_grabber()
        if args.show:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass
    return 0


def cmd_grab_once(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    win_cfg = cfg.get("window") or {}
    info = find_unreal_game_window(
        exe_name=str(win_cfg.get("exe_name", "HTGame.exe")),
        class_name=str(win_cfg.get("class_name", "UnrealWindow")),
    )
    if not info:
        return 2
    cap_cfg = cfg.get("capture") or {}
    use_client = bool(cap_cfg.get("use_client_area", True))
    cr = client_rect_screen(info.hwnd)
    wr = window_rect_screen(info.hwnd)
    try:
        frame = grab_game_client_bgr(
            info.hwnd,
            cap_cfg,
            client_rect=cr,
            window_rect=wr,
            use_client=use_client,
        )
    except RuntimeError as e:
        logger.error("%s", e)
        return 3
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), frame)
    logger.info("已保存: %s (%sx%s)", out, frame.shape[1], frame.shape[0])
    return 0


def cmd_test_image(args: argparse.Namespace, cfg: dict[str, Any]) -> int:
    path = Path(args.image)
    if not path.is_file():
        logger.error("文件不存在: %s", path)
        return 2
    frame = np.fromfile(str(path), dtype=np.uint8)
    frame = cv2.imdecode(frame, cv2.IMREAD_COLOR)
    if frame is None:
        logger.error("无法读取图像：%s", path)
        return 2
    h, w = frame.shape[:2]
    layout = build_lane_layout(cfg, w, h)
    detector = RhythmDetector(cfg)
    triggers, masks, pixels = detector.analyze(frame, layout)
    vis = _draw_overlay(frame, layout, masks, triggers)
    out = Path(args.out_vis) if args.out_vis else path.with_name(path.stem + "_vis.png")
    cv2.imwrite(str(out), vis)
    hsv_ranges = list(cfg.get("hsv_ranges") or [])
    fixed_keys = ("d", "f", "j", "k")
    for i, fired in enumerate(triggers):
        if fired:
            rn = (hsv_ranges[i] if i < len(hsv_ranges) else {}).get("name", "?")
            kn = fixed_keys[i] if i < len(fixed_keys) else "?"
            logger.info(
                "test-image 触发 | 轨道=%d | HSV配置名=%s | 判定带像素=%d (阈值=%d) | 将对应按键=%s",
                i + 1,
                rn,
                pixels[i],
                detector.min_pixels_for_lane(i),
                kn,
            )
    logger.info("test-image 结果 triggers=%s pixels=%s -> 可视化: %s", triggers, pixels, out)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="nte-rhythm-auto 异环四键节奏辅助原型",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
子命令示例:
  python -m src gui
  python -m src run --debug
  python -m src grab-once -o debug_frames/once.png
  python -m src test-image screenshot.png --out-vis out.png
未写子命令时默认执行 run（可与 --debug 等全局参数混用）。
""".strip(),
    )
    p.add_argument(
        "--config",
        default=str(default_config_path()),
        help="YAML 配置文件路径",
    )
    p.add_argument("--debug", action="store_true", help="写入 debug_frames 叠加图")
    p.add_argument("--show", action="store_true", help="显示 OpenCV 窗口（调试用）")
    p.add_argument("--debug-interval", type=float, default=0.5, help="debug 写盘最小间隔秒")
    sub = p.add_subparsers(dest="command", metavar="command")
    sub.required = False

    run_p = sub.add_parser("run", help="实时运行（默认）")
    run_p.set_defaults(handler="run")

    g = sub.add_parser("grab-once", help="截取一帧游戏窗口")
    g.add_argument("-o", "--output", default="debug_frames/grab_once.png")
    g.set_defaults(handler="grab")

    t = sub.add_parser("test-image", help="离线测试单张截图")
    t.add_argument("image", help="输入图片路径")
    t.add_argument("--out-vis", default="", help="输出可视化路径")
    t.set_defaults(handler="test_image")

    gui_p = sub.add_parser("gui", help="打开图形界面（开始/停止、配置路径）")
    gui_p.set_defaults(handler="gui")

    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    _setup_logging(bool(getattr(args, "debug", False)))
    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)

    handler = getattr(args, "handler", None)
    if handler is None:
        handler = "run"
    if handler == "grab":
        return cmd_grab_once(args, cfg)
    if handler == "test_image":
        return cmd_test_image(args, cfg)
    if handler == "gui":
        from src.gui import run_gui

        return run_gui(Path(args.config))
    return run_loop(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
