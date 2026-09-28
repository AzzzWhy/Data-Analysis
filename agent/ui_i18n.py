"""UI-only translations. Model answers and raw execution logs are never rewritten."""
STRINGS = {
    'GPU加速与数据分析': 'GPU Acceleration & Data Analysis',
    '选择数据文件  ·  Enter 确认 / Esc 返回': 'Choose data file  ·  Enter to confirm / Esc to return',
    '[ 加载 ]': '[ Load ]', '[ 执行 ]': '[ Run ]',
    '文件：未选择\n大小：—': 'File: not selected\nSize: —',
    'F3 选择文件\nEnter 确认路径\nTab 切换区域': 'F3 Choose file\nEnter Confirm path\nTab Change focus',
    '当前模型': 'Current model', '未配置': 'Not configured', '未配置模型': 'No model configured',
    '未选择文件': 'No file selected', '次分析': 'analyses',
    'welcome': ('### Start with a question.\n\n/file · Choose a local file   /settings · Connection settings'
                '   /language zh · 中文\n\nExample: Compare revenue by region, find outliers, and export a report.'
                '\n\nThe status shows the actual CPU / GPU engine. Press Ctrl+O for execution details.'),
    'shortcuts': 'Ctrl+Q Exit   Ctrl+O Details   F3 File   F5 Settings   F6 中文',
    '输入分析问题，或 /help': 'Ask about your data, or type /help',
    '执行日志 · F2 收起': 'Execution log · F2 to hide',
    '待命': 'Ready', '尚未执行': 'Not run yet', '正在思考': 'Thinking',
    '等待实际执行': 'Waiting for execution', '执行': 'Running',
    '整理结果': 'Preparing answer', '遇到问题 · 正在处理': 'Handling an error',
    '分析失败': 'Analysis failed', '分析完成': 'Analysis complete',
    'CPU 回退 · 查看执行详情': 'CPU fallback · see execution details',
    '请填写本机已有的数据文件路径': 'Enter the path of an existing file on the agent machine.',
    '文件无法访问，请检查路径与权限。': 'Cannot access the file. Check its path and permissions.',
    '未知命令。可用：/file、/settings、/language、/logs、/help、/quit':
        'Unknown command. Use /file, /settings, /language, /logs, /help, or /quit.',
    'help': ('/file PATH: choose a file on the agent machine (SSH: server file; no upload)\n'
             '/settings or F5: API URL, key, model and language\n'
             '/language en | zh or F6: UI language\n'
             '/logs or Ctrl+O: execution details\n'
             'Enter: send · F3: file · Esc: return · Ctrl+Q: safe exit\n'
             'Exit during analysis waits for completion and memory cleanup.'),
    '帮助': 'Help', '日志': 'Logs', '执行详情': 'Details', '文件': 'File', '设置': 'Settings',
    '输入': 'Input', '返回': 'Return', '退出': 'Exit', '输入问题': 'Question', '提示': 'Notice',
    '请等待当前分析完成，再修改连接设置。': 'Wait for the current analysis before changing the connection.',
    '此嵌入式界面未提供连接配置；请从 agent_main.py 启动。':
        'Connection settings are unavailable in this embedded view. Start agent_main.py.',
    '连接已切换。旧对话仍可查看，但不会发送给新的服务。':
        'Connection changed. Earlier answers remain visible but are not sent to the new service.',
    '当前分析结束、释放会话内存后退出。': 'Exit will follow analysis completion and session-memory cleanup.',
    '语言偏好无法保存，请检查配置目录权限。': 'Cannot save the language preference. Check config-directory permissions.',
    '语言已切换。': 'Language changed.',
    '用法：/language zh 或 /language en；不带参数则切换语言。':
        'Usage: /language zh or /language en; without an argument, toggle the language.',
    '连接设置': 'Connection settings', '界面语言 / UI language': 'UI language / 界面语言',
    '支持 OpenAI 兼容接口。密钥只发送到你填写的地址；模型须支持工具调用。':
        'OpenAI-compatible APIs only. Keys go to the selected address; the model must support tool calls.',
    'API 基础地址（含 /v1 或服务指定的前缀）': 'API base URL (include /v1 or your provider\'s prefix)',
    'API Key（隐藏输入；本机无鉴权服务可填 local）': 'API key (masked; use local for unauthenticated local services)',
    '读取此地址的模型列表': 'Fetch models from this address',
    '先读取列表，再选择模型': 'Fetch the list, then select a model',
    '模型名（选择后填入；服务不支持列表时可手填）': 'Model ID (select from the list, or enter manually)',
    '以后不再弹出启动设置': 'Do not show setup on future launches',
    '保存密钥到本机明文文件（请勿共享）': 'Save key in a local plaintext file (do not share)',
    '默认仅保存地址、模型与提示偏好，密钥只在本次进程中使用。':
        'By default only URL, model and preferences are saved; the key stays in this process.',
    '保存并进入': 'Save and continue', '暂时跳过': 'Skip for now', '忽略，不再提示': 'Ignore; do not ask again',
    '正在读取模型列表…（不会发送分析数据）': 'Fetching models… (no analysis data is sent)',
    '请填写 API 地址、密钥并选择或填写模型名。': 'Enter the API URL, key, and a selected or manually entered model ID.',
    '配置无法保存。请检查本机配置目录的写入权限。': 'Cannot save settings. Check config-directory write permissions.',
    '读取失败：请检查地址、密钥或网络；服务不支持 /models 时可手填模型名。':
        'Fetch failed: check URL, key and network. If /models is unavailable, enter a model ID manually.',
    '地址或密钥已变更，请重新读取模型列表。': 'URL or key changed. Fetch the model list again.',
    'models_loaded': 'Fetched {count} models. Select one or enter an ID; listing does not verify tool-call support.',
    '请输入不带账号、查询参数的 API 基础地址，例如 https://example.com/v1':
        'Enter an API base URL without credentials or query parameters, e.g. https://example.com/v1',
    '远程 API 必须使用 HTTPS；本机服务可使用 http://localhost':
        'Remote APIs require HTTPS; local services may use http://localhost.',
    'API 地址的端口无效': 'Invalid port in the API URL.',
    '[model call failed] 尚未配置 API 地址、密钥与模型。请输入 /settings 打开连接设置。':
        '[model call failed] API URL, key and model are not configured. Use /settings.',

    # Browser workbench chrome. Kept here rather than in app.js so the terminal and the browser
    # translate from one table; two vocabularies is how they drift apart. These label the
    # interface only -- model prose and raw engine values are never rewritten through this table.
    '数据源': 'Data sources', '分析计划': 'Analysis plan', '计算会话': 'Compute session',
    '分析工作台': 'Analysis workspace', '分析结果': 'Analysis results', '运行分析': 'Run analysis',
    '运行方式': 'Run mode', '分析类型': 'Analysis type', '聚合规则': 'Aggregation',
    '分析目标': 'Analysis goal', '运行任务': 'Run task', '执行记录': 'Execution log',
    '保存设置': 'Save settings', '强制 CPU': 'Force CPU', '强制 GPU': 'Force GPU',
    '问题': 'Question', '分析': 'Analyze',
    '即时分析': 'One-shot analysis', '驻留会话': 'Resident session',
    '生成报告': 'Generate report', '发现数据': 'Discover data',
    '打开': 'Open', '关闭': 'Close', '查看': 'List', '自动判断': 'Auto',
    '数据概览': 'Profile', '统计摘要': 'Summary', '分组聚合': 'Group by',
    '相关性': 'Correlation',
    '数据文件': 'Data files', '会话': 'Session', '状态': 'Status', '会话数': 'Sessions',
    '步数': 'Steps', '占用': 'Held', '保留帧': 'Warm frames', '释放会话': 'Release session',
    '计划': 'Plan', '类型': 'Kind', '进度': 'Progress',
    '未设定分析目标，因此没有计划': 'No goal was set, so there is no plan.',
    '计划外步骤': 'off-plan step',
    '提问': 'Ask', '发送': 'Send', '直接工具调用': 'Direct tool call',
    '工具': 'Tool', '会话操作': 'Session operation', '分析操作': 'Operation',
    '分组列': 'Group by', '聚合': 'Aggregate', '输出': 'Output', '结果': 'Result',
    '设置': 'Settings', '连接设置': 'Connection settings', 'API 地址': 'API base URL',
    '模型': 'Model', 'API 密钥': 'API key', '取消': 'Cancel', '保存': 'Save',
    '执行详情': 'Execution details', '收起': 'hide', '展开': 'show',
    '扫描行数': 'Rows scanned', '引擎': 'Engine', '本次耗时': 'This run',
    '纯计算': 'Compute only', '离群点': 'Outliers', '跨轮复用': 'Resident reuse',
    '分组数': 'Groups', '驻留内存': 'Resident', '打开报告': 'Open report',
    '未选择数据文件': 'No dataset selected', '运行失败': 'Run failed',
    'active（会话打开中）': 'active (session open)', 'cold / released': 'cold / released',
    'worker 未响应': 'worker not responding',
    '目标（可选，仅 open 用）': 'Goal (optional, open only)',
    '例如：找出 revenue 离群点的成因': 'e.g. find the cause of the revenue outliers',
    '本机不可观测（需 cuDF）': 'Not observable here (needs cuDF)',
    '暂无产物': 'No artifacts yet', '暂无结果表': 'No result tables yet',
    '留空表示不修改': 'Leave empty to keep unchanged',

    # The chart viewer and the key row. Tab labels are matched against the artifact names the report
    # generator actually writes (groupby_bar / groupby_line / corr_heatmap / outliers_bar), so a kind
    # with no file gets no tab and no label is ever shown for a chart that does not exist.
    '折线': 'Line', '柱状': 'Bar', '热力': 'Heat', '直方': 'Histogram', '散点': 'Scatter',
    '图表类型': 'Chart type', '语言': 'Language', '关闭弹窗': 'Close dialog', '提交': 'Submit',
    '直接工具调用（不经过模型）': 'Direct tool call (no model)',

    # Added when the browser sweep learned to reach #phase, <option> and <title>: every string
    # below was already reaching the screen untranslated under language=en, so this is not new
    # vocabulary -- it is the part of the interface the table had never covered.
    'GPU加速与数据分析 · 工作台': 'GPU Acceleration & Data Analysis · Workbench',
    '执行工具': 'Running a tool', '完成': 'Done', '提交中': 'Submitting',
    '连接中断': 'Connection lost',
    '模型客户端不可用：': 'Model client unavailable: ', '未知原因': 'unknown reason',
    '安装：pip install -r requirements.txt\n在此之前，下方“直接工具调用”仍然真实可用，提问区不可用。':
        'Install it with: pip install -r requirements.txt\n'
        'Until then the direct tool calls below still run for real; only the question box is closed.',
    '模型客户端已安装，但没有配置 API 地址 / 密钥 / 模型。':
        'The model client is installed, but no API base URL / key / model is configured.',
    # The precise version: /api/state names the fields it found empty, so the sentence is assembled
    # from facts instead of guessing that all three are missing. Field labels above are reused, and
    # only the names ever reach the screen -- never a value.
    '模型客户端已安装，但还缺少：{fields}':
        'The model client is installed, but these are still missing: {fields}',
    '已保存，但提问框还不可用，缺少：{fields}':
        'Saved, but the question box is still unavailable; missing: {fields}',
    '已保存，提问框已可用。': 'Saved. The question box is now available.',
    '已保存。': 'Saved. ', '未知项': 'an unknown item',
    '未保存：': 'Not saved: ',
    '分析引擎未响应：': 'Analysis engine not responding: ',
    '引擎与模型客户端均就绪。': 'Engine and model client both ready.',
    '提问会把选中的文件路径作为一行上下文附在问题后面，日志里会显示模型实际收到的原文。':
        'Your question is sent with the selected file path as one line of context, and the log '
        'shows exactly what the model received.',
    '提问需要模型客户端（pip install -r requirements.txt）。下面「直接工具调用」不需要它。':
        'Asking needs the model client (pip install -r requirements.txt). '
        'The direct tool calls below do not.',
    '共 {total} 个可分析文件，这里按大小列出前 {shown} 个':
        '{total} analysable files found; the {shown} largest are listed here',
    '共 {total} 个可分析文件，按大小排序': '{total} analysable files found, sorted by size',
    '没有可分析的数据文件': 'No analysable data files',
    'analyze_dataset（单次，无驻留）': 'analyze_dataset (one shot, nothing resident)',
    'dataset_session（驻留会话）': 'dataset_session (resident session)',
    'export_deliverables（出报告与图表）': 'export_deliverables (report and charts)',
    'list_datasets（发现数据）': 'list_datasets (discover data)',
}

