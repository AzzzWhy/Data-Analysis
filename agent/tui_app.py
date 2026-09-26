"""Full-screen presentation; the existing Agent remains the sole execution backend."""
from pathlib import Path
import platform
import re
import time

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
    TITLE = 'Data Analysis / GB10'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [('f1', 'help', '帮助'), ('f2', 'details', '日志'),
                ('f3', 'focus_file', '文件'), ('f4', 'focus_prompt', '输入'),
                ('f10', 'safe_quit', '退出'),
                Binding('ctrl+q', 'safe_quit', '退出', show=False),
                Binding('ctrl+l', 'focus_prompt', '输入问题', show=False),
                Binding('ctrl+c', 'safe_quit', '退出', show=False, priority=True)]
    CSS = '''
    $classic-blue: #000080;
    $classic-white: #c0c0c0;
    $classic-black: #000000;
    $classic-yellow: #ffff00;
    Screen { background: $classic-blue; color: $classic-white; }
    #brand { height: 1; padding: 0 1; background: $classic-white; color: $classic-black; text-style: bold; }
    #body { height: 1fr; }
    #sidebar { width: 30; padding: 0 1; background: $classic-blue; border: solid $classic-white; }
    .section { color: $classic-yellow; margin-bottom: 0; text-style: bold; }
    #dataset { height: auto; margin: 1 0; }
    #file-path { width: 1fr; }
    #use-file { width: 1fr; margin-top: 0; }
    #file-hint { height: auto; color: $classic-white; margin-top: 1; }
    #session { height: auto; color: $classic-white; margin-top: 1; }
    #main { width: 1fr; }
    #conversation { height: 1fr; padding: 0 1; border: solid $classic-white; scrollbar-size: 1 1; }
    .question { height: auto; color: $classic-yellow; margin: 1 0 0 0; }
    .answer { height: auto; margin: 0 0 1 0; }
    Markdown { background: $classic-blue; color: $classic-white; padding: 0; }
    Markdown .code_inline { color: #ffffff; background: $classic-blue; text-style: bold; }
    MarkdownH1, MarkdownH2, MarkdownH3 { color: #ffffff; text-style: bold; }
    #details { height: 8; padding: 0 1; background: $classic-black; color: $classic-white; border: solid $classic-white; }
    #status { height: 1; padding: 0 1; color: $classic-black; background: $classic-white; }
    #composer { height: 3; padding: 0; background: $classic-blue; }
    #prompt { width: 1fr; }
    #send { width: 10; margin-left: 0; }
    Input { border: solid $classic-white; background: $classic-blue; color: #ffffff; padding: 0 1; }
    Input:focus { border: solid $classic-yellow; }
    Button { background: $classic-blue; border: solid $classic-white; color: $classic-white; height: 3; }
    Button:focus, Button:hover { background: $classic-white; color: $classic-black; text-style: bold; }
    Footer { background: $classic-black; color: $classic-white; }
    Footer > .footer--key { background: $classic-white; color: $classic-black; text-style: bold; }
    Footer > .footer--description { background: $classic-black; color: $classic-white; }
    .compact #sidebar { width: 22; padding: 1; }
    .compact #conversation { padding: 0 1; }
    .tiny #body { layout: vertical; }
    .tiny #sidebar { width: 1fr; height: 4; padding: 0; border: none; }
    .tiny #dataset, .tiny #session, .tiny #file-hint, .tiny .section { display: none; }
    .tiny #use-file { display: none; }
    .tiny #file-path { margin: 0; }
    .tiny #brand { height: 1; padding: 0 1; }
    .tiny #composer { padding: 0; }
    .tiny #status { padding: 0 1; }
    '''

    def __init__(self, agent, model: str, initial_file: str = '', record_details: bool = True):
        super().__init__()
        self.agent, self.model = agent, model
        self.selected_file = ''
        self.initial_file = initial_file
        self.record_details = record_details
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
        yield Static(' DATA ANALYSIS / GB10  |  数据分析终端', id='brand', markup=False)
        with Horizontal(id='body'):
            with Vertical(id='sidebar'):
                yield Static('文件路径', classes='section', markup=False)
                yield Input(placeholder='/home/Developer/data.csv', id='file-path')
                yield Button('[ 加载 ]', id='use-file')
                yield Static('文件：未选择\n大小：—', id='dataset', markup=False)
                yield Static('F3 选择文件\nEnter 确认路径\nTab 切换区域', id='file-hint', markup=False)
                yield Static(f'当前会话\n{self.model}\n本机 · {platform.node()}\n0 次分析', id='session', markup=False)
            with Vertical(id='main'):
                with VerticalScroll(id='conversation'):
                    yield Markdown('**DATA ANALYSIS READY**\n\nF3：输入本机数据文件路径，Enter 加载。\n\nF4：输入分析问题，Enter 执行。\n\n支持统计、分组、相关性、异常检测与报告导出。\n\n示例：按地区统计收入总和与均值。', classes='answer', id='welcome')
                yield RichLog(id='details', wrap=True, markup=False, max_lines=500)
        yield Static('待命 · 引擎尚未执行', id='status', markup=False)
        with Horizontal(id='composer'):
            yield Input(placeholder='> 输入问题，Enter 执行', id='prompt')
            yield Button('[ 执行 ]', id='send')
        yield Footer()

    def on_mount(self):
        self.query_one('#sidebar').border_title = '数据 / 会话'
        self.query_one('#conversation').border_title = '分析结果'
        self.query_one('#details').border_title = '执行日志 · F2 收起'
        self.query_one('#details').display = False
        self.clock = self.set_interval(0.25, self.refresh_status)
        if self.initial_file:
            self.query_one('#file-path', Input).value = self.initial_file
            self.select_file()
        self.action_focus_prompt()
        self.adapt_layout(self.size.width)

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
        statuses[0].update(f'{self.phase} · {self.engine}{elapsed}')

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
        self.query_one('#dataset', Static).update(f'{path.name}\n{size / 1e6:.1f} MB\n目录 · {path.parent.name}')
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
        await conversation.mount(Static('你 · ' + question, classes='question', markup=False))
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
        self.query_one('#session', Static).update(f'当前会话\n{self.model}\n本机 · {platform.node()}\n{self.turns} 次分析')
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
        self.query_one('#prompt', Input).focus()

    def action_focus_file(self):
        if not self.busy:
            self.query_one('#file-path', Input).focus()

    def action_help(self):
        self.notify('F3 文件 · F4 问题 · Enter 执行 · F2 日志 · F10 退出；分析运行时退出会等待内存清理。', title='快捷键帮助', timeout=8)

    def action_safe_quit(self):
        if self.busy:
            # Python thread cancellation cannot safely stop a model call or CUDA operation.
            self.quit_pending = True
            self.notify('当前分析结束、释放会话内存后退出。')
        else:
            self.exit()
