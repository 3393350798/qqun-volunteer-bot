from __future__ import annotations

import logging
import os
import webbrowser
import time

from .parser import classify_link

log = logging.getLogger(__name__)


class GroupJoiner:
    """Open a QQ/WeChat invitation using the user's existing client association."""

    def __init__(self, enabled: bool = False, verification_message: str = ""):
        self.enabled = enabled
        self.verification_message = verification_message

    @staticmethod
    def _qq_windows():
        from pywinauto import Desktop
        import win32api
        import win32process
        result = []
        for window in Desktop(backend="uia").windows():
            try:
                handle = win32api.OpenProcess(0x1000, False, window.element_info.process_id)
                try:
                    path = win32process.GetModuleFileNameEx(handle, 0)
                finally:
                    win32api.CloseHandle(handle)
                if os.path.basename(path).lower() == "qq.exe":
                    result.append(window)
            except Exception:
                continue
        return result

    @staticmethod
    def _button(window, name):
        return next((c for c in window.descendants(control_type="Button")
                     if c.window_text() == name and c.is_visible()), None)

    def finish_qq_join(self, timeout: float = 12) -> bool:
        """Submit the currently opened QQ invitation, never a chat message."""
        if not self.enabled:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for window in self._qq_windows():
                if window.window_text() == "加好友/群":
                    labels = [c.window_text() for c in window.descendants()]
                    if "申请加群" not in labels or "填写验证信息" not in labels:
                        continue
                    edits = window.descendants(control_type="Edit")
                    send = self._button(window, "发送")
                    if len(edits) != 1 or send is None:
                        continue
                    edits[0].set_edit_text(self.verification_message)
                    current = ""
                    try:
                        current = edits[0].get_value()
                    except Exception:
                        current = edits[0].window_text()
                    if current != self.verification_message:
                        raise RuntimeError("QQ 加群验证消息未正确填入")
                    send.click_input()
                    log.info("QQ 加群申请已发送，等待管理员审核；不代表已经入群")
                    return True
                button = self._button(window, "申请加群")
                if button is not None:
                    button.click_input()
                    break
            time.sleep(0.2)
        log.warning("未找到可提交的 QQ 加群表单，可能已在群内或需要额外验证")
        return False

    def join_image(self, control) -> bool:
        if not self.enabled:
            return False
        existing = {w.handle for w in self._qq_windows() if w.window_text() == "图片查看器"}
        if existing:
            log.warning("已有 QQ 图片查看器打开，暂不自动识别另一张图片")
            return False
        viewer = None
        try:
            control.double_click_input()
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                viewer = next((w for w in self._qq_windows() if w.window_text() == "图片查看器"), None)
                button = self._button(viewer, "识别二维码") if viewer is not None else None
                if button is not None:
                    button.click_input()
                    return self.finish_qq_join()
                time.sleep(0.2)
            log.info("QQ 图片未出现二维码识别入口")
            return False
        finally:
            if viewer is not None:
                try:
                    button = self._button(viewer, "关闭图片查看器")
                    if button is not None:
                        button.click_input()
                except Exception:
                    pass

    def join(self, url: str) -> bool:
        kind = classify_link(url)
        if kind not in {"qq_group", "wechat_group"}:
            return False
        if not self.enabled:
            log.info("发现 %s 邀请，auto_join_groups=false：%s", kind, url)
            return False
        try:
            if os.name == "nt":
                os.startfile(url)
            else:
                webbrowser.open(url)
            log.info("已打开 %s 邀请链接，等待客户端完成入群：%s", kind, url)
            if kind == "qq_group":
                return self.finish_qq_join()
            return True
        except OSError:
            log.exception("打开 %s 邀请失败：%s", kind, url)
            return False
