import logging

import pytest

from src.group_join import GroupJoiner


class Control:
    def __init__(self, text, value=""):
        self.text = text
        self.value = value
        self.clicked = False

    def window_text(self):
        return self.text

    def is_visible(self):
        return True

    def set_edit_text(self, value):
        self.value = value

    def get_value(self):
        return self.value

    def click_input(self):
        self.clicked = True


class Form(Control):
    def __init__(self):
        super().__init__("加好友/群")
        self.edit = Control("")
        self.send = Control("发送")

    def descendants(self, control_type=None):
        if control_type == "Edit":
            return [self.edit]
        if control_type == "Button":
            return [self.send]
        return [Control("申请加群"), Control("填写验证信息")]


def test_verification_uses_value_and_reports_pending(monkeypatch, caplog):
    form = Form()
    joiner = GroupJoiner(True, "测试验证消息")
    monkeypatch.setattr(joiner, "_qq_windows", lambda: [form])
    with caplog.at_level(logging.INFO):
        assert joiner.finish_qq_join()
    assert form.edit.value == "测试验证消息"
    assert form.send.clicked
    assert "等待管理员审核；不代表已经入群" in caplog.text


def test_failed_verification_write_does_not_send(monkeypatch):
    form = Form()
    joiner = GroupJoiner(True, "测试验证消息")
    monkeypatch.setattr(joiner, "_qq_windows", lambda: [form])
    monkeypatch.setattr(form.edit, "set_edit_text", lambda value: None)
    with pytest.raises(RuntimeError, match="未正确填入"):
        joiner.finish_qq_join()
    assert not form.send.clicked
