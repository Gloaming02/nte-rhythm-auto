"""
用你自己的全屏/窗口截图做几何标定：在图上点 4 个鼓心（顺序随意，会按 x 从左到右对应 D F J K），
再点一下判定线高度，输出可粘贴到 configs/*.yaml 的 lanes 片段。

说明：比例是「相对这张图的宽高」。若游戏实际分辨率或窗口大小与截图不同，仍需按比例目测微调，
或重新用当前分辨率截一张再标一次。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2


def run_calibrate_image(image_path: Path, output_yaml: Path | None) -> int:
    img_bytes = np.fromfile(str(image_path), dtype=np.uint8)
    img = cv2.imdecode(img_bytes, cv2.IMREAD_COLOR)
    if img is None:
        print(f"无法读取图像：{image_path}", file=sys.stderr)
        return 2

    h, w = img.shape[:2]
    clicks: list[tuple[int, int]] = []

    steps = [
        "1/5 点「左起第 1 个鼓」中心（D）",
        "2/5 点「第 2 个鼓」中心（F）",
        "3/5 点「第 3 个鼓」中心（J）",
        "4/5 点「第 4 个鼓」中心（K）",
        "5/5 点「判定线」——音符应与鼓心对齐的那条水平高度",
    ]

    win = "calibrate-image — 左键标点 | R 重设 | Q 退出"

    def draw() -> None:
        vis = img.copy()
        for i, (cx, cy) in enumerate(clicks):
            cv2.circle(vis, (cx, cy), max(4, w // 200), (0, 255, 0), 2)
            cv2.putText(vis, str(i + 1), (cx + 8, cy - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2)
        step = len(clicks)
        if step < len(steps):
            hint = steps[step] + "  |  R重设  Q退出"
        else:
            hint = "已完成 — 看终端 YAML；任意键关闭"
        y0 = 28
        for line in _wrap_text(hint, max_chars=48):
            cv2.putText(vis, line, (10, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 220, 255), 2)
            y0 += 22
        cv2.imshow(win, vis)

    def on_mouse(event: int, x: int, y: int, flags: int, param: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 5:
            clicks.append((max(0, min(w - 1, x)), max(0, min(h - 1, y))))
            draw()

    def _wrap_text(text: str, max_chars: int) -> list[str]:
        words = text.replace("  ", " ").split(" ")
        lines: list[str] = []
        cur = ""
        for word in words:
            if len(cur) + len(word) + 1 <= max_chars:
                cur = f"{cur} {word}".strip()
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines if lines else [text]

    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, min(1280, w), min(800, h))
    cv2.setMouseCallback(win, on_mouse)
    draw()

    while len(clicks) < 5:
        key = cv2.waitKey(30) & 0xFF
        if key == ord("q") or key == 27:
            cv2.destroyAllWindows()
            return 1
        if key == ord("r"):
            clicks.clear()
            draw()

    drums = sorted(clicks[:4], key=lambda p: p[0])
    xs_frac = [round(px / w, 4) for px, _ in drums]
    _, jy = clicks[4]
    judge_y_frac = round(jy / h, 4)

    block = f"""# 粘贴到 configs/*.yaml 的 lanes: 下（可按需再微调）
lanes:
  center_x_frac: {xs_frac}
  # 以下若你未改，可保留原文件的 half_width_frac、judge_band 等
  judge_line_y_frac: {judge_y_frac}
"""

    print(block)
    if output_yaml:
        outp = Path(output_yaml)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(block, encoding="utf-8")
        print(f"已写入: {outp.resolve()}", file=sys.stderr)

    # 预览
    vis = img.copy()
    for xf in xs_frac:
        cx = int(xf * w)
        cv2.line(vis, (cx, 0), (cx, h), (180, 180, 255), 1)
    cv2.line(vis, (0, jy), (w, jy), (0, 200, 255), 2)
    cv2.putText(vis, "Preview — any key to close", (10, h - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
    cv2.imshow(win, vis)
    cv2.waitKey(0)
    cv2.destroyAllWindows()
    return 0