ZH_SPECIAL = {
    'welcome': ('### 从一个问题开始。\n\n/file · 选择本机文件   /settings · 连接设置'
                '   /language en · English\n\n例如：按地区比较收入，找出异常值，并导出报告。'
                '\n\n执行时显示实际 CPU / GPU 引擎；详细过程按 Ctrl+O 查看。'),
    'shortcuts': 'Ctrl+Q 退出   Ctrl+O 执行详情   F3 文件   F5 设置   F6 English',
    'help': ('/file 路径：选择本机文件（SSH 时指服务器文件，不上传）\n'
             '/settings 或 F5：API 地址、密钥、模型与语言\n'
             '/language zh | en 或 F6：切换界面语言\n'
             '/logs 或 Ctrl+O：执行详情\n'
             'Enter：发送 · F3：文件 · Esc：返回 · Ctrl+Q：安全退出\n'
             '分析运行时退出会等待任务完成与内存清理。'),
    'models_loaded': '已读取 {count} 个模型。请选择一个，或保留手填模型名；列表不代表工具调用已验证。',
}


def tr(language, key, **values):
    text = STRINGS.get(key, key) if language == 'en' else ZH_SPECIAL.get(key, key)
    return text.format(**values) if values else text


def status_text(language, value):
    if value.startswith('执行 · '):
        return tr(language, '执行') + value[len('执行'):]
    return tr(language, value)
