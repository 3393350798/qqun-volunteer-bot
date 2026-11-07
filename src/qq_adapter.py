from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

from .models import Message
from .parser import extract_message, message_id

log = logging.getLogger(__name__)


class QQWindowReader:
    """Read visible QQNT text through UI Automation; no password or login handling."""

    def __init__(self, process_name: str = "QQ.exe", groups: list[str] | tuple[str, ...] = (), scan_all_groups: bool = True,
                 group_names: list[str] | tuple[str, ...] = (), scan_qr_images: bool = True,
                 message_window_title: str = ""):
        self.process_name = process_name
        self.groups = {str(item).strip() for item in groups if str(item).strip()}
        self.group_names = {str(item).strip() for item in group_names if str(item).strip()}
        self.scan_all_groups = scan_all_groups
        self.scan_qr_images = scan_qr_images
        self.message_window_title = message_window_title
        self._seen_text: set[str] = set()
        self.completed_polls = 0
        self.group_joiner = None
        self._seen_images: set[str] = set()
        self.progress = ("准备监听", time.monotonic())

    def set_phase(self, phase: str) -> None:
        self.progress = (phase, time.monotonic())

    @staticmethod
    def message_blocks(controls):
        root = next((c for c in controls if getattr(c.element_info, "automation_id", "") == "ml-root"), None)
        if root is None:
            log.warning("QQ 未暴露消息列表 ml-root，跳过文本读取，避免将侧栏或相邻消息误当活动")
            return []
        result = []
        for bubble in root.children():
            values = []
            for control in [bubble, *bubble.descendants()]:
                kind = control.element_info.control_type
                if kind == "Text" or (kind == "Hyperlink" and not control.children()):
                    value = control.window_text().strip()
                    if value:
                        values.append(value)
            if values:
                result.append("\n".join(values))
        return result

    def _group_context(self, window, controls: list) -> tuple[bool, str]:
        active_names = []
        for control in controls:
            try:
                if getattr(control.element_info, "class_name", "") == "chat-header__contact-name":
                    value = control.window_text().strip()
                    if value:
                        active_names.append(value)
            except Exception:
                continue
        if self.scan_all_groups:
            return True, active_names[0] if active_names else ""
        if self.group_names:
            matched_name = next((name for name in self.group_names if any(name == active or name in active for active in active_names)), "")
            return bool(matched_name), matched_name
        labels = []
        for control in (window, *controls):
            try:
                value = control.window_text().strip()
                if value and value not in labels:
                    labels.append(value)
            except Exception:
                continue
        context = "\n".join(labels)
        if not self.groups or self.scan_all_groups:
            return True, ""
        matched = next((group for group in self.groups if group in context), "")
        return bool(matched), matched

    def messages(self) -> Iterator[Message]:
        self.set_phase("查找 QQ 窗口")
        try:
            from pywinauto import Desktop
            import os
            import win32api
            import win32process
            windows = []
            for candidate in Desktop(backend="uia").windows():
                try:
                    pid = candidate.element_info.process_id
                    handle = win32api.OpenProcess(0x1000, False, pid)
                    executable = win32process.GetModuleFileNameEx(handle, 0)
                    win32api.CloseHandle(handle)
                    if os.path.basename(executable).lower() == self.process_name.lower():
                        windows.append(candidate)
                except Exception:
                    continue
        except Exception as exc:
            log.warning("无法读取 QQ 窗口：%s", exc)
            return
        log.info("发现 QQ 窗口 %d 个，监听模式=%s，指定群=%s", len(windows), "可见会话" if self.scan_all_groups else "指定群", sorted(self.group_names or self.groups))
        for window in windows:
            try:
                if self.message_window_title and self.message_window_title not in window.window_text():
                    continue
                self.set_phase("读取 QQ 控件树")
                controls = [window, *window.descendants()]
                self.set_phase("识别当前群和读取文本")
                allowed, group_id = self._group_context(window, controls)
                if not allowed:
                    log.info("跳过未匹配指定群的 QQ 窗口：标题=%r", window.window_text())
                    continue
                blocks = self.message_blocks(controls)
            except Exception as exc:
                log.warning("读取 QQ 会话失败：%s", exc)
                continue
            self.set_phase("提取 QQ 消息")
            for candidate in blocks:
                key = group_id + ":" + message_id(candidate)
                if key in self._seen_text:
                    continue
                self._seen_text.add(key)
                message = extract_message(candidate)
                if message:
                    log.info("监听到 QQ 活动消息：群=%s，消息=%s", group_id or "未识别", message.message_id)
                    yield Message(message.message_id, message.text, message.source, message.image_paths, group_id)
                    self.set_phase("提取 QQ 消息")
            if not self.scan_qr_images:
                continue
            try:
                import os
                import tempfile
                self.set_phase("截取 QQ 窗口以识别二维码")
                image = window.capture_as_image()
                fd, filename = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                try:
                    image.save(filename)
                    self.set_phase("识别 QQ 窗口二维码")
                    from .qr import decode_qr
                    started = time.monotonic()
                    urls = decode_qr(filename)
                    log.info("二维码识别完成：群=%s，识别 %d 个，耗时 %.2f 秒", group_id or "未识别", len(urls), time.monotonic() - started)
                    for url in urls:
                        if url in self._seen_text:
                            continue
                        self._seen_text.add(url)
                        yield Message(message_id(f"qr:{url}"), f"二维码群邀请：{url}", "qq", (), group_id)
                        self.set_phase("处理二维码识别结果")
                    if not urls and self.group_joiner and self.group_joiner.enabled:
                        import hashlib
                        for control in controls:
                            if control.window_text() != "图片" or control.element_info.control_type != "Image" or not control.is_visible():
                                continue
                            picture = control.capture_as_image()
                            key = group_id + hashlib.sha256(picture.tobytes()).hexdigest()
                            if key in self._seen_images:
                                continue
                            self._seen_images.add(key)
                            self.set_phase("通过 QQ 原图识别并申请加群")
                            self.group_joiner.join_image(control)
                finally:
                    os.unlink(filename)
            except Exception as exc:
                log.warning("QQ窗口二维码识别跳过：%s", exc)

    def poll(self, seconds: float = 1.0, stop_event=None) -> Iterator[Message]:
        while stop_event is None or not stop_event.is_set():
            yield from self.messages()
            self.completed_polls += 1
            self.set_phase("本轮读取完成，等待下次轮询")
            if stop_event is None:
                time.sleep(seconds)
            else:
                stop_event.wait(seconds)
