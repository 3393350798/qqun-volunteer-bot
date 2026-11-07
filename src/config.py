from pathlib import Path
from typing import Any
import yaml


DEFAULT_CONFIG = '''# Q群志愿机器人：个人配置仅保存在本机，请勿上传
# 报名资料，按目标表格实际要求填写
profile:
  # 姓名
  name: ""
  # 学院或组织
  college: ""
  # 学号或报名标识
  student_id: ""
  # 联系电话
  phone: ""
  # 可选，仅表格明确要求时填写
  identity_number: ""
# 日期解析
activity:
  # 学期第一周的周一，格式 YYYY-MM-DD；使用学期周时必填
  semester_start_date: ""
  # 消息缺省工作日，0 为周一，6 为周日
  default_weekdays: [0, 1, 2, 3, 4]
# 运行及监测选项
runtime:
  # 演练只读表格；确认测试成功后设为 false 允许报名
  dry_run: true
  # QQ 轮询间隔，秒
  poll_seconds: 1
  # 本地时区
  timezone: Asia/Shanghai
  # 去重状态位置，其余日程和记录保存在同目录
  state_file: data/seen_messages.json
  # 日志位置
  log_file: logs/volunteer_bot.log
  # 非持续监测下无空位达到此次数后屏蔽，0 禁用
  no_slot_retry_limit: 3
  # 打开失败的额外重试次数
  document_open_retries: 2
  # 持续监测未报名场次
  monitor_documents: true
  # 每轮检查结束后的等待秒数，0 表示立即继续
  document_poll_seconds: 0
  # 最后待报名场次开始前多少天结束监测
  document_deadline_days: 0
  # 允许跨表重叠报名并在日程中标红
  allow_cross_document_conflicts: true
  # 意外退出的表格线程重启等待秒数
  thread_restart_seconds: 2
  # 已结束监测的已知表格只读日程同步间隔，秒
  schedule_sync_seconds: 60
# QQ 可见窗口监听
qq:
  # QQ 进程名
  process_name: QQ.exe
  # 群号备用过滤，QQNT 可能不暴露群号
  groups: []
  # 填写目标群名，优先于群号过滤
  group_names: [请填写目标群名]
  # true 仅关闭群名过滤，仍只读取可见会话
  scan_all_groups: false
  # 可选窗口标题过滤
  message_window_title: ""
  # 允许申请加入 QQ 群或打开微信邀请
  auto_join_groups: false
  # 加群申请验证文字
  join_verification_message: ""
  # 是否扫描可见图片中的群二维码
  scan_qr_images: false
# Edge 与动态表格适配
tencent_docs:
  # 可选驱动路径，留空由 Selenium 处理
  driver_path: ""
  # 本机 Edge 调试地址
  debugger_address: 127.0.0.1:9222
  # 端口未运行时自动启动独立 Edge 会话
  auto_start_debugger: true
  # 独立会话目录，留空使用本机 LocalAppData 下的专用目录
  debug_user_data_dir: ""
  # 非调试模式下的浏览器用户目录
  user_data_dir: ""
  # 页面及粘贴确认超时，秒
  page_timeout: 27
  # 当前 WPS 表格就绪元素
  ready_selector: '#edit_proxy_runs'
  # 保存方式，适配 WPS 自动保存
  submit_mode: autosave
  # 默认覆盖资料残缺行；false 保留任何已填写个人字段的行
  overwrite_incomplete_rows: true
  # 页面字段校验错误提示
  validation_error_patterns: [请输入正确的身份证号码, 请输入正确的手机号]
  # 动态表头及单元格定位
  selectors:
    # 单元格名称框
    name_box: '.edit-box'
    # 编辑代理
    editor: '#edit_proxy_runs'
    # 按目标表调整表头行
    header_row: 2
    # 数据开始行
    data_start_row: 3
    # 扫描数据行数
    scan_max_rows: 200
    # 表头搜索列
    header_scan_columns: [A, B, C, D, E, F, G, H, I]
    # 字段别名，可在 GUI 修改
    header_aliases:
      # 姓名列
      name: [姓名, 名字]
      # 学院列
      college: [学院, 院系, 专业]
      # 学号列
      student_id: [学号]
      # 联系方式列
      phone: [联系方式, 手机号, 手机, 电话]
      # 点位列
      location: [点位, 地点, 岗位]
      # 星期列
      weekday: [星期, 周几, 星期几]
      # 时段列
      time: [时间, 时段]
      # 开始时间列
      start: [开始时间, 开始]
      # 结束时间列
      end: [结束时间, 结束]
      # 日期列
      date: [日期, 活动日期]
# 不可用时间
unavailable:
  # GUI 编辑周历规则
  weekly: []
  # 文本规则，例如 周一 18:00-21:00
  text_rules: []
  # 本机 ICS 文件路径
  ics_files: []
# 日程表界面显示范围
calendar:
  # 每日显示开始时间
  start_time: '08:00'
  # 每日显示结束时间，可用 24:00
  end_time: '23:00'
'''


def ensure_config(path: str | Path) -> None:
    path = Path(path)
    if not path.exists():
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")


def load_config(path: str | Path) -> dict[str, Any]:
    ensure_config(path)
    with Path(path).open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ensure_dirs(config: dict[str, Any]) -> None:
    runtime = config.setdefault("runtime", {})
    Path(runtime.get("state_file", "data/seen_messages.json")).parent.mkdir(parents=True, exist_ok=True)
    Path(runtime.get("log_file", "logs/volunteer_bot.log")).parent.mkdir(parents=True, exist_ok=True)
