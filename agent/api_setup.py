"""Connection setup modal; model discovery never blocks terminal input."""
from dataclasses import replace

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Select, Static

from api_config import discover_models, normalize_url


class ModelsLoaded(Message):
    def __init__(self, generation, models=None, error=''):
        super().__init__()
        self.generation, self.models, self.error = generation, models, error


class APISetup(ModalScreen):
    BINDINGS = [('escape', 'cancel', '暂时跳过')]
    DEFAULT_CSS = '''
    APISetup { align: center middle; background: #000000 65%; padding: 0; }
    #api-dialog { width: 82; max-width: 100%; height: 35; max-height: 100%;
                  background: #191919; color: #e4ded6; border: round #d99a76; padding: 0 2; }
    #api-title { color: #d99a76; text-style: bold; height: 2; padding-top: 1; }
    .api-label { height: 1; color: #a49c93; }
    #api-note { height: auto; margin-bottom: 1; color: #a49c93; }
    #api-feedback { height: auto; min-height: 2; color: #d99a76; }
    APISetup Input, APISetup Select { height: 3; margin: 0; }
    APISetup Checkbox { height: auto; background: #191919; border: none; padding: 0; }
    APISetup Checkbox > .toggle--button { color: #45403a; background: #45403a; }
    APISetup Checkbox.-on > .toggle--button { color: #e4ded6; background: #9d654a; }
    #api-buttons { height: auto; layout: horizontal; }
    #api-buttons Button { min-width: 12; width: 1fr; height: 3; margin: 0; }
    #fetch-models { height: 3; width: 1fr; margin: 0; }
    APISetup.compact #api-dialog { padding: 0 1; }
    APISetup.compact #api-buttons { layout: vertical; height: 9; }
    APISetup.compact #api-buttons Button { width: 1fr; }
    '''

    def __init__(self, config, persist, model_loader=discover_models):
        super().__init__()
        self.config = replace(config)
        self.persist, self.model_loader = persist, model_loader
        self.generation = 0
        self.fetching = False

    def compose(self) -> ComposeResult:
        with VerticalScroll(id='api-dialog'):
            yield Static('GPU加速与数据分析 · 连接设置', id='api-title', markup=False)
            yield Static('支持 OpenAI 兼容接口。密钥只发送到你填写的地址；模型须支持工具调用。', id='api-note', markup=False)
            yield Static('API 基础地址（含 /v1 或服务指定的前缀）', classes='api-label')
            yield Input(self.config.base_url, id='api-url', placeholder='https://example.com/v1')
            yield Static('API Key（隐藏输入；本机无鉴权服务可填 local）', classes='api-label')
            yield Input(self.config.api_key, password=True, id='api-key')
            yield Button('读取此地址的模型列表', id='fetch-models')
            yield Select([], prompt='先读取列表，再选择模型', id='api-model-list')
            yield Static('模型名（选择后填入；服务不支持列表时可手填）', classes='api-label')
            yield Input(self.config.model, id='api-model')
            yield Checkbox('以后不再弹出启动设置', value=self.config.skip_setup, id='api-skip')
            yield Checkbox('保存密钥到本机明文文件（请勿共享）', value=self.config.remember_key, id='api-remember')
            yield Static('默认仅保存地址、模型与提示偏好，密钥只在本次进程中使用。', id='api-feedback', markup=False)
            with Horizontal(id='api-buttons'):
                yield Button('保存并进入', id='api-save')
                yield Button('暂时跳过', id='api-cancel')
                yield Button('忽略，不再提示', id='api-ignore')

    def on_mount(self):
        self.set_class(self.size.width < 65, 'compact')
        self.query_one('#api-url', Input).focus()

    def on_resize(self, event):
        self.set_class(event.size.width < 65, 'compact')

    def on_input_changed(self, event):
        if event.input.id in ('api-url', 'api-key'):
            self.generation += 1
            self.query_one('#api-model-list', Select).set_options([])
            if event.input.id == 'api-url' and event.value != self.config.base_url:
                # No credential or model is silently reused on a different provider.
                self.query_one('#api-key', Input).value = ''
                self.query_one('#api-model', Input).value = ''

    def on_select_changed(self, event):
        if event.select.id == 'api-model-list' and event.value is not Select.BLANK:
            self.query_one('#api-model', Input).value = str(event.value)

    def values(self):
        return replace(self.config,
                       base_url=normalize_url(self.query_one('#api-url', Input).value),
                       api_key=self.query_one('#api-key', Input).value.strip(),
                       model=self.query_one('#api-model', Input).value.strip(),
                       skip_setup=self.query_one('#api-skip', Checkbox).value,
                       remember_key=self.query_one('#api-remember', Checkbox).value)

    def on_button_pressed(self, event):
        event.stop()
        name = event.button.id
        if name == 'api-cancel':
            self.action_cancel()
            return
        if name == 'api-ignore':
            # Ignore only changes the preference, never applies incomplete form data.
            config = replace(self.config, skip_setup=True)
            self.save_and_dismiss(config)
            return
        try:
            config = self.values()
        except ValueError as exc:
            self.feedback(str(exc))
            return
        if name == 'fetch-models':
            if self.fetching:
                return
            self.fetching = True
            self.query_one('#fetch-models', Button).disabled = True
            self.feedback('正在读取模型列表…（不会发送分析数据）')
            self.fetch(self.generation, config)
        elif name == 'api-save':
            if not config.ready:
                self.feedback('请填写 API 地址、密钥并选择或填写模型名。')
                return
            self.save_and_dismiss(config)

    def save_and_dismiss(self, config):
        try:
            self.persist(config)
        except OSError:
            self.feedback('配置无法保存。请检查本机配置目录的写入权限。')
            return
        self.dismiss(config)

    def feedback(self, text):
        self.query_one('#api-feedback', Static).update(text)

    @work(thread=True, exit_on_error=False)
    def fetch(self, generation, config):
        try:
            result = self.model_loader(config)
        except Exception:
            # Do not display provider-controlled exception bodies: they may echo keys.
            self.post_message(ModelsLoaded(generation, error='读取失败：请检查地址、密钥或网络；服务不支持 /models 时可手填模型名。'))
        else:
            self.post_message(ModelsLoaded(generation, models=result))

    def on_models_loaded(self, event):
        if not self.is_mounted:
            return
        self.fetching = False
        self.query_one('#fetch-models', Button).disabled = False
        if event.generation != self.generation:
            self.feedback('地址或密钥已变更，请重新读取模型列表。')
            return
        if event.error:
            self.feedback(event.error)
            return
        self.query_one('#api-model-list', Select).set_options([(name, name) for name in event.models])
        self.feedback(f'已读取 {len(event.models)} 个模型。请选择一个，或保留手填模型名；列表不代表工具调用已验证。')

    def action_cancel(self):
        self.dismiss(None)
