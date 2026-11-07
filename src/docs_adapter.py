from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from dataclasses import asdict, replace

from .models import Activity, TimeSlot
from .table_schedule import row_slots

log = logging.getLogger(__name__)


class TencentDocsError(RuntimeError):
    pass


class TencentDocsAdapter:
    """Best-effort adapter for a user-authenticated Tencent Docs table.

    WPS/Tencent Docs has no stable public table-write API used here. The adapter keeps
    browser interaction isolated so selectors can be updated without touching rules.
    """

    def __init__(self, config: dict):
        self.config = config
        self.driver = None
        self._attached = False
        self._opened_tab = False
        self._edge_process = None
        self._tab_handle = None
        self.on_slot_start = None
        self.on_slot_done = None
        self._table_snapshot = None

    def _row_writable(self, values, columns):
        personal = [key for key in ("name", "college", "student_id", "phone") if key in columns]
        populated = [bool(str(values.get(key, "")).strip()) for key in personal]
        if not personal or all(populated):
            return False
        return bool(self.config.get("overwrite_incomplete_rows", True)) or not any(populated)

    def rebuild_activity(self, activity, message, semester_start=None):
        """Read the configured worksheet once; submission reuses this round's rows."""
        from selenium.webdriver.common.by import By
        selectors = self.config.get("selectors", {})
        name_box = self.driver.find_element(By.CSS_SELECTOR, selectors["name_box"])
        editor = self.driver.find_element(By.CSS_SELECTOR, selectors["editor"])
        header = int(selectors.get("header_row", 1))
        columns = self._dynamic_columns(name_box, editor, selectors, header)
        if not {"name", "student_id", "phone"} <= columns.keys():
            raise TencentDocsError("动态表头缺少姓名、学号或联系方式")
        if "time" not in columns and not {"start", "end"} <= columns.keys():
            raise TencentDocsError("表格缺少时段或开始/结束时间列，无法重建活动")
        first = int(selectors.get("data_start_row", header + 1))
        end = first + int(selectors.get("scan_max_rows", 200))
        scan_columns = selectors.get("header_scan_columns") or [self._column_name(i) for i in range(1, 27)]
        context = "\n".join(self._read_canvas_cell(name_box, editor, f"{col}{row}")
                            for row in range(1, header) for col in scan_columns)
        raw_rows = [(row, {key: self._read_canvas_cell(name_box, editor, f"{col}{row}")
                          for key, col in columns.items()}) for row in range(first, end)]
        # Do not extend the last inherited date/time into an unlimited blank tail.
        while raw_rows and not any(str(v).strip() for v in raw_rows[-1][1].values()):
            raw_rows.pop()
        carry, rows, unresolved = {}, [], []
        for row, raw in raw_rows:
            values = dict(raw)
            for key in ("location", "weekday", "date", "time", "start", "end"):
                if values.get(key, "").strip():
                    carry[key] = values[key]
                elif key in carry:
                    values[key] = carry[key]
            try:
                slots = row_slots(values, message, activity, semester_start, context)
            except ValueError as exc:
                log.warning("第 %d 行日期/时间无法解析：%s", row, exc)
                slots = []
            if not slots:
                unresolved.append(row)
            rows.append((row, values, slots))
        self._table_snapshot = dict(columns=columns, rows=rows, unresolved=unresolved,
                                    first=first, end=end, url=activity.url)
        slots = sorted({slot for _, _, slots in rows for slot in slots}, key=lambda s: (s.start, s.end))
        log.info("从表格重建活动：%d 个时段，%d 行未能确定日期/时间，扫描第 %d-%d 行", len(slots), len(unresolved), first, end - 1)
        return replace(activity, slots=tuple(slots))

    def sync_schedule(self, schedule, activity, profile):
        snapshot = self._table_snapshot
        if snapshot is None or not str(profile.get("student_id", "")).strip():
            return
        student_id = str(profile["student_id"]).strip()
        own = []
        for row, values, slots in snapshot["rows"]:
            if values.get("student_id", "").strip() != student_id:
                continue
            complete = all(values.get(key, "").strip() for key in ("name", "college", "student_id", "phone") if key in snapshot["columns"])
            for slot in slots:
                own.append(dict(slot=slot, row=str(row), location=values.get("location", ""), complete=complete))
        schedule.reconcile(activity, own, snapshot)
        log.info("日程同步读取完成：匹配本人报名 %d 条，未解析行 %d 条", len(own), len(snapshot["unresolved"]))

    def open(self, url: str) -> None:
        if self.driver is not None:
            if self._tab_handle is not None:
                self.driver.switch_to.window(self._tab_handle)
                from .parser import document_id
                if document_id(self.driver.current_url) != document_id(url):
                    raise TencentDocsError("监测标签页已跳转到其他文档，需要重新打开")
            return
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
        options = Options()
        options.use_chromium = True
        debugger_address = self.config.get("debugger_address")
        self._attached = bool(debugger_address)
        self._opened_tab = False
        if debugger_address:
            if self.config.get("auto_start_debugger", False):
                self._ensure_debugger(str(debugger_address))
            options.add_experimental_option("debuggerAddress", str(debugger_address))
        user_data_dir = self.config.get("user_data_dir")
        if user_data_dir and not debugger_address:
            options.add_argument(f"--user-data-dir={user_data_dir}")
        try:
            driver_path = self.config.get("driver_path")
            if driver_path:
                from selenium.webdriver.edge.service import Service
                self.driver = webdriver.Edge(options=options, service=Service(str(driver_path)))
            else:
                self.driver = webdriver.Edge(options=options)
        except Exception as exc:
            self._attached = False
            raise TencentDocsError("无法启动或附着 Edge 会话") from exc
        try:
            reused = False
            if self.config.get("reuse_document_tabs"):
                from .parser import document_id
                for handle in self.driver.window_handles:
                    self.driver.switch_to.window(handle)
                    try:
                        reused = document_id(self.driver.current_url) == document_id(url)
                    except ValueError:
                        continue
                    if reused:
                        break
            if self._attached:
                if not reused:
                    try:
                        self.driver.switch_to.new_window("tab")
                        self._opened_tab = True
                    except Exception:
                        raise TencentDocsError("无法创建独立报名标签页")
            self._tab_handle = getattr(self.driver, "current_window_handle", None)
            if not reused:
                self.driver.get(url)
        except Exception as exc:
            raise TencentDocsError(f"文档无法打开：{url}") from exc
        from selenium.webdriver.support.ui import WebDriverWait
        timeout = float(self.config.get("page_timeout", 15))
        ready_selector = self.config.get("ready_selector")
        if ready_selector:
            from selenium.webdriver.common.by import By
            try:
                WebDriverWait(self.driver, timeout).until(lambda driver: driver.find_elements(By.CSS_SELECTOR, ready_selector))
            except Exception as exc:
                raise TencentDocsError(f"文档未就绪或登录失效：{ready_selector}") from exc

    def _ensure_debugger(self, address: str) -> None:
        host, _, port_text = address.partition(":")
        port = int(port_text or 9222)
        try:
            with socket.create_connection((host or "127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            pass
        command = self._debugger_command(port)
        try:
            self._edge_process = subprocess.Popen(command + ["about:blank"],
                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            raise TencentDocsError(f"无法自动启动 Edge 调试会话：{command[0]}") from exc
        deadline = time.time() + float(self.config.get("debugger_start_timeout", 10))
        while time.time() < deadline:
            try:
                with socket.create_connection((host or "127.0.0.1", port), timeout=0.3):
                    log.info("已自动启动 Edge 远程调试会话：%s", address)
                    return
            except OSError:
                time.sleep(0.2)
        raise TencentDocsError(f"Edge 远程调试端口未就绪：{address}")

    def _debugger_command(self, port: int) -> list[str]:
        edge_path = self.config.get("edge_path") or os.path.join(
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            "Microsoft", "Edge", "Application", "msedge.exe")
        profile = self.config.get("debug_user_data_dir") or os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "VolunteerBotEdgeProfile")
        return [edge_path, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                "--no-first-run", "--no-default-browser-check"]

    def open_debug_browser(self, url: str = "https://www.kdocs.cn") -> None:
        address = str(self.config.get("debugger_address") or "127.0.0.1:9222")
        self._ensure_debugger(address)
        port = int(address.partition(":")[2] or 9222)
        subprocess.Popen(self._debugger_command(port) + ["--new-tab", url],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def submit(self, activity: Activity, slots: list[TimeSlot], profile: dict, dry_run: bool) -> int | None:
        if not slots:
            raise TencentDocsError("没有与不可用时间不冲突的时段")
        if dry_run:
            profile_status = {key: bool(value) for key, value in profile.items()}
            log.info("演练模式：%s", {"activity": asdict(activity), "slots": [asdict(s) for s in slots], "profile_configured": profile_status})
            return
        if not self.driver:
            raise TencentDocsError("文档浏览器尚未打开")
        required_profile = ("name", "college", "student_id", "phone")
        missing_profile = [key for key in required_profile if not str(profile.get(key, "")).strip()]
        if missing_profile:
            raise TencentDocsError(f"个人资料未配置：{', '.join(missing_profile)}")
        selectors = self.config.get("selectors", {})
        submit_mode = str(self.config.get("submit_mode", "button")).lower()
        if selectors.get("name_box") and selectors.get("editor"):
            return self._submit_canvas(activity, slots, profile, selectors, submit_mode)
        row_selector = selectors.get("row")
        if not row_selector:
            raise TencentDocsError("未配置 selectors.row；请在测试文档中配置可编辑数据行选择器")
        from selenium.webdriver.common.by import By
        header_selector = selectors.get("header")
        required_headers = self.config.get("required_headers", [])
        if header_selector and required_headers:
            headers = {item.text.strip() for item in self.driver.find_elements(By.CSS_SELECTOR, header_selector)}
            missing = [item for item in required_headers if item not in headers]
            if missing:
                raise TencentDocsError(f"文档缺少必需表头：{', '.join(missing)}")
        rows = self.driver.find_elements(By.CSS_SELECTOR, row_selector)
        if len(rows) < len(slots):
            raise TencentDocsError(f"可编辑行数量不足：需要 {len(slots)}，找到 {len(rows)}")
        values = {"name": str(profile.get("name", "")), "college": str(profile.get("college", "")), "student_id": str(profile.get("student_id", "")), "phone": str(profile.get("phone", "")), "identity_number": str(profile.get("identity_number", ""))}
        written = []
        for row_number, (row, slot) in enumerate(zip(rows, slots), 1):
            if self.on_slot_start and not self.on_slot_start(slot, row_number, ""):
                continue
            fields = {"date": slot.start.strftime("%Y-%m-%d"), "start": slot.start.strftime("%H:%M"), "end": slot.end.strftime("%H:%M"), **values}
            for key, value in fields.items():
                selector = selectors.get(key)
                if not selector:
                    continue
                cells = row.find_elements(By.CSS_SELECTOR, selector)
                if not cells:
                    raise TencentDocsError(f"行内找不到字段选择器：{key}={selector}")
                cells[0].click()
                cells[0].clear()
                cells[0].send_keys(value)
            written.append(slot)
            if submit_mode == "autosave" and self.on_slot_done:
                self.on_slot_done(slot)
        submit_selector = selectors.get("submit")
        if submit_mode != "autosave" and not submit_selector:
            raise TencentDocsError("未配置 selectors.submit，拒绝盲目提交")
        if submit_mode == "autosave":
            log.info("腾讯文档按自动保存模式完成填写：%s，共 %d 个时段", activity.url, len(written))
        else:
            self.driver.find_element(By.CSS_SELECTOR, submit_selector).click()
            if self.on_slot_done:
                for slot in written:
                    self.on_slot_done(slot)
        log.info("腾讯文档提交完成：%s，共 %d 个时段", activity.url, len(written))
        return len(written)

    def _submit_canvas(self, activity: Activity, slots: list[TimeSlot], profile: dict, selectors: dict, submit_mode: str = "button") -> int:
        """Fill WPS canvas sheets through the name box and edit proxy."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        name_box = self.driver.find_element(By.CSS_SELECTOR, selectors["name_box"])
        editor = self.driver.find_element(By.CSS_SELECTOR, selectors["editor"])
        return self._submit_canvas_dynamic(activity, slots, profile, selectors, submit_mode, name_box, editor)

    def _submit_canvas_dynamic(self, activity: Activity, slots: list[TimeSlot], profile: dict,
                               selectors: dict, submit_mode: str, name_box, editor) -> int:
        """Find rows from visible headers and fixed activity values before writing."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        header_row = int(selectors.get("header_row", 1))
        data_start = int(selectors.get("data_start_row", header_row + 1))
        max_rows = int(selectors.get("scan_max_rows", 200))
        columns = self._dynamic_columns(name_box, editor, selectors, header_row)
        required = ("name", "student_id", "phone")
        missing = [key for key in required if key not in columns]
        if missing:
            raise TencentDocsError(f"动态表头缺少必需字段：{', '.join(missing)}")
        fixed = [key for key in ("location", "weekday", "time", "date", "start", "end") if key in columns]
        if "time" not in columns and not ({"start", "end"} <= set(columns)):
            raise TencentDocsError("动态表头缺少时间字段，拒绝盲目选择空行")
        if not fixed:
            raise TencentDocsError("动态表头缺少活动匹配字段，拒绝盲目选择空行")

        snapshot = self._table_snapshot
        if snapshot is not None and snapshot["url"] != activity.url:
            snapshot = None
        if snapshot is not None:
            columns = snapshot["columns"]
            rows = [(row, values) for row, values, _ in snapshot["rows"] if self._row_writable(values, columns)]
            row_times = {row: times for row, _, times in snapshot["rows"]}
        else:
            rows = []
            carry = {}
            for row_number in range(data_start, data_start + max_rows):
                values = {key: self._read_canvas_cell(name_box, editor, f"{column}{row_number}")
                          for key, column in columns.items()}
                for key in ("location", "weekday", "date", "time", "start", "end"):
                    if values.get(key, "").strip():
                        carry[key] = values[key]
                    elif key in carry:
                        values[key] = carry[key]
                if not self._row_writable(values, columns):
                    continue
                rows.append((row_number, values))


        assignments = []
        for slot in slots:
            used_rows = {row for row, _, _ in assignments}
            if any(slot.overlaps(chosen) for _, _, chosen in assignments):
                continue
            match = next((item for item in rows if item[0] not in used_rows
                          and (slot in row_times[item[0]] if snapshot is not None else
                               self._canvas_row_matches(item[1], slot, activity, columns))), None)
            if match is None:
                label = f"{slot.start:%Y-%m-%d %H:%M}-{slot.end:%H:%M}"
                log.warning("跳过时段，未找到匹配空行：%s（扫描行 %d-%d）", label, data_start, data_start + max_rows - 1)
                continue
            assignments.append((match[0], match[1].get("location", ""), slot))

        if not assignments:
            raise TencentDocsError("找不到匹配且为空的报名行：所有可用时段均无匹配空行")
        log.info("报名行匹配完成：可填写 %d/%d 个时段，跳过 %d 个", len(assignments), len(slots), len(slots) - len(assignments))
        submit_selector = selectors.get("submit")
        if submit_mode != "autosave" and not submit_selector:
            raise TencentDocsError("未配置 selectors.submit，拒绝在真实表格中填写")
        values = {
            "name": str(profile.get("name", "")),
            "college": str(profile.get("college", "")),
            "student_id": str(profile.get("student_id", "")),
            "phone": str(profile.get("phone", "")),
            "identity_number": str(profile.get("identity_number", "")),
        }
        written = []
        for row_number, location, slot in assignments:
            current = {key: self._read_canvas_cell(name_box, editor, f"{column}{row_number}")
                       for key, column in columns.items() if key in values}
            if not self._row_writable(current, columns):
                log.info("第 %d 行已被占用，跳过本行", row_number)
                continue
            if self.on_slot_start and not self.on_slot_start(slot, row_number, location):
                continue
            log.info("开始填写报名行：第 %d 行", row_number)
            for key, value in values.items():
                column = columns.get(key)
                if not column or not value:
                    continue
                self._write_canvas_cell(name_box, editor, f"{column}{row_number}", value, Keys)
                self._raise_on_validation_error()
            log.info("报名行填写完成：第 %d 行", row_number)
            written.append(slot)
            if submit_mode == "autosave" and self.on_slot_done:
                self.on_slot_done(slot)
        if submit_mode == "autosave":
            log.info("WPS 动态行按自动保存模式完成填写，共 %d 个时段", len(written))
        else:
            self.driver.find_element(By.CSS_SELECTOR, submit_selector).click()
            if self.on_slot_done:
                for slot in written:
                    self.on_slot_done(slot)

        return len(written)

    def _dynamic_columns(self, name_box, editor, selectors: dict, header_row: int) -> dict[str, str]:
        aliases = selectors.get("header_aliases", {})
        defaults = {
            "name": ["姓名", "名字"],
            "college": ["学院", "院系", "专业"],
            "student_id": ["学号"],
            "phone": ["联系方式", "手机号", "手机", "电话"],
            "identity_number": ["身份证号", "身份证号码"],
            "location": ["点位", "地点", "岗位"],
            "weekday": ["星期", "周几", "星期几"],
            "time": ["时间", "时段"],
            "start": ["开始时间", "开始"],
            "end": ["结束时间", "结束"],
            "date": ["日期", "活动日期"],
        }
        normalized_aliases = {
            key: {self._normalize_header(value) for value in values}
            for key, values in {**defaults, **aliases}.items()
        }
        configured = selectors.get("header_scan_columns")
        columns = list(configured) if configured else [self._column_name(index) for index in range(1, 27)]
        found = {}
        for column in columns:
            value = self._read_canvas_cell(name_box, editor, f"{column}{header_row}")
            header = self._normalize_header(value)
            if not header:
                continue
            for key, candidates in normalized_aliases.items():
                if key not in found and header in candidates:
                    found[key] = column
                    break
        return found

    @staticmethod
    def _column_name(number: int) -> str:
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    @staticmethod
    def _normalize_header(value: str) -> str:
        return "".join(str(value or "").strip().lower().split()).replace("：", ":")

    def _read_canvas_cell(self, name_box, editor, target: str) -> str:
        if hasattr(self.driver, "execute_script") and self.driver.execute_script("return !!(window.APP && APP.workbook && APP.workbook.getActiveSheet)"):
            column = 0
            for char in target:
                if char.isalpha():
                    column = column * 26 + ord(char.upper()) - 64
            row = int("".join(char for char in target if char.isdigit()))
            return str(self.driver.execute_script(
                "const s=APP.workbook.getActiveSheet(); return s.getCellString(arguments[0]-1, arguments[1]-1) || '';",
                row, column,
            ) or "").strip()
        from selenium.webdriver.common.keys import Keys
        self._select_canvas_cell(name_box, target, Keys)
        for attribute in ("value", "textContent", "innerText"):
            value = editor.get_attribute(attribute)
            if value:
                return str(value).strip()
        return str(getattr(editor, "text", "") or "").strip()

    def _select_canvas_cell(self, name_box, target: str, Keys) -> None:
        if not hasattr(self.driver, "execute_cdp_cmd"):
            name_box.click()
            name_box.send_keys(Keys.CONTROL, "a", Keys.BACKSPACE, target, Keys.ENTER)
        else:
            self._cdp_click(name_box)
            for char in target:
                code = f"Key{char.upper()}" if char.isascii() and char.isalpha() else f"Digit{char}" if char.isdigit() else ""
                self._cdp_key(char, code, code, text=char)
            self._cdp_key("Enter", "Enter", "Enter")
        if self.config.get("selectors", {}).get("verify_cell_selection", True):
            selected = name_box.get_attribute("value")
            if selected and selected.upper() != target.upper():
                raise TencentDocsError(f"WPS 未定位到目标单元格：需要 {target}，当前 {selected}")

    def _cdp_click(self, element) -> None:
        rect = self.driver.execute_script(
            "const r=arguments[0].getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2};", element)
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1,
        })
        self.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": rect["x"], "y": rect["y"], "button": "left", "clickCount": 1,
        })

    def _cdp_key(self, key: str, code: str, key_code: str, *, text: str = "", modifiers: int = 0,
                 key_up_only: bool = False) -> None:
        if not key_up_only:
            payload = {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": 0, "modifiers": modifiers}
            if text:
                payload.update({"text": text, "unmodifiedText": text})
            self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", payload)
        self.driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": 0, "modifiers": modifiers,
        })

    def _write_canvas_cell(self, name_box, editor, target: str, value: str, Keys) -> None:
        if hasattr(self.driver, "execute_script") and self.driver.execute_script("return !!(window.APP && APP._uilCmdTool && APP.workbook && APP.workbook.getActiveSheet)"):
            column = 0
            for char in target:
                if char.isalpha():
                    column = column * 26 + ord(char.upper()) - 64
            row = int("".join(char for char in target if char.isdigit()))
            timeout = float(self.config.get("page_timeout", 15))
            self.driver.set_script_timeout(timeout)
            result = self.driver.execute_async_script(
                "const done=arguments[arguments.length-1], value=arguments[2]; "
                "const s=APP.workbook.getActiveSheet(), u=APP._uilCmdTool; "
                "const r=s.createRANGE(arguments[0]-1, arguments[0]-1, arguments[1]-1, arguments[1]-1); "
                "Promise.resolve().then(()=>u.setSelection(r)).then(()=>u.pasteText(value))"
                ".then(()=>done({ok:true}), e=>done({ok:false,error:String(e)}));",
                row, column, value,
            )
            if not result or not result.get("ok"):
                raise TencentDocsError(f"WPS 粘贴命令未完成：{target}")
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.common.exceptions import TimeoutException
            try:
                WebDriverWait(self.driver, timeout, poll_frequency=0.1).until(
                    lambda _: self._read_canvas_cell(name_box, editor, target) == value.strip())
            except TimeoutException as exc:
                raise TencentDocsError(f"WPS 粘贴未生效：{target}，停止后续填写") from exc
            log.info("单元格填写已读回确认：%s", target)
            return
        self._select_canvas_cell(name_box, target, Keys)
        if not hasattr(self.driver, "execute_cdp_cmd"):
            editor.click()
            editor.send_keys(Keys.CONTROL, "a", Keys.BACKSPACE, value, Keys.ENTER)
            return
        self._cdp_click(editor)
        self._cdp_key("a", "KeyA", "KeyA", text="a", modifiers=2)
        self._cdp_key("Control", "ControlLeft", "ControlLeft", modifiers=2, key_up_only=True)
        self._cdp_key("Backspace", "Backspace", "Backspace")
        for char in value:
            code = f"Key{char.upper()}" if char.isascii() and char.isalpha() else f"Digit{char}" if char.isdigit() else ""
            self._cdp_key(char, code, "", text=char)
        self._cdp_key("Enter", "Enter", "Enter")

    @staticmethod
    def _canvas_row_matches(values: dict, slot: TimeSlot, activity: Activity, columns: dict) -> bool:
        if activity.locations and columns.get("location"):
            row_location = values.get("location", "").strip()
            if not any(row_location == item or item in row_location or row_location in item for item in activity.locations):
                return False
        if columns.get("weekday"):
            expected = str(slot.start.weekday())
            actual = values.get("weekday", "").strip().lower()
            weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
            if actual not in {expected, str(slot.start.weekday() + 1), weekday_names[slot.start.weekday()], weekday_names[slot.start.weekday()].replace("周", "星期")}:
                return False
        if columns.get("date") and values.get("date", "").strip():
            actual_date = values["date"].strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
            if actual_date not in {slot.start.strftime("%Y-%m-%d"), slot.start.strftime("%Y-%m-%d").lstrip("0") }:
                return False
        if columns.get("time"):
            actual_time = values.get("time", "").replace("：", ":").replace(" ", "")
            expected_time = f"{slot.start:%H:%M}-{slot.end:%H:%M}"
            short_expected = f"{slot.start.hour}:{slot.start:%M}-{slot.end.hour}:{slot.end:%M}"
            if expected_time not in actual_time and short_expected not in actual_time:
                return False
        elif columns.get("start") and columns.get("end"):
            if values.get("start", "").strip() not in {slot.start.strftime("%H:%M"), f"{slot.start.hour}:{slot.start:%M}"}:
                return False
            if values.get("end", "").strip() not in {slot.end.strftime("%H:%M"), f"{slot.end.hour}:{slot.end:%M}"}:
                return False
        return True

    def _raise_on_validation_error(self) -> None:
        """Stop when the sheet reports a visible data-validation error."""
        from selenium.webdriver.common.by import By
        patterns = self.config.get("validation_error_patterns", ["请输入正确的身份证号码", "请输入正确的手机号"])
        try:
            body = self.driver.find_element(By.TAG_NAME, "body").text
        except Exception:
            return
        for pattern in patterns:
            if pattern and pattern in body:
                raise TencentDocsError(f"表格校验失败：{pattern}")

    def close(self, keep_tab: bool = False, complete: bool = False) -> None:
        if self.driver:
            try:
                if self._attached:
                    if (self._opened_tab or complete) and not keep_tab:
                        if self._tab_handle is not None:
                            self.driver.switch_to.window(self._tab_handle)
                        self.driver.close()
                else:
                    self.driver.quit()
            finally:
                service = getattr(self.driver, "service", None)
                self.driver = None
                self._tab_handle = None
                self._attached = False
                self._opened_tab = False
                if service is not None:
                    service.stop()
