from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from src.models import Activity
from src.parser import activity_title
from src.config import ensure_dirs, load_config
from src.docs_adapter import TencentDocsAdapter, TencentDocsError
from src.group_join import GroupJoiner
from src.parser import DOC_RE, extract_group_links, parse_activity, document_id
from src.qq_adapter import QQWindowReader
from src.state import SeenState, DocumentRecords
from src.time_rules import available_slots, unavailable_slots
from src.schedule import Schedule
from src.monitor import DocumentMonitor


def handle_message(message, config, state, docs, joiner, log, set_phase=None, records=None,
                   schedule=None, continuous=False, outcome=None) -> bool:
    def phase(value):
        if set_phase is not None:
            set_phase(value)
        log.info("%s：消息=%s", value, message.message_id)

    def record(status, reason="", attempts=0):
        if outcome is not None:
            outcome["status"] = status
        link = DOC_RE.search(message.text)
        if records is not None and link:
            records.add(mode, link.group(0), status, reason, attempts)

    def error_reason(exc):
        reasons = []
        while exc is not None:
            reasons.append(f"{type(exc).__name__}: {exc}")
            exc = exc.__cause__
        return " → ".join(reasons)

    def close_docs():
        try:
            docs.close()
        except Exception:
            log.exception("关闭文档会话失败")

    phase("检查消息记录")
    dry_run = bool(config.get("runtime", {}).get("dry_run", True))
    allow_cross = bool(config.get("runtime", {}).get("allow_cross_document_conflicts", False))
    lead = timedelta(days=max(0, float(config.get("runtime", {}).get("document_deadline_days", 0))))
    mode = "drill" if dry_run else "live"
    message_key = f"{mode}:message:{message.message_id}"
    if not continuous and state.contains(message_key):
        log.info("消息已处理，跳过：%s", message.message_id)
        return False
    phase("解析群邀请")
    groups = extract_group_links(message.text)
    for url in groups:
        phase("处理群邀请")
        joiner.join(url)
    if groups:
        log.info("同步处理群邀请（不影响文档报名）：%s", groups)
    try:
        phase("解析活动日期和时段")
        weekdays = tuple(config.get("activity", {}).get("default_weekdays", [0, 1, 2, 3, 4]))
        activity = parse_activity(message, default_weekdays=weekdays,
                                  semester_start_date=config.get("activity", {}).get("semester_start_date"))
        document_key = f"{mode}:document:{document_id(activity.url)}"
    except ValueError as exc:
        if DOC_RE.search(message.text):
            if hasattr(docs, "rebuild_activity") and config.get("tencent_docs", {}).get("selectors", {}).get("name_box"):
                log.warning("消息日期无法解析，尝试表格日期：%s", exc)
                activity = Activity(DOC_RE.search(message.text).group(0), activity_title(message.text), ())
                document_key = f"{mode}:document:{document_id(activity.url)}"
            else:
                log.warning("活动日期解析失败：%s", exc)
                record("日期解析失败", error_reason(exc))
                return False
        else:
            if groups:
                log.info("发现群邀请链接，等待加入后继续监听：%s", groups)
            else:
                log.info("消息包含图片或未识别链接，等待群内后续报名文档：%s", message.message_id)
            state.add(message_key)
            return False
    if state.contains(f"{mode}:manual-block:{document_id(activity.url)}") or (not continuous and state.contains(document_key)):
        log.info("该文档已处理，跳过重复报名：%s", activity.url)
        if outcome is not None:
            outcome["status"] = "已屏蔽"
        state.add(message_key)
        return False
    can_scan = hasattr(docs, "rebuild_activity") and bool(config.get("tencent_docs", {}).get("selectors", {}).get("name_box"))
    open_attempts = 0
    open_errors = []
    def open_document():
        nonlocal open_attempts
        if dry_run and not can_scan:
            return True
        retries = max(0, int(config.get("runtime", {}).get("document_open_retries", 2)))
        for attempt in range(retries + 1):
            open_attempts = attempt + 1
            phase(f"打开 Edge 报名文档（第 {open_attempts}/{retries + 1} 次）")
            try:
                docs.open(activity.url)
                break
            except Exception as exc:
                reason = f"第 {open_attempts} 次：{error_reason(exc)}"
                open_errors.append(reason)
                log.warning("文档打开失败：%s；%s", activity.url, reason)
                close_docs()
                if attempt == retries:
                    record("打开失败，已跳过", "\n".join(open_errors), open_attempts)
                    state.add(document_key)
                    state.add(message_key)
                    log.error("文档打开 %d 次均失败，跳过该链接；可在 GUI 文档记录中允许重试：%s", open_attempts, activity.url)
                    return False
                phase("等待 1 秒后重试打开文档")
                time.sleep(1)
        return True
    if can_scan and not open_document():
        return False
    if can_scan:
        try:
            phase("从表格重建活动时段并同步日程")
            activity = docs.rebuild_activity(activity, message, config.get("activity", {}).get("semester_start_date"))
            if schedule is not None and not dry_run:
                docs.sync_schedule(schedule, activity, config.get("profile", {}))
        except Exception as exc:
            log.exception("读取表格时段失败")
            record("表格解析失败", error_reason(exc), open_attempts)
            if not continuous:
                close_docs()
            return False
    if not activity.slots:
        log.warning("未解析到活动时段：%s", activity.url)
        record("未填写", "表格未能确定日期和起止时间" if can_scan else "未解析到活动时段")
        if not continuous:
            close_docs()
        state.add(message_key)
        return False
    start = min(s.start for s in activity.slots)
    end = max(s.end for s in activity.slots)
    try:
        phase("读取不可用时间和 ICS 日历")
        blocked = unavailable_slots(config, start, end)
    except (OSError, ValueError, KeyError) as exc:
        log.exception("读取不可用时间失败，停止本次报名：%s", exc)
        record("未填写", error_reason(exc))
        if not continuous:
            close_docs()
        return False
    def remaining_slots():
        occupied = schedule.blocked(activity.url, allow_cross) if schedule is not None else []
        cancelled = schedule.cancelled(activity.url) if schedule is not None else []
        remaining = available_slots(list(activity.slots), [*blocked, *occupied, *cancelled])
        return [s for s in remaining if s.start > datetime.now()] if continuous else remaining

    slots = remaining_slots()
    log.info("发现活动 %s，可用时段 %d/%d", activity.url, len(slots), len(activity.slots))
    if not slots:
        log.warning("活动没有可报名时段：%s", activity.url)
        record("监测结束，无待报名时段" if continuous else "无待报名时段", "时段已过期、已报名或与不可用时间/本地日程冲突")
        state.add(message_key)
        if not continuous:
            close_docs()
        return False
    deadline = max(s.start for s in slots) - lead
    if continuous:
        if outcome is not None:
            outcome["deadline"] = deadline
        if datetime.now() >= deadline:
            record("监测截止，关闭表格", f"最后待报名时段 {max(s.start for s in slots):%Y-%m-%d %H:%M}；提前 {lead.total_seconds() / 86400:g} 天；截止 {deadline:%Y-%m-%d %H:%M}")
            return False
    if not can_scan and not open_document():
        return False
    try:
        phase("计算演练结果" if dry_run else "读取表头、匹配空行并填写")
        if schedule is not None and not dry_run:
            def begin_slot(slot, row, location):
                with schedule.lock:
                    if continuous:
                        pending = remaining_slots()
                        if not pending or datetime.now() >= max(s.start for s in pending) - lead:
                            return False
                    return schedule.begin(activity, slot, row, location, allow_cross)
            docs.on_slot_start = begin_slot
            docs.on_slot_done = lambda slot: schedule.confirm(activity, slot)
        filled = docs.submit(activity, slots, config.get("profile", {}), dry_run)
    except Exception as exc:
        if isinstance(exc, TencentDocsError) and "找不到匹配且为空的报名行" in str(exc):
            runtime = config.get("runtime", {})
            limit = 0 if continuous else int(runtime.get("no_slot_retry_limit", 3))
            retry_key = f"{mode}:no-slot:{document_id(activity.url)}"
            if not continuous:
                state.add(f"{retry_key}:{state.count_prefix(retry_key + ':') + 1}")
            attempts = state.count_prefix(retry_key + ":")
            if continuous:
                log.info("文档暂无匹配空行，保留标签页持续监测：%s", activity.url)
            else:
                log.warning("文档没有匹配空行，第 %d 次：%s", attempts, activity.url)
            if limit > 0 and attempts >= limit:
                state.add(document_key)
                state.add(message_key)
                log.warning("连续 %d 次找不到空行，已屏蔽该文档：%s", attempts, activity.url)
            elif not continuous:
                log.warning("本次无空行，文档未屏蔽；再次处理该文档时可重试：%s", activity.url)
            record("监测中，等待空位" if continuous else ("无空位，已屏蔽" if limit > 0 and attempts >= limit else "无空位，未屏蔽"),
                   "\n".join([*open_errors, str(exc)]), open_attempts)
            return False
        log.exception("填写失败，本次停止；可能已有部分单元格写入，请查看文档：%s", exc)
        record("填写失败，需查看表格", "\n".join([*open_errors, error_reason(exc)]), open_attempts)
        return False
    finally:
        phase("本轮表格检查结束，保留标签页" if continuous else "结束本次文档会话")
        if not continuous:
            close_docs()
    phase("保存报名处理记录")
    status = "演练，未填写" if dry_run else "填写完成"
    if not dry_run and isinstance(filled, int):
        status = f"{'部分填写' if filled < len(slots) else '填写完成'}（{filled}/{len(slots)} 时段）"
    record(status, "\n".join(open_errors), open_attempts)
    if continuous and outcome is not None:
        remaining = remaining_slots()
        if remaining:
            outcome["deadline"] = max(s.start for s in remaining) - lead
        if not remaining or datetime.now() >= outcome["deadline"]:
            outcome["finished"] = True
            log.info("本表可用时段已处理完或已到提前截止时间，关闭表格：%s", activity.url)
    if not continuous:
        state.add(document_key)
        state.add(message_key)
    return True


