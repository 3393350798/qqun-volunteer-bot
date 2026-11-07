import logging

import pytest

from run import handle_message
from src.group_join import GroupJoiner
from src.parser import document_id, extract_message
from src.state import SeenState, DocumentRecords


class RecordingDocs:
    def __init__(self):
        self.submissions = []
        self.opens = []
        self.fail = False

    def open(self, url):
        self.opens.append(url)
        if self.fail:
            raise RuntimeError("测试：文档无法打开")

    def submit(self, activity, slots, profile, dry_run):
        self.submissions.append((activity.url, dry_run))

    def close(self):
        pass


def dispatch(url, state, docs, dry_run=False, prefix="招募", retries=0):
    message = extract_message(f"{prefix} 周一 09:00-10:00 {url}")
    return handle_message(message, {"runtime": {"dry_run": dry_run, "document_open_retries": retries}},
                          state, docs, GroupJoiner(), logging.getLogger(__name__), records=DocumentRecords(state.path))


@pytest.mark.parametrize("original,forwarded", [
    ("https://www.kdocs.cn/l/AbC?from=docs&startTime=1",
     "https://kdocs.cn/l/AbC?from=qq&startTime=2"),
    ("https://docs.qq.com/sheet/AbC?tab=one",
     "https://docs.qq.com/sheet/AbC?tab=two"),
])
def test_forwarded_document_only_submits_once_after_restart(tmp_path, original, forwarded):
    path = str(tmp_path / "seen.json")
    docs = RecordingDocs()
    assert dispatch(original, SeenState(path), docs)
    assert not dispatch(forwarded, SeenState(path), docs, prefix="转发补充说明")
    assert len(docs.submissions) == 1
    assert len(docs.opens) == 1


def test_drill_does_not_suppress_later_live_submission(tmp_path):
    state, docs = SeenState(str(tmp_path / "seen.json")), RecordingDocs()
    url = "https://www.kdocs.cn/l/example"
    assert dispatch(url, state, docs, dry_run=True)
    assert dispatch(url, state, docs)
    assert not dispatch(url, state, docs)
    assert [dry_run for _, dry_run in docs.submissions] == [True, False]
    assert docs.opens == [url]


def test_failed_open_skips_until_manually_released(tmp_path, monkeypatch):
    monkeypatch.setattr("run.time.sleep", lambda _: None)
    state, docs = SeenState(str(tmp_path / "seen.json")), RecordingDocs()
    url = "https://www.kdocs.cn/l/example"
    docs.fail = True
    assert not dispatch(url, state, docs, retries=2)
    assert len(docs.opens) == 3
    record = DocumentRecords(state.path).read()[-1]
    assert record["url"] == url and record["attempts"] == 3
    assert record["status"] == "打开失败，已跳过"
    assert "第 3 次：RuntimeError: 测试：文档无法打开" in record["reason"]
    docs.fail = False
    assert not dispatch(url, SeenState(str(state.path)), docs, prefix="转发")
    state.forget_document("live", document_id(url))
    assert dispatch(url, state, docs)
    assert len(docs.submissions) == 1


def test_open_retry_recovers_and_submits_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr("run.time.sleep", lambda _: None)
    state, docs = SeenState(str(tmp_path / "seen.json")), RecordingDocs()
    def open_after_failure(url):
        docs.opens.append(url)
        if len(docs.opens) == 1:
            raise RuntimeError("临时网络错误")
    docs.open = open_after_failure
    assert dispatch("https://kdocs.cn/l/retry", state, docs, retries=2)
    assert len(docs.opens) == 2 and len(docs.submissions) == 1
    record = DocumentRecords(state.path).read()[-1]
    assert record["status"] == "填写完成" and "临时网络错误" in record["reason"]


def test_submit_failure_is_recorded_without_open_retry(tmp_path):
    state, docs = SeenState(str(tmp_path / "seen.json")), RecordingDocs()
    def failed_submit(*args):
        raise RuntimeError("字段缺失")
    docs.submit = failed_submit
    assert not dispatch("https://kdocs.cn/l/fail", state, docs, retries=3)
    assert len(docs.opens) == 1
    record = DocumentRecords(state.path).read()[-1]
    assert record["status"] == "填写失败，需查看表格"
    assert "字段缺失" in record["reason"]


def test_different_document_ids_are_not_collapsed(tmp_path):
    state, docs = SeenState(str(tmp_path / "seen.json")), RecordingDocs()
    assert dispatch("https://kdocs.cn/l/AbC", state, docs)
    assert dispatch("https://kdocs.cn/l/abc", state, docs)
    assert len(docs.submissions) == 2
    assert document_id("https://docs.qq.com/sheet/AbC") != document_id("https://kdocs.cn/l/AbC")
