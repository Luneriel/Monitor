"""多语言支持（中文 / English）。

用法：
    from ..i18n import tr
    button = TextButton(tr("home.add"), ...)

切换语言后调用 ``i18n.set_language(code)``，界面由 MonitorApp 重建以应用新文案。
"""

from __future__ import annotations

LANGUAGE_ZH = "zh"
LANGUAGE_EN = "en"

STRINGS: dict[str, dict[str, str]] = {
    # ---------------- 通用 ----------------
    "common.cancel": {"zh": "取消", "en": "Cancel"},
    "common.confirm": {"zh": "确认", "en": "OK"},
    "common.delete": {"zh": "删除", "en": "Delete"},
    "common.confirm_delete": {"zh": "确认删除", "en": "Delete"},
    "common.select_all": {"zh": "全选", "en": "Select all"},
    "common.unselect_all": {"zh": "取消全选", "en": "Clear selection"},
    "common.close": {"zh": "关闭", "en": "Close"},
    "common.search": {"zh": "搜索", "en": "Search"},
    "common.click_to_rename": {"zh": "点击可重命名", "en": "Click to rename"},
    "common.running": {"zh": "运行中", "en": "Running"},
    "common.unknown": {"zh": "—", "en": "—"},

    # ---------------- 导航 / 窗口 ----------------
    "nav.home": {"zh": "首页", "en": "Home"},
    "nav.logs": {"zh": "日志", "en": "Logs"},
    "nav.notify": {"zh": "通知", "en": "Notifications"},
    "nav.settings": {"zh": "设置", "en": "Settings"},
    "hint.home": {"zh": "被追踪程序的运行状态与用时", "en": "Runtime status and usage of tracked programs"},
    "hint.logs": {"zh": "所有历史运行记录", "en": "All historical run records"},
    "hint.notify": {"zh": "系统消息", "en": "System messages"},
    "hint.settings": {"zh": "启动、托盘与关闭行为", "en": "Startup, tray and close behaviour"},
    "nav.footer": {"zh": "正在追踪 {tracked}/{total} 个目标",
                   "en": "Tracking {tracked}/{total} target(s)"},
    "window.minimize": {"zh": "最小化", "en": "Minimize"},
    "window.maximize": {"zh": "最大化", "en": "Maximize"},
    "window.restore": {"zh": "向下还原", "en": "Restore"},
    "tray.tooltip": {"zh": "Monitor 正在追踪程序使用时长", "en": "Monitor is tracking program usage"},
    "tray.show": {"zh": "显示主窗口", "en": "Show window"},
    "tray.quit": {"zh": "退出", "en": "Quit"},

    # ---------------- 首页 ----------------
    "home.add": {"zh": "添加", "en": "Add"},
    "home.add.program": {"zh": "添加程序", "en": "Add program"},
    "home.add.site": {"zh": "添加网站", "en": "Add website"},
    "home.section.programs": {"zh": "程序", "en": "Programs"},
    "home.section.sites": {"zh": "网站", "en": "Websites"},
    "home.delete": {"zh": "删除", "en": "Delete"},
    "home.search_placeholder": {"zh": "搜索程序名称或路径", "en": "Search by name or path"},
    "home.sort": {"zh": "排序", "en": "Sort"},
    "home.sort.added": {"zh": "按添加时间", "en": "By added time"},
    "home.sort.name": {"zh": "按名称", "en": "By name"},
    "home.sort.last_run": {"zh": "按最后运行日期", "en": "By last run"},
    "home.sort.manual": {"zh": "手动排序", "en": "Manual order"},
    "home.sort.manual_hint": {"zh": "手动排序：按住卡片左侧手柄拖动排序",
                              "en": "Manual order: drag the handle on the left of a card"},
    "home.sort.asc": {"zh": "正序", "en": "Ascending"},
    "home.sort.desc": {"zh": "倒序", "en": "Descending"},
    "home.empty.title": {"zh": "还没有追踪程序", "en": "No tracked programs yet"},
    "home.empty.hint": {"zh": "点击左上角“添加”，选择要监控的可执行文件",
                        "en": "Click “Add” at the top-left and pick executables to monitor"},
    "home.empty.no_match": {"zh": "没有找到“{query}”", "en": "No match for “{query}”"},
    "home.empty.no_match_hint": {"zh": "试试其他名称或路径关键词", "en": "Try another name or path keyword"},
    "home.empty.nothing_to_delete": {"zh": "还没有可以删除的程序", "en": "Nothing to delete"},
    "card.not_run_yet": {"zh": "尚未运行", "en": "Not run yet"},
    "card.tracking_off": {"zh": "已关闭追踪", "en": "Tracking off"},
    "card.running": {"zh": "运行中 · 启动 {start} · 已用 {elapsed}",
                     "en": "Running · started {start} · elapsed {elapsed}"},
    "card.last_end": {"zh": "上次结束 {time}", "en": "Last ended {time}"},
    "card.no_logs": {"zh": "尚未记录到运行日志", "en": "No run logs yet"},

    # ---------------- 添加 / 删除弹窗 ----------------
    "dialog.add.title": {"zh": "添加追踪程序", "en": "Add tracked programs"},
    "dialog.add.subtitle": {"zh": "确认后加入列表并默认开启追踪",
                            "en": "They will be added to the list with tracking enabled"},
    "dialog.add.more": {"zh": "添加其他程序", "en": "Add more programs"},
    "dialog.add.selected": {"zh": "已选择 {count} 个程序，确认后加入列表并默认开启追踪",
                            "en": "{count} program(s) selected · they will be added with tracking enabled"},
    "dialog.add.all_existing": {"zh": "所选程序都已在追踪列表中（{count} 个已跳过）",
                                "en": "All selected programs are already tracked ({count} skipped)"},
    "dialog.add.none": {"zh": "还没有选择任何程序", "en": "No programs selected yet"},
    "dialog.delete.title": {"zh": "是否确认删除 {count} 个程序", "en": "Delete {count} program(s)?"},
    "dialog.delete.hint": {"zh": "该操作无法撤回 · 右侧滑块可选择保留日志",
                           "en": "This cannot be undone · use the switches to keep logs"},
    "dialog.delete.keep_logs": {"zh": "保留日志", "en": "Keep logs"},
    "dialog.delete.keep_logs_tip": {"zh": "保留该程序的使用日志", "en": "Keep this program's usage logs"},
    "dialog.delete_logs.title": {"zh": "是否确认删除 {count} 条运行日志",
                                 "en": "Delete {count} run record(s)?"},
    "dialog.delete_logs.hint": {"zh": "该操作无法撤回 · 删除后首页的时间信息会同步更新",
                                "en": "This cannot be undone · the Home page time info will update"},
    "dialog.pick.title": {"zh": "选择要追踪的程序", "en": "Select programs to track"},
    "dialog.pick.filter": {"zh": "可执行文件 (*.exe);;所有文件 (*.*)",
                           "en": "Executables (*.exe);;All files (*.*)"},
    "dialog.time.start": {"zh": "开始 {time}", "en": "Start {time}"},
    "dialog.time.end": {"zh": "结束 {time}", "en": "End {time}"},
    "dialog.sites.title": {"zh": "添加追踪网站", "en": "Add tracked websites"},
    "dialog.sites.subtitle": {"zh": "每行一个域名，确认后加入列表并默认开启追踪",
                              "en": "One domain per line · they will be tracked once confirmed"},
    "dialog.sites.hint": {"zh": "支持域名、通配与路径：bilibili.com、*.bilibili.com、"
                               "https://www.bilibili.com/video/*（每行一个）",
                          "en": "Domains, wildcards and paths are supported: bilibili.com, "
                                "*.bilibili.com, https://www.bilibili.com/video/* (one per line)"},
    "dialog.sites.placeholder": {"zh": "bilibili.com\nhttps://www.bilibili.com/*",
                                 "en": "bilibili.com\nhttps://www.bilibili.com/*"},
    "dialog.sites.ready": {"zh": "将添加 {count} 个网站", "en": "Will add {count} website(s)"},
    "dialog.sites.existing": {"zh": "{count} 个已在追踪列表中，将跳过",
                              "en": "{count} already tracked, will be skipped"},
    "dialog.sites.dropped": {"zh": "{count} 行无效或重复，将忽略",
                             "en": "{count} line(s) invalid or duplicated, will be ignored"},
    "dialog.sites.invalid": {"zh": "请至少填写一个有效域名", "en": "Enter at least one valid domain"},

    # ---------------- 日志页 ----------------
    "logs.mode.time": {"zh": "按时间排序", "en": "By time"},
    "logs.mode.program": {"zh": "按程序分类", "en": "By program"},
    "logs.view_tooltip": {"zh": "切换日志视图", "en": "Switch log view"},
    "logs.view_current": {"zh": "当前：{mode}（点击切换）", "en": "Current: {mode} (click to switch)"},
    "logs.no_program": {"zh": "无追踪程序", "en": "No tracked program"},
    "logs.choose_program": {"zh": "选择程序", "en": "Choose a program"},
    "logs.deleted_program": {"zh": "已删除的程序", "en": "Deleted program"},
    "logs.count": {"zh": "共 {count} 条记录", "en": "{count} record(s)"},
    "logs.page.label": {"zh": "第 {current} / {total} 页", "en": "Page {current} of {total}"},
    "logs.page.prev": {"zh": "上一页", "en": "Previous page"},
    "logs.page.next": {"zh": "下一页", "en": "Next page"},
    "logs.page.goto": {"zh": "点击输入页码", "en": "Click to type a page number"},
    "logs.selected": {"zh": "已选择 {selected} / {total} 条", "en": "Selected {selected} / {total}"},
    "logs.empty.title": {"zh": "暂无日志", "en": "No logs yet"},
    "logs.empty.hint": {"zh": "被追踪程序启动后会自动写入运行记录",
                        "en": "Run records are written automatically once a tracked program starts"},
    "logs.empty.no_program": {"zh": "无追踪程序", "en": "No tracked program"},
    "logs.empty.no_program_hint": {"zh": "先在首页添加要监控的可执行文件",
                                   "en": "Add an executable on the Home page first"},
    "logs.empty.program": {"zh": "“{name}”暂无日志", "en": "No logs for “{name}”"},
    "logs.empty.program_hint": {"zh": "该程序还没有产生运行记录", "en": "This program has no run records yet"},

    # ---------------- 通知页 ----------------
    "notify.empty.title": {"zh": "暂无通知", "en": "No notifications"},
    "notify.empty.hint": {"zh": "程序运行中的提醒会显示在这里",
                          "en": "Alerts from tracked programs will show up here"},

    # ---------------- 设置页 ----------------
    "settings.language": {"zh": "语言", "en": "Language"},
    "settings.language.desc": {"zh": "界面显示语言（中文 / English）", "en": "Interface language"},
    "settings.language.zh": {"zh": "中文", "en": "中文"},
    "settings.language.en": {"zh": "English", "en": "English"},
    "settings.theme": {"zh": "外观", "en": "Appearance"},
    "settings.theme.desc": {"zh": "浅色模式 / 深色模式 / 跟随系统", "en": "Light / Dark / Follow system"},
    "settings.theme.light": {"zh": "浅色模式", "en": "Light"},
    "settings.theme.dark": {"zh": "深色模式", "en": "Dark"},
    "settings.theme.system": {"zh": "跟随系统", "en": "Follow system"},
    "settings.autostart": {"zh": "开机自启动", "en": "Launch at startup"},
    "settings.autostart.desc": {"zh": "写入注册表 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run",
                                "en": "Write to the registry HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run"},
    "settings.silent": {"zh": "静默启动", "en": "Silent start"},
    "settings.silent.desc": {"zh": "启动时隐藏主窗口与任务栏图标，仅保留托盘图标",
                             "en": "Start hidden with only a tray icon (no taskbar entry)"},
    "settings.admin": {"zh": "以管理员身份启动", "en": "Start as administrator"},
    "settings.admin.desc": {"zh": "创建登录触发的计划任务（最高权限启动，不弹 UAC）。需要 Monitor 以管理员身份运行才能写入",
                            "en": "Create a logon scheduled task with highest privileges (no UAC prompt). Requires Monitor to run as administrator to write"},
    "settings.admin.need": {"zh": "「以管理员身份启动」需要管理员权限才能创建计划任务。\n\n现在以管理员身份重启 Monitor 吗？重启后该选项会自动生效。",
                            "en": "\"Start as administrator\" needs administrator rights to create the scheduled task.\n\nRestart Monitor as administrator now? The option takes effect automatically after restarting."},
    "settings.admin.failed": {"zh": "创建计划任务失败：{error}",
                              "en": "Failed to create the scheduled task: {error}"},
    "settings.grace": {"zh": "离开控制页后重新验证", "en": "Re-verify after leaving Control"},
    "settings.grace.desc": {"zh": "离开「控制」页超过该时长后，再进入需要重新输入密码；0 表示每次进入都要密码",
                            "en": "Time away from the Control page before the password is required again; 0 means every time"},
    "settings.grace.suffix": {"zh": " 分钟", "en": " min"},
    "settings.close_action": {"zh": "点击关闭程序时", "en": "When closing the window"},
    "settings.close_action.desc": {"zh": "点击窗口右上角 ✕ 后的行为",
                                   "en": "Behaviour when clicking ✕ at the top-right"},
    "settings.close_action.tray": {"zh": "隐藏到托盘图标", "en": "Hide to tray"},
    "settings.close_action.exit": {"zh": "关闭 Monitor", "en": "Quit Monitor"},
    "settings.tray_icon": {"zh": "托盘图标", "en": "Tray icon"},
    "settings.tray_icon.desc": {"zh": "支持 PNG / JPG / ICO；左键显示主窗口，右键弹出菜单",
                                "en": "PNG / JPG / ICO supported; left click shows the window, "
                                      "right click opens the menu"},
    "settings.tray_icon.change": {"zh": "更换图片", "en": "Change image"},
    "settings.tray_icon.reset": {"zh": "恢复默认", "en": "Reset"},
    "settings.tray_icon.pick": {"zh": "选择托盘图标", "en": "Choose a tray icon"},
    "settings.tray_icon.filter": {"zh": "图片 (*.png *.jpg *.jpeg *.ico *.bmp)",
                                  "en": "Images (*.png *.jpg *.jpeg *.ico *.bmp)"},
    "settings.tip": {"zh": "提示：删除程序时可以选择保留日志；保留日志的程序仍会出现在日志页"
                           "“按程序分类”的下拉菜单中。",
                     "en": "Tip: when deleting a program you may keep its logs; kept programs "
                           "remain available in the “By program” list on the Logs page."},

    # ---------------- 控制页 ----------------
    "nav.control": {"zh": "控制", "en": "Control"},
    "hint.control": {"zh": "使用限制、密码与遮罩", "en": "Usage limits, password and overlays"},
    "control.gate.title": {"zh": "控制功能已锁定", "en": "Control is locked"},
    "control.gate.hint": {"zh": "进入控制页需要先验证控制页密码。",
                          "en": "Enter the control password to continue."},
    "control.gate.enter": {"zh": "输入密码", "en": "Enter password"},
    "control.gate.placeholder": {"zh": "输入控制页密码", "en": "Enter the control password"},
    "control.gate.enter_hint": {"zh": "输入后按回车解锁", "en": "Press Enter to unlock"},
    "control.gate.unlock": {"zh": "解锁", "en": "Unlock"},
    "control.setup.placeholder": {"zh": "设置密码（至少 4 位）",
                                  "en": "Set a password (4+ characters)"},
    "control.gate.locked": {"zh": "密码错误次数过多，请 {seconds} 秒后再试。",
                            "en": "Too many wrong attempts. Try again in {seconds}s."},
    "control.setup.title": {"zh": "设置控制页密码", "en": "Set the control password"},
    "control.setup.hint": {"zh": "首次进入控制页需要先设置密码（至少 4 位）。该密码用于修改规则、"
                                 "删除规则、退出 Monitor、以管理员身份重启与临时解锁。",
                           "en": "Set a password (at least 4 characters) first. It is used to edit "
                                 "or delete rules, quit Monitor, restart as administrator, and unlock."},
    "control.setup.action": {"zh": "设置密码", "en": "Set password"},
    "control.verify.title": {"zh": "验证控制页密码", "en": "Verify control password"},
    "control.verify.hint": {"zh": "输入控制页密码以继续。", "en": "Enter the control password to continue."},
    "control.error.empty": {"zh": "请输入密码", "en": "Please enter a password"},
    "control.error.mismatch": {"zh": "两次输入的密码不一致", "en": "The two passwords do not match"},
    "control.error.wrong": {"zh": "密码错误", "en": "Wrong password"},
    "control.error.too_short": {"zh": "密码至少 4 位", "en": "At least 4 characters"},
    # 框内提示位置窄，长文案简化（倒计时仍在标题下方的 gate.locked 里显示）
    "control.error.locked": {"zh": "错误次数过多", "en": "Too many attempts"},
    "control.enable": {"zh": "启用控制", "en": "Enable control"},
    "control.enable.hint": {"zh": "关闭后所有规则暂停生效，所有遮罩立即消失（关闭不需要密码）。",
                            "en": "When off, all rules pause and overlays disappear (no password needed)."},
    "control.enable.need_password": {"zh": "关闭控制功能需要验证密码",
                                     "en": "Password required to turn control off"},
    "control.quit.need_password": {"zh": "退出 Monitor 需要验证密码（控制功能已启用）",
                                   "en": "Password required to quit Monitor (control is enabled)"},
    "control.enable.need_admin": {"zh": "「启用控制」需要管理员权限：遮罩要覆盖以管理员身份运行的程序（如原神）。\n\n现在以管理员身份重启 Monitor 吗？",
                                  "en": "\"Enable control\" needs administrator rights: overlays must cover programs running as administrator (e.g. Genshin Impact).\n\nRestart Monitor as administrator now?"},
    "control.change_password": {"zh": "修改密码", "en": "Change password"},
    "control.change_password.hint": {"zh": "输入新的控制页密码（至少 4 位）。",
                                     "en": "Enter the new control password (at least 4 characters)."},
    "control.minutes_suffix": {"zh": " 分钟", "en": " min"},
    "unit.s": {"zh": "秒", "en": "s"},
    "unit.m": {"zh": "分钟", "en": "min"},
    "unit.h": {"zh": "小时", "en": "h"},
    "unit.value": {"zh": "{value} {unit}", "en": "{value} {unit}"},
    "control.rules.title": {"zh": "程序 / 网站使用限制", "en": "Per-program / site limits"},
    "control.section.programs": {"zh": "程序", "en": "Programs"},
    "control.section.sites": {"zh": "网站", "en": "Websites"},
    "control.rules.empty": {"zh": "还没有可配置的程序或网站，请先在首页添加。",
                            "en": "No programs or websites yet — add one on the Home page first."},
    "control.rule.daily": {"zh": "每日使用上限", "en": "Daily limit"},
    "control.rule.streak_x": {"zh": "连续使用阈值", "en": "Continuous-use threshold"},
    "control.rule.streak_y": {"zh": "强制暂停时长", "en": "Forced pause duration"},
    "control.rule.edit": {"zh": "编辑", "en": "Edit"},
    "control.rule.cancel": {"zh": "收起", "en": "Close"},
    "control.rule.save": {"zh": "保存", "en": "Save"},
    "control.rule.delete": {"zh": "删除规则", "en": "Delete rule"},
    "control.summary.daily": {"zh": "每日 {amount}", "en": "Daily {amount}"},
    "control.summary.streak": {"zh": "连续 {x} 暂停 {y}",
                               "en": "Pause {y} after {x} continuous"},
    "control.summary.none": {"zh": "未配置规则", "en": "No rules configured"},
    "control.summary.blocked": {"zh": "限制生效中", "en": "Limit active"},
    "control.summary.unlocked": {"zh": "临时解锁中 剩 {time}",
                                 "en": "Unlocked, {time} left"},
    "control.rule.window": {"zh": "指定时间段无法使用", "en": "Blocked time slots"},
    "control.rule.window.hint": {
        "zh": "时段内该程序 / 网站完全不可用（显示限制遮罩）；跨午夜的时段按"
              "开始那天算（周三 22:00–07:00 会挡到周四早上），开始与结束相同表示整天。",
        "en": "During a slot the program / site is fully unavailable (blocked overlay). "
              "Slots crossing midnight belong to their start day (Wed 22:00–07:00 also "
              "covers Thursday morning); equal start and end means all day."},
    "control.window.add": {"zh": "添加时段", "en": "Add time slot"},
    "control.window.empty": {"zh": "还没有时段，点“添加时段”新建一条",
                             "en": "No time slots yet — use “Add time slot”"},
    "common.list_sep": {"zh": "、", "en": ", "},
    "control.window.remove": {"zh": "删除这个时段", "en": "Remove this slot"},
    "control.window.start": {"zh": "从", "en": "From"},
    "control.window.end": {"zh": "至", "en": "to"},
    "control.window.repeat": {"zh": "重复", "en": "On"},
    "control.window.overnight": {"zh": "跨午夜", "en": "crosses midnight"},
    "control.days.all": {"zh": "每天", "en": "every day"},
    "control.days.workdays": {"zh": "周一至周五", "en": "Mon–Fri"},
    "control.days.weekend": {"zh": "周末", "en": "weekends"},
    "control.days.custom": {"zh": "每周 {days}", "en": "every {days}"},
    "control.days.none": {"zh": "未选择星期", "en": "no days selected"},
    "control.day.mon": {"zh": "一", "en": "Mo"},
    "control.day.tue": {"zh": "二", "en": "Tu"},
    "control.day.wed": {"zh": "三", "en": "We"},
    "control.day.thu": {"zh": "四", "en": "Th"},
    "control.day.fri": {"zh": "五", "en": "Fr"},
    "control.day.sat": {"zh": "六", "en": "Sa"},
    "control.day.sun": {"zh": "日", "en": "Su"},
    "control.summary.window": {"zh": "时段 {ranges}", "en": "Slots {ranges}"},
    "control.summary.window_range": {"zh": "{start}-{end} {days}",
                                     "en": "{start}-{end} {days}"},
    "control.summary.window_used": {"zh": "时段中 剩 {time}",
                                    "en": "Blocked now, {time} left"},
    "control.rule.expand": {"zh": "展开设置", "en": "Expand settings"},
    "control.rule.collapse": {"zh": "收起设置", "en": "Collapse settings"},
    "common.drag_handle": {"zh": "按住拖动排序", "en": "Drag to reorder"},
    "overlay.window_title": {"zh": "当前时间不允许使用", "en": "Not available at this time"},
    "overlay.window_countdown": {"zh": "距离本时段结束 {time}",
                                 "en": "This time slot ends in {time}"},
    "control.unlock.title": {"zh": "临时解锁", "en": "Temporary unlock"},
    "control.unlock.hint": {"zh": "输入控制页密码后可选择预设时长或自定义分钟数；"
                                  "本次解锁结束前不能再次延长。",
                            "en": "Enter the control password, then pick a preset or custom "
                                  "duration. It cannot be extended until it ends."},
    "control.unlock.preset": {"zh": "{minutes} 分钟", "en": "{minutes} min"},
    "notify.duration.title": {"zh": "追踪时长通知", "en": "Tracked-duration notifications"},
    "notify.duration.empty": {"zh": "还没有时间值，点 + 号添加",
                              "en": "No values yet — use the + button"},
    "notify.duration.confirm": {"zh": "添加", "en": "Add"},
    "notify.custom.title": {"zh": "自定义通知内容", "en": "Custom notification text"},
    "notify.custom.hint": {"zh": "每个通知可以单独自定义；留空表示使用默认文案。"
                                 "可用占位符：{tokens}；写 \\n 表示换行，"
                                 "{{duration}} 是本次追踪时长。",
                           "en": "Customise each notification; leave empty for the default text. "
                                 "Placeholders: {tokens}; write \\n for a line break; "
                                 "{{duration}} is the duration of this tracking session."},
    "notify.test.need_windows": {"zh": "请先打开上面的“启用 Windows 通知”",
                                 "en": "Turn on “Enable Windows notifications” first"},
    "notify.test.need_wecom": {"zh": "请先打开“启用企业微信通知”并填写地址",
                               "en": "Turn on WeCom notifications and fill in the URL first"},
    "notify.test.failed": {"zh": "发送失败，请检查设置", "en": "Sending failed — check the settings"},
    "overlay.close_program": {"zh": "关闭程序", "en": "Close program"},
    "control.close.title": {"zh": "关闭程序", "en": "Close program"},
    "control.close.confirm": {"zh": "要结束“{name}”的所有进程吗？未保存的内容会丢失。",
                              "en": "Close all processes of “{name}”? Unsaved work will be lost."},
    "control.close.timeout": {"zh": "“{name}”没有响应，是否强制结束？",
                              "en": "“{name}” is not responding. Force close it?"},
    "control.setup.confirm_placeholder": {"zh": "再次输入密码", "en": "Repeat the password"},
    "control.add_program": {"zh": "添加程序 / 网站", "en": "Add program / site"},
    "control.add_program.hint": {"zh": "勾选要限制使用的程序或网站；已加入的不可重复选择。"
                                       "网站规则在浏览器当前显示该网站时生效：遮挡窗口并静音。",
                                 "en": "Tick the programs or websites to limit; already added ones "
                                       "are disabled. A website rule applies while the browser shows "
                                       "that site: the window is covered and muted."},
    "control.add_program.none": {"zh": "没有可添加的程序或网站（Monitor 自己不能被限制）。",
                                 "en": "Nothing left to add (Monitor itself cannot be limited)."},
    "control.rule.remove": {"zh": "删除规则", "en": "Delete rule"},
    "control.summary.used": {"zh": "今日 {used}", "en": "Used today {used}"},
    "control.summary.streak_used": {"zh": "连续 {used}", "en": "Continuous {used}"},
    "notify.independent.add_hint": {"zh": "勾选要单独配置通知的程序；已添加的不可重复选择。",
                                    "en": "Tick the programs to configure separately."},
    "notify.test.sent": {"zh": "测试通知已发送", "en": "Test notification sent"},
    "notify.test.sending": {"zh": "正在发送…", "en": "Sending…"},
    "notify.test.disabled": {"zh": "请先打开 Windows 通知或填写企业微信地址",
                             "en": "Enable Windows notifications or fill the WeCom URL first"},
    "control.log.daily_limit": {"zh": "“{name}”达到每日使用上限（{amount}）",
                                "en": "“{name}” reached the daily limit ({amount})"},
    "control.log.unlocked": {"zh": "“{name}”被临时解锁 {amount}",
                             "en": "“{name}” temporarily unlocked for {amount}"},
    "control.log.pause": {"zh": "“{name}”触发强制暂停（{amount}）",
                          "en": "“{name}” hit the forced pause ({amount})"},
    "control.log.window": {"zh": "“{name}”进入指定时间段（{amount} 后结束）",
                           "en": "“{name}” entered a blocked time slot (ends in {amount})"},

    # ---------------- 遮罩 ----------------
    "overlay.limit_title": {"zh": "使用受限", "en": "Usage limited"},
    "overlay.daily_title": {"zh": "今日使用额度已用完，明天 0 点后恢复",
                            "en": "Today's quota is used up; available again after midnight"},
    "overlay.daily_countdown": {"zh": "距离恢复还有 {time}", "en": "Resumes in {time}"},
    "overlay.streak_title": {"zh": "连续使用时间过长，请休息 {time}",
                             "en": "Continuous use too long — please rest {time}"},
    "overlay.streak_countdown": {"zh": "剩余暂停 {time}", "en": "Pause remaining {time}"},
    "overlay.unlock": {"zh": "临时解锁", "en": "Unlock temporarily"},
    "overlay.locked": {"zh": "密码错误次数过多，请 {seconds} 秒后再试。",
                       "en": "Too many wrong attempts. Try again in {seconds}s."},
    "overlay.admin_hint": {
        "zh": "该程序以管理员身份运行，遮罩只能覆盖整个屏幕。\n"
              "以管理员身份重启 Monitor 即可只遮挡该程序。",
        "en": "This program runs as administrator, so the overlay can only cover the "
              "whole screen.\nRestart Monitor as administrator to cover only this program."},
    # 紧凑遮罩：盖在画中画小窗上（窗口只有两三百像素，只能用最短的文案）
    "overlay.compact.daily_title": {"zh": "今日额度已用完", "en": "Quota used up"},
    "overlay.compact.window_title": {"zh": "当前时段不可用", "en": "Blocked now"},
    "overlay.compact.streak_title": {"zh": "连续使用超时", "en": "Rest limit"},
    "overlay.compact.unlock": {"zh": "解锁", "en": "Unlock"},
    "overlay.compact.close": {"zh": "关闭", "en": "Close"},

    # ---------------- 权限（管理员） ----------------
    "admin.restart": {"zh": "以管理员身份重启", "en": "Restart as administrator"},
    "admin.hint": {
        "zh": "部分游戏/程序以管理员身份运行（如原神）。以管理员身份重启 Monitor 后，"
              "遮罩才能只遮挡该程序本身、不会盖住其它窗口，也是「启用控制」的前置条件。",
        "en": "Some games run as administrator (e.g. Genshin Impact). Restart Monitor as "
              "administrator so the overlay covers only that program, and to be able to "
              "turn control on."},
    "admin.running": {"zh": "当前已以管理员身份运行", "en": "Running as administrator"},
    "admin.failed": {"zh": "提权请求被取消或失败。", "en": "The elevation request was cancelled or failed."},

    # ---------------- 通知 ----------------
    "notify.title": {"zh": "Monitor 通知", "en": "Monitor notification"},
    "notify.body.open": {"zh": "{time}\\n\\n“{program}”已启动",
                         "en": "{time}\\n\\n“{program}” started"},
    "notify.body.close": {"zh": "{time}\\n\\n“{program}”已关闭",
                          "en": "{time}\\n\\n“{program}” closed"},
    "notify.body.daily_limit": {"zh": "{time}\\n\\n“{program}”今日使用额度已用完",
                                "en": "{time}\\n\\n“{program}” has used up today's quota"},
    "notify.body.unlocked": {"zh": "{time}\\n\\n“{program}”被临时解锁{unlockTime}",
                             "en": "{time}\\n\\n“{program}” was temporarily unlocked{unlockTime}"},
    "notify.unlock.minutes": {"zh": "{minutes} 分钟", "en": "{minutes} minutes"},
    "notify.body.pause": {"zh": "{time}\\n\\n“{program}”连续使用时间过长，已强制暂停",
                          "en": "{time}\\n\\n“{program}” was paused after long continuous use"},
    "notify.body.duration": {"zh": "{time}\\n\\n“{program}”已使用{duration}",
                             "en": "{time}\\n\\n“{program}” used {duration}"},
    "notify.body.password_changed": {"zh": "{time}\\n\\n控制页密码已修改",
                                     "en": "{time}\\n\\nThe control password was changed"},
    "notify.test.body": {"zh": "这是一条测试通知", "en": "This is a test notification"},
    "notify.global.title": {"zh": "全局通知设置", "en": "Global notification settings"},
    "notify.global.hint": {"zh": "影响下方所有通知的设置；未单独配置的程序使用这里的设置。",
                           "en": "Applies to all notifications below; programs without their own "
                                 "settings use these."},
    "notify.event.open": {"zh": "程序打开时", "en": "When a program opens"},
    "notify.event.close": {"zh": "程序关闭时", "en": "When a program closes"},
    "notify.event.daily_limit": {"zh": "程序达到每日上限时", "en": "When the daily limit is reached"},
    "notify.event.unlocked": {"zh": "程序被临时解锁时", "en": "When temporarily unlocked"},
    "notify.event.pause": {"zh": "程序触发强制暂停时", "en": "When a forced pause triggers"},
    "notify.event.duration": {"zh": "程序追踪时长达到指定时长",
                              "en": "When tracked duration reaches a given length"},
    "notify.event.password_changed": {"zh": "控制页密码修改时",
                                      "en": "When the control password changes"},
    "notify.duration.add": {"zh": "添加一个时长条件", "en": "Add a duration condition"},
    "notify.duration.chip": {"zh": "{minutes} 分钟", "en": "{minutes} min"},
    "notify.duration.remove": {"zh": "点击移除该条件", "en": "Click to remove this condition"},
    "notify.independent.title": {"zh": "独立设置", "en": "Per-program settings"},
    "notify.independent.add": {"zh": "添加独立设置", "en": "Add per-program settings"},
    "notify.independent.item": {"zh": "{name} 的独立设置", "en": "Settings for {name}"},
    "notify.independent.item_hint": {"zh": "该程序的独立通知条件",
                                     "en": "Independent conditions for this program"},
    "notify.independent.remove": {"zh": "移除此独立设置", "en": "Remove this setting"},
    "notify.independent.not_in_control": {"zh": "该程序未加入控制页，因此第三至五项不可用。",
                                          "en": "Not in the Control page, so rows 3–5 are unavailable."},
    "notify.windows.title": {"zh": "启用 Windows 通知", "en": "Enable Windows notifications"},
    "notify.windows.hint": {"zh": "通过系统托盘气泡提示，游戏全屏时可能看不到",
                            "en": "Shown as a system tray balloon; may be hidden in fullscreen games"},
    "notify.wecom.title": {"zh": "启用企业微信通知", "en": "Enable WeCom notifications"},
    "notify.wecom.hint": {"zh": "企业微信通知相关设置（webhook 通道）",
                          "en": "WeCom notification settings (webhook channel)"},
    "notify.wecom.placeholder": {"zh": "https://qyapi.weixin.qq.com/...",
                                 "en": "https://qyapi.weixin.qq.com/..."},
    "notify.test.hint": {"zh": "发送测试通知", "en": "Send a test notification"},
    "notify.test.send": {"zh": "发送", "en": "Send"},

    # ---------------- 日志：按时间分类 ----------------
    "logs.mode.date": {"zh": "按时间分类", "en": "By date"},
    "logs.date.all": {"zh": "全部", "en": "All"},
    "logs.date.year": {"zh": "{year} 年", "en": "{year}"},
    "logs.date.month": {"zh": "{month} 月", "en": "{month}"},
    "logs.date.day": {"zh": "{day} 日", "en": "{day}"},
    "logs.date.empty": {"zh": "该时间段内没有记录", "en": "No records in this period"},
}

_current = LANGUAGE_ZH


def set_language(code: str) -> str:
    global _current
    _current = code if code in (LANGUAGE_ZH, LANGUAGE_EN) else LANGUAGE_ZH
    return _current


def current_language() -> str:
    return _current


def tr(key: str, **kwargs) -> str:
    """取当前语言的文案；缺失时回退到中文，再回退到 key 本身。"""
    entry = STRINGS.get(key)
    if not entry:
        return key
    text = entry.get(_current) or entry.get(LANGUAGE_ZH) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


def unit_label(unit: str) -> str:
    """单位代号（s / m / h）-> 当前语言的单位名（未知单位按分钟处理，与 unit_seconds 一致）。"""
    code = str(unit or "m")
    key = f"unit.{code}"
    if key not in STRINGS:
        key = "unit.m"
    return tr(key)


def format_amount(value, unit: str = "m") -> str:
    """把数值 + 单位拼成 "1 分钟" / "90 秒"（控制页的数值展示统一走这里）。"""
    number = float(value or 0)
    shown = int(number) if float(number).is_integer() else round(number, 1)
    return tr("unit.value", value=shown, unit=unit_label(unit))