def main(config_path: str = "config.yaml", once: bool = False, dry_run_override: bool | None = None,
         control_stdin: bool = False) -> None:
    config = load_config(config_path)
    if dry_run_override is not None:
        config.setdefault("runtime", {})["dry_run"] = dry_run_override
        if dry_run_override:
            config.setdefault("qq", {})["auto_join_groups"] = False
    ensure_dirs(config)
    log_format = "%(asctime)s %(levelname)s [%(threadName)s] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.FileHandler(config["runtime"]["log_file"], encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger(__name__)
    state = SeenState(config["runtime"].get("state_file", "data/seen_messages.json"))
    records = DocumentRecords(state.path)
    qq_config = config.get("qq", {})
    reader = QQWindowReader(
        qq_config.get("process_name", "QQ.exe"),
        qq_config.get("groups", []),
        bool(qq_config.get("scan_all_groups", not (qq_config.get("groups") or qq_config.get("group_names")))),
        qq_config.get("group_names", []),
        bool(qq_config.get("scan_qr_images", True)),
        str(qq_config.get("message_window_title", "")),
    )
    joiner = GroupJoiner(bool(qq_config.get("auto_join_groups", False)),
                        str(qq_config.get("join_verification_message", "")))
    reader.group_joiner = joiner
    poll_seconds = float(config.get("runtime", {}).get("poll_seconds", 1))
    log.info("机器人启动，PID=%s，dry_run=%s，轮询间隔=%.1f秒", os.getpid(), config["runtime"].get("dry_run", True), poll_seconds)
    stop_event = threading.Event()
    monitor_done = threading.Event()
    worker_config = config if not once else {**config, "runtime": {**config["runtime"], "monitor_documents": False}}
    worker = DocumentMonitor(worker_config, state, records, Schedule(state.path), stop_event, handle_message)
    worker_thread = None if once else threading.Thread(target=worker.run, name="document-monitor")
    if worker_thread:
        worker_thread.start()

    def report_progress():
        while not monitor_done.wait(10):
            phase, started = reader.progress
            elapsed = time.monotonic() - started
            report = log.warning if elapsed >= 30 else log.info
            report("运行状态：PID=%s，已完成轮询=%d，阶段=%s，阶段耗时=%.1f秒%s",
                   os.getpid(), reader.completed_polls, phase, elapsed,
                   "（该阶段尚未返回）" if elapsed >= 30 else "")
            doc_phase, doc_started = worker.progress
            log.info("表格状态：%s，耗时 %.1f 秒，待接收消息 %d 条", doc_phase, time.monotonic() - doc_started, worker.queue.qsize())

    monitor = threading.Thread(target=report_progress, daemon=True)
    monitor.start()
    if control_stdin:
        def wait_for_stop():
            sys.stdin.readline()
            stop_event.set()
            log.info("收到停止请求，当前任务结束后停止监听")
        threading.Thread(target=wait_for_stop, daemon=True).start()
    try:
        messages = reader.messages() if once else reader.poll(poll_seconds, stop_event)
        for message in messages:
            if stop_event.is_set():
                break
            worker.queue.put(message)
            # QQ client operations stay on the reader thread; browser work runs separately.
            for url in extract_group_links(message.text):
                try:
                    joiner.join(url)
                except Exception:
                    log.exception("处理群邀请失败，已排队的文档继续处理：%s", url)
            log.info("消息已交给表格队列：%s", message.message_id)
        if once:
            worker.drain_once()
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        reader.set_phase("停止 QQ 监听，等待表格任务结束")
        stop_event.set()
        try:
            if worker_thread:
                worker_thread.join()
        finally:
            monitor_done.set()
            monitor.join()
            log.info("机器人停止，PID=%s", os.getpid())


if __name__ == "__main__":
    args = sys.argv[1:]
    config = next((arg for arg in args if not arg.startswith("--")), "config.yaml")
    main(config, once="--once" in args, dry_run_override=True if "--dry-run" in args else None,
         control_stdin="--gui" in args)
