"""Independent document threads, each owning its browser session and tab."""
import json
import logging
import queue
import time
import threading
from datetime import datetime
from dataclasses import asdict, replace
from pathlib import Path

from .docs_adapter import TencentDocsAdapter
from .group_join import GroupJoiner
from .models import Activity, Message
from .parser import DOC_RE, document_id, message_id, parse_activity, activity_title
from .state import write_json

log = logging.getLogger(__name__)


class DocumentMonitor:
    def __init__(self, config, state, records, schedule, stop_event, handler,
                 adapter_factory=TencentDocsAdapter):
        self.config, self.state, self.records, self.schedule = config, state, records, schedule
        self.stop_event, self.handler, self.adapter_factory = stop_event, handler, adapter_factory
        self.queue = queue.Queue()
        self.jobs = {}
        self.lock = threading.RLock()
        self.threads = []
        self.progress = ("等待报名链接", time.monotonic())
        self.path = Path(state.path).with_name("watched_documents.json")
        self.interval = max(0, float(config.get("runtime", {}).get("document_poll_seconds", 0)))
        self.continuous = bool(config.get("runtime", {}).get("monitor_documents", True)) and not config.get("runtime", {}).get("dry_run", True)
        self.mode = "drill" if config.get("runtime", {}).get("dry_run", True) else "live"
        runtime = config.get("runtime", {})
        self.restart_delay = max(0, float(runtime.get("thread_restart_seconds", 2)))
        self.sync_interval = max(1, float(runtime.get("schedule_sync_seconds", 60)))
        self.sources_path = self.path.with_name("document_sources.json")
        self.sources = json.loads(self.sources_path.read_text(encoding="utf-8")) if self.sources_path.exists() else {}
        self.syncing = set()
        self.sync_thread = None

    def set_phase(self, phase):
        self.progress = (phase, time.monotonic())

    def save(self):
        with self.lock:
            if self.continuous:
                write_json(self.path, [asdict(job["message"]) for job in self.jobs.values()])

    def add(self, message):
        with self.lock:
            for url in dict.fromkeys(DOC_RE.findall(message.text)):
                text = DOC_RE.sub(lambda match: url if match.group(0) == url else "", message.text)
                self._add_one(replace(message, text=text, message_id=message_id(text)))

    def _add_one(self, message):
        match = DOC_RE.search(message.text)
        if match is None:
            return
        try:
            identifier = document_id(match.group(0))
        except ValueError:
            log.warning("跳过不支持的报名链接：%s", match.group(0))
            return
        if self.mode == "live" and identifier not in self.sources:
            self.sources[identifier] = asdict(message)
            write_json(self.sources_path, self.sources)
        if identifier in self.jobs:
            return
        if self.state.contains(f"{self.mode}:manual-block:{identifier}") or self.state.contains(f"{self.mode}:document:{identifier}"):
            log.info("跳过已处理或已屏蔽链接；如需监测，可在 GUI 解除后重启：%s", match.group(0))
            return
        docs_config = dict(self.config.get("tencent_docs", {}), reuse_document_tabs=self.continuous)
        self.jobs[identifier] = dict(message=message, docs=self.adapter_factory(docs_config), next_run=0)
        self.save()
        log.info("加入表格队列：%s，当前 %d 份", match.group(0), len(self.jobs))

    def check(self, identifier):
        job = self.jobs[identifier]
        outcome = {}
        try:
            self.handler(job["message"], self.config, self.state, job["docs"], GroupJoiner(), log,
                         self.set_phase, self.records, schedule=self.schedule,
                         continuous=self.continuous, outcome=outcome)
        except Exception as exc:
            log.exception("表格任务意外退出，将由监督循环重新拉起")
            url = DOC_RE.search(job["message"].text).group(0)
            self.records.add(self.mode, url, "线程异常，等待重启", f"{type(exc).__name__}: {exc}")
            raise
        waiting = outcome.get("status", "")
        normal_end = outcome.get("finished", False) or waiting.startswith(("监测结束", "监测截止"))
        keep = self.continuous and not normal_end and (waiting == "监测中，等待空位" or waiting.startswith(("部分填写", "填写完成")))
        if keep:
            job["next_run"] = time.monotonic() + self.interval
            job["deadline"] = outcome.get("deadline")
        else:
            try:
                job["docs"].close(keep_tab=self.continuous and not normal_end, complete=normal_end)
            except Exception:
                log.exception("释放该表格会话失败，继续处理其他表格")
            finally:
                with self.lock:
                    self.state.add(f"{self.mode}:document:{identifier}")
                    del self.jobs[identifier]
                    self.save()
        self.set_phase(f"等待下轮表格检查，监测 {len(self.jobs)} 份")
        return keep

    def _document_loop(self, identifier):
        with self.lock:
            job = self.jobs[identifier]
        try:
            while not self.stop_event.is_set():
                if not self.check(identifier):
                    return
                delay = self.interval
                if job.get("deadline") is not None:
                    delay = min(delay, max(0, (job["deadline"] - datetime.now()).total_seconds()))
                if self.stop_event.wait(delay):
                    return
        except Exception:
            log.exception("表格线程异常：%s", identifier)
        finally:
            with self.lock:
                retained = identifier in self.jobs
            if retained:
                try:
                    job["docs"].close(keep_tab=self.continuous)
                except Exception:
                    log.exception("释放表格会话失败")
                job["restart_at"] = time.monotonic() + self.restart_delay

    def start_pending(self):
        with self.lock:
            if self.stop_event.is_set():
                return
            self.threads = [thread for thread in self.threads if thread.is_alive()]
            for identifier, job in self.jobs.items():
                previous = job.get("thread")
                if identifier not in self.syncing and (previous is None or not previous.is_alive()) and time.monotonic() >= job.get("restart_at", 0):
                    if previous is not None:
                        job["docs"] = self.adapter_factory(dict(self.config.get("tencent_docs", {}), reuse_document_tabs=self.continuous))
                        log.warning("自动重新拉起表格线程：%s", identifier)
                    thread = threading.Thread(target=self._document_loop, args=(identifier,), name=f"表格-{identifier[:8]}")
                    job["thread"] = thread
                    self.threads.append(thread)
                    thread.start()

    def sync_once(self):
        """Read known, inactive documents without registering or reopening monitors."""
        with self.lock:
            sources = dict(self.sources)
        last_status = {}
        for url in self.schedule.known_urls():
            sources.setdefault(document_id(url), asdict(Message(message_id(url), url)))
        for item in self.records.read():
            if item["mode"] == "live":
                url = item["url"]
                sources.setdefault(document_id(url), asdict(Message(message_id(url), url)))
                last_status[document_id(url)] = item["status"]
        for identifier, source in sources.items():
            if self.stop_event.is_set():
                return
            if self.state.contains(f"live:manual-block:{identifier}") or last_status.get(identifier, "").startswith("打开失败"):
                continue
            with self.lock:
                if identifier in self.jobs:
                    continue
                self.syncing.add(identifier)
            docs = None
            try:
                message = Message(**source)
                url = DOC_RE.search(message.text).group(0)
                try:
                    activity = parse_activity(message, semester_start_date=self.config.get("activity", {}).get("semester_start_date"))
                except ValueError:
                    activity = Activity(url, activity_title(message.text), ())
                docs = self.adapter_factory(dict(self.config.get("tencent_docs", {}), reuse_document_tabs=False))
                log.info("自动同步本地日程：%s", url)
                docs.open(url)
                activity = docs.rebuild_activity(activity, message, self.config.get("activity", {}).get("semester_start_date"))
                docs.sync_schedule(self.schedule, activity, self.config.get("profile", {}))
            except Exception as exc:
                log.warning("日程同步失败，保留本地记录：%s；%s", identifier, exc)
            finally:
                if docs is not None:
                    try:
                        docs.close()
                    except Exception:
                        log.exception("关闭日程同步标签失败")
                with self.lock:
                    self.syncing.discard(identifier)

    def _sync_loop(self):
        while not self.stop_event.is_set():
            try:
                self.sync_once()
            except Exception:
                log.exception("日程同步轮次异常，下轮重新尝试")
            if self.stop_event.wait(self.sync_interval):
                return

    def run(self):
        try:
            if self.continuous and self.path.exists():
                messages = json.loads(self.path.read_text(encoding="utf-8"))
                for item in messages:
                    self.add(Message(**item))
            while not self.stop_event.is_set():
                try:
                    message = self.queue.get(timeout=0.2)
                    self.add(message)
                except queue.Empty:
                    pass
                self.start_pending()
                if self.mode == "live" and self.config.get("tencent_docs", {}).get("selectors", {}).get("name_box"):
                    if self.sync_thread is None or not self.sync_thread.is_alive():
                        self.sync_thread = threading.Thread(target=self._sync_loop, name="日程同步")
                        self.sync_thread.start()
        except Exception:
            log.exception("表格工作线程停止，通知 QQ 监听退出")
            self.stop_event.set()
        finally:
            self.stop_event.set()
            for thread in self.threads:
                thread.join()
            if self.sync_thread is not None:
                self.sync_thread.join()
            while not self.queue.empty():
                self.add(self.queue.get_nowait())
            self.save()

    def drain_once(self):
        while not self.queue.empty():
            self.add(self.queue.get_nowait())
        for identifier in list(self.jobs):
            self.check(identifier)
