"""Full-screen presentation; the existing Agent remains the sole execution backend."""
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from textual import work
from textual.binding import Binding
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Footer, Input, Markdown, RichLog, Static


class Trace(Message):
    def __init__(self, text: str):
        super().__init__()
        self.text = text


class Finished(Message):
    def __init__(self, answer: str, failed: bool = False):
        super().__init__()
        self.answer, self.failed = answer, failed


class SparkTUI(App):
    TITLE = 'GPU加速与数据分析'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [Binding('f1', 'help', '帮助', show=False),
                Binding('f2', 'details', '日志', show=False),
                ('ctrl+o', 'details', '执行详情'), ('f3', 'focus_file', '文件'),
                ('f5', 'settings', '设置'),
                Binding('f4', 'focus_prompt', '输入', show=False),
                Binding('escape', 'close_file', '返回', show=False),
                Binding('f10', 'safe_quit', '退出', show=False),
                ('ctrl+q', 'safe_quit', '退出'),
                Binding('ctrl+l', 'focus_prompt', '输入问题', show=False),
                Binding('ctrl+c', 'safe_quit', '退出', show=False, priority=True)]
    CSS = '''
    $surface: #191919;
    $ink: #e4ded6;
    $muted: #a49c93;
    $accent: #d99a76;
    Screen { background: $surface; color: $ink; padding: 0 2; }
    #brand { height: 2; padding-top: 1; color: $accent; text-style: bold; }
    #context { height: 1; color: $muted; }
    #body { height: 1fr; layout: vertical; }
    #sidebar { width: 1fr; height: 7; padding: 0 1; border: round #57504a; }
    .section { color: $accent; height: 1; }
    #file-path { width: 1fr; }
    #use-file, #file-hint, #session { display: none; }
    #dataset { height: 1; color: $muted; }
    #main { width: 1fr; height: 1fr; }
    #conversation { height: 1fr; padding: 1 0; scrollbar-size: 1 1; border: none; }
    #welcome { max-width: 84; }
    .question { height: auto; color: $ink; background: #292725; padding: 0 1; margin: 1 0; }
    .answer { height: auto; margin: 0 0 1 0; }
    Markdown { background: $surface; color: $ink; padding: 0; }
    Markdown .code_inline { color: $accent; background: $surface; text-style: bold; }
    MarkdownH1, MarkdownH2, MarkdownH3 { color: $accent; text-style: bold; }
    #details { height: 8; padding: 0 1; background: #22211f; color: $muted; border-top: solid #57504a; }
    #status { height: 1; color: $muted; }
    #status.working { color: $accent; }
    #composer { height: 3; border-top: solid #57504a; border-bottom: solid #57504a; }
    #chevron { width: 2; height: 1; color: $accent; text-style: bold; }
    #prompt { width: 1fr; height: 1; border: none; padding: 0; }
    #send { display: none; }
    Input { background: $surface; color: $ink; border: tall #57504a; padding: 0 1; }
    Input:focus { border: tall $accent; }
    #prompt:focus { border: none; }
    #prompt:disabled { opacity: 60%; }
    Footer { background: $surface; color: $muted; }
    Footer > .footer--key { background: $surface; color: $muted; }
    Footer > .footer--description { background: $surface; color: $muted; }
    Footer > .footer--highlight { background: #34302c; }
    .tiny { padding: 0 1; }
    .tiny #context { height: 1; }
    '''

    def __init__(self, agent, model: str, initial_file: str = '', record_details: bool = True,
                 connection=None, agent_factory=None, persist_config=None, force_setup=False,
                 model_loader=None):
        super().__init__()
        self.agent, self.model = agent, model
        self.selected_file = ''
        self.initial_file = initial_file
        self.record_details = record_details
        self.connection, self.agent_factory = connection, agent_factory
        self.persist_config, self.force_setup = persist_config, force_setup
        self.model_loader = model_loader
        self.busy = False
        self.quit_pending = False
        self.started = 0.0
        self.elapsed = 0.0
        self.phase = '待命'
        self.engine = '尚未执行'
        self.turns = 0
        # Worker threads post messages; they never mutate UI widgets directly.
        self.agent.event_sink = lambda text: self.post_message(Trace(text))

    def compose(self) -> ComposeResult:
        yield Static('✳  GPU加速与数据分析', id='brand', markup=False)
        yield Static(self.context_text(), id='context', markup=False)
        with Horizontal(id='body'):
            with Vertical(id='sidebar'):
                yield Static('选择数据文件  ·  Enter 确认 / Esc 返回', classes='section', markup=False)
                yield Input(placeholder='/home/Developer/data.csv', id='file-path')
                yield Button('[ 加载 ]', id='use-file')
                yield Static('文件：未选择\n大小：—', id='dataset', markup=False)
                yield Static('F3 选择文件\nEnter 确认路径\nTab 切换区域', id='file-hint', markup=False)
                yield Static(f'当前模型 · {self.model or "未配置"}', id='session', markup=False)
            with Vertical(id='main'):
                with VerticalScroll(id='conversation'):
                    yield Markdown('### 从一个问题开始。\n\n`/file` 选择本机文件 · `/settings` 连接设置\n\n例如：按地区比较收入，找出异常值，并导出报告。\n\n执行时显示实际 CPU / GPU 引擎；详细过程按 Ctrl+O 查看。', classes='answer', id='welcome')
                yield RichLog(id='details', wrap=True, markup=False, max_lines=500)
        yield Static('待命 · 引擎尚未执行', id='status', markup=False)
        with Horizontal(id='composer'):
            yield Static('❯', id='chevron', markup=False)
            yield Input(placeholder='输入分析问题，或 /help', id='prompt')
            yield Button('[ 执行 ]', id='send')
        yield Footer()

    def on_mount(self):
        self.query_one('#sidebar').display = False
        self.query_one('#details').border_title = '执行日志 · F2 收起'
        self.query_one('#details').display = False
        self.clock = self.set_interval(0.25, self.refresh_status)
        if self.initial_file:
            self.query_one('#file-path', Input).value = self.initial_file
            self.select_file()
        self.action_focus_prompt()
        self.adapt_layout(self.size.width)
        if self.connection is not None and (self.force_setup or not self.connection.skip_setup):
            self.call_after_refresh(self.action_settings)

    def context_text(self):
        host = urlsplit(self.connection.base_url).netloc if self.connection else ''
        dataset = Path(self.selected_file).name if self.selected_file else '未选择文件'
        return '  ·  '.join(part for part in (self.model or '未配置模型', host, dataset) if part)

    def on_resize(self, event):
        self.adapt_layout(event.size.width)

    def adapt_layout(self, width):
        self.set_class(width < 100, 'compact')
        self.set_class(width < 65, 'tiny')

    def refresh_status(self):
        # A timer event may already be queued when terminal teardown removes widgets.
        statuses = list(self.query('#status'))
        if not statuses:
            return
        seconds = time.monotonic() - self.started if self.started else self.elapsed
        elapsed = f' · {seconds:.1f}s' if seconds else ''
        pulse = ('✳', '✻', '✽', '✻')[int(time.monotonic() * 3) % 4] if self.busy else '·'
        statuses[0].set_class(self.busy, 'working')
        statuses[0].update(f'{pulse} {self.phase} · {self.engine}{elapsed}')

    def on_unmount(self):
        if hasattr(self, 'clock'):
            self.clock.stop()

    def select_file(self):
        value = self.query_one('#file-path', Input).value.strip()
        try:
            path = Path(value).expanduser().resolve()
            if not value or not path.is_file():
                raise ValueError('请填写服务器上已有的数据文件路径')
            size = path.stat().st_size
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity='error')
            return
        self.selected_file = str(path)
        self.query_one('#dataset', Static).update(f'{path.name}  ·  {size / 1e6:.1f} MB')
        self.query_one('#context', Static).update(self.context_text())
        self.query_one('#sidebar').display = False
        self.action_focus_prompt()

    async def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == 'file-path':
            self.select_file()
        else:
            await self.submit_question()

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == 'use-file':
            self.select_file()
        elif event.button.id == 'send':
            await self.submit_question()

    async def submit_question(self):
        if self.busy:
            return
        widget = self.query_one('#prompt', Input)
        question = widget.value.strip()
        if not question:
            return
        if question.startswith('/'):
            command, _, argument = question.partition(' ')
            if command == '/file':
                widget.value = ''
                if argument.strip():
                    self.query_one('#file-path', Input).value = argument.strip()
                    self.select_file()
                else:
                    self.action_focus_file()
            elif command in ('/help', '/logs', '/settings'):
                widget.value = ''
                {'/help': self.action_help, '/logs': self.action_details,
                 '/settings': self.action_settings}[command]()
            elif command == '/quit':
                self.action_safe_quit()
            else:
                # Absolute paths may begin with '/'; keep them as normal analysis input.
                if '/' not in command[1:] and not Path(command).is_file():
                    self.notify('未知命令。可用：/file、/settings、/logs、/help、/quit', severity='warning')
                    return
                # A path-only prompt is ambiguous; let the existing agent interpret it.
                command = ''
            if command:
                return
        if question.lower() in ('exit', 'quit'):
            self.action_safe_quit()
            return
        self.busy = True
        self.started = time.monotonic()
        self.elapsed = 0.0
        self.phase = '正在思考'
        self.engine = '等待实际执行'
        widget.value = ''
        widget.disabled = True
        self.query_one('#send', Button).disabled = True
        self.query_one('#file-path', Input).disabled = True
        self.query_one('#use-file', Button).disabled = True
        self.query_one('#welcome').display = False
        conversation = self.query_one('#conversation', VerticalScroll)
        self.query_one('#sidebar').display = False
        await conversation.mount(Static('❯ ' + question, classes='question', markup=False))
        conversation.scroll_end(animate=False)
        prompt = f'数据文件：{self.selected_file}\n用户问题：{question}' if self.selected_file else question
        self.analyze(prompt)

    @work(thread=True, exit_on_error=False)
    def analyze(self, prompt):
        try:
            answer = self.agent.run(prompt)
            failed = answer.startswith(('[model call failed]', '[tool round limit'))
        except Exception as exc:
            answer, failed = f'分析失败：{type(exc).__name__}: {exc}', True
        self.post_message(Finished(answer, failed))

    def on_trace(self, message: Trace):
        text = message.text
        if self.record_details:
            self.query_one('#details', RichLog).write(text)
        match = re.search(r'-> ([a-z_]+)\(', text)
        if match:
            self.phase = '执行 · ' + match.group(1)
        if 'engine=cudf' in text:
            self.engine = 'GPU · cuDF'
        elif 'engine=pandas' in text or 'CPU BY CHOICE' in text:
            self.engine = 'CPU · pandas'
        if 'FALLBACK' in text:
            self.engine = 'CPU 回退 · 查看执行详情'
        if 'OK' in text:
            self.phase = '整理结果'
        if 'FAILED' in text or '[api error]' in text:
            self.phase = '遇到问题 · 正在处理'
        self.refresh_status()

    async def on_finished(self, message: Finished):
        await self.query_one('#conversation', VerticalScroll).mount(Markdown(message.answer, classes='answer'))
        self.query_one('#conversation', VerticalScroll).scroll_end(animate=False)
        self.busy = False
        self.turns += 1
        self.phase = '分析失败' if message.failed else '分析完成'
        for selector in ('#prompt', '#send', '#file-path', '#use-file'):
            self.query_one(selector).disabled = False
        self.query_one('#session', Static).update(f'当前模型 · {self.model or "未配置"}\n{self.turns} 次分析')
        # Freeze elapsed time at completion; no fake progress percentage.
        self.elapsed = time.monotonic() - self.started
        self.started = 0
        self.refresh_status()
        self.action_focus_prompt()
        if self.quit_pending:
            self.exit()

    def action_details(self):
        widget = self.query_one('#details')
        widget.display = not widget.display

    def action_focus_prompt(self):
        self.query_one('#sidebar').display = False
        self.query_one('#prompt', Input).focus()

    def action_focus_file(self):
        if not self.busy:
            self.query_one('#sidebar').display = True
            self.query_one('#file-path', Input).focus()

    def action_close_file(self):
        self.action_focus_prompt()

    def action_help(self):
        self.notify('/file 路径：选择本机文件（SSH 时指服务器文件，不上传）\n/settings 或 F5：修改 API 地址、密钥与模型\n/logs 或 Ctrl+O：执行详情\nEnter：发送 · F3：文件 · Esc：返回 · Ctrl+Q：安全退出\n分析运行时退出会等待任务完成与内存清理。', title='GPU加速与数据分析 · 帮助', timeout=12)

    def action_settings(self):
        if self.busy:
            self.notify('请等待当前分析完成，再修改连接设置。')
            return
        if self.connection is None or self.agent_factory is None:
            self.notify('此嵌入式界面未提供连接配置；请从 agent_main.py 启动。')
            return
        from api_setup import APISetup
        from api_config import save_config
        kwargs = {'model_loader': self.model_loader} if self.model_loader else {}
        self.push_screen(APISetup(self.connection, self.persist_config or save_config, **kwargs),
                         self.apply_connection)

    def apply_connection(self, config):
        if config is None:
            self.action_focus_prompt()
            return
        changed = (config.base_url, config.api_key, config.model) != (
            self.connection.base_url, self.connection.api_key, self.connection.model)
        if changed:
            old_client = getattr(self.agent, 'client', None)
            self.agent = self.agent_factory(config)
            self.agent.event_sink = lambda text: self.post_message(Trace(text))
            if old_client:
                old_client.close()
            self.phase, self.engine, self.elapsed = '待命', '尚未执行', 0.0
            self.notify('连接已切换。旧对话仍可查看，但不会发送给新的服务。')
        self.connection, self.model = config, config.model
        self.query_one('#context', Static).update(self.context_text())
        self.refresh_status()
        self.action_focus_prompt()

    def action_safe_quit(self):
        if self.busy:
            # Python thread cancellation cannot safely stop a model call or CUDA operation.
            self.quit_pending = True
            self.notify('当前分析结束、释放会话内存后退出。')
        else:
            self.exit()
