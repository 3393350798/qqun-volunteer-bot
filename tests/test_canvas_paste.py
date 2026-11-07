from unittest.mock import Mock

import pytest

from src.docs_adapter import TencentDocsAdapter, TencentDocsError


class DeferredPasteDriver:
    """A paste command returns before the workbook applies its change."""

    def __init__(self, apply=True):
        self.pending = None
        self.cells = {}
        self.reads = 0
        self.commands = []
        self.apply = apply

    def set_script_timeout(self, timeout):
        pass

    def execute_async_script(self, script, row, column, value):
        assert self.pending is None, "Selection changed while previous paste was pending"
        self.commands.append((row, column))
        self.pending = (row, column, value)
        self.reads = 0
        return {"ok": True}

    def execute_script(self, script, *args):
        if not args:
            return True
        self.reads += 1
        if self.pending and self.apply and self.reads >= 3:
            row, column, value = self.pending
            self.cells[row, column] = value
            self.pending = None
        return self.cells.get(tuple(args), "")


def test_waits_for_each_cell_before_pasting_next_field():
    adapter = TencentDocsAdapter({"page_timeout": 1})
    adapter.driver = DeferredPasteDriver()
    values = ["测试用户", "示例学院", "TEST_STUDENT_001", "13800000000"]
    for column, value in zip("FGHI", values):
        adapter._write_canvas_cell(None, None, f"{column}4", value, None)
    assert [adapter.driver.cells[4, col] for col in range(6, 10)] == values
    assert adapter.driver.pending is None


def test_unapplied_paste_stops_before_next_field():
    adapter = TencentDocsAdapter({"page_timeout": 0.01})
    adapter.driver = DeferredPasteDriver(apply=False)
    with pytest.raises(TencentDocsError, match="粘贴未生效：F4"):
        for column in "FGHI":
            adapter._write_canvas_cell(None, None, f"{column}4", "测试", None)
    assert adapter.driver.commands == [(4, 6)]


def test_rejected_paste_is_not_reported_as_completed():
    adapter = TencentDocsAdapter({})
    adapter.driver = DeferredPasteDriver()
    adapter.driver.execute_async_script = Mock(return_value={"ok": False})
    with pytest.raises(TencentDocsError, match="粘贴命令未完成：F4"):
        adapter._write_canvas_cell(None, None, "F4", "测试", None)
