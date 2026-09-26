"""Offline connection/settings tests: synthetic keys and a mock HTTP transport only."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

import openai
if int(openai.__version__.split('.')[0]) >= 3:
    import httpx2 as httpx
else:
    import httpx
from openai import OpenAI
from textual.widgets import Button, Checkbox, Input, Select

from api_config import APIConfig, discover_models, load_config, normalize_url, save_config
from api_setup import APISetup
from agent_main import Agent
import agent_main
from tui_app import SparkTUI
from tui_test import FakeAgent


def config_checks(folder):
    path = folder / 'connection.json'
    with patch.dict(os.environ, {}, clear=True):
        config = APIConfig('https://first.example/v1', 'synthetic-private-key', 'first-model', True)
        save_config(config, path)
        assert 'synthetic-private-key' not in path.read_text()
        assert 'synthetic-private-key' not in repr(config)
        loaded = load_config(path)
        assert loaded.skip_setup and loaded.model == 'first-model' and not loaded.api_key
        save_config(replace(config, remember_key=True), path)
        assert load_config(path).api_key == config.api_key
        if os.name != 'nt':
            assert path.stat().st_mode & 0o777 == 0o600
        with patch.dict(os.environ, {'GPU_API_BASE_URL': 'https://second.example/v1'}):
            moved = load_config(path)
            assert not moved.api_key and not moved.model
        with patch.dict(os.environ, {'GPU_API_BASE_URL': 'https://second.example/v1',
                                     'GPU_API_KEY': 'second-test-key', 'GPU_API_MODEL': 'second-model'}):
            moved = load_config(path)
            assert moved.api_key == 'second-test-key' and moved.model == 'second-model'
        save_config(replace(config, remember_key=False), path)
        assert 'api_key' not in json.loads(path.read_text())
        legacy = folder / 'legacy.json'
        with patch.dict(os.environ, {'STEPFUN_API_KEY': 'legacy-test-key'}):
            loaded = load_config(legacy)
            assert loaded.model == 'step-3.7-flash' and loaded.api_key == 'legacy-test-key'
            with patch.dict(os.environ, {'OPENAI_BASE_URL': 'https://other.example/v1'}):
                assert not load_config(legacy).api_key
        assert normalize_url('http://localhost:8000') == 'http://localhost:8000/v1'
        for url in ('http://remote.example/v1', 'https://key@remote.example/v1',
                    'https://remote.example/v1?key=secret', 'not-a-url'):
            try:
                normalize_url(url)
            except ValueError:
                pass
            else:
                raise AssertionError('unsafe URL accepted')
    print('PASS persistence, plaintext opt-in/removal, env compatibility, endpoint-bound credentials')


def http_checks():
    requests = []
    def transport(request):
        requests.append(request)
        if request.url.path.endswith('/models'):
            return httpx.Response(200, json={'data': [
                {'id': 'test-z', 'created': 0, 'object': 'model', 'owned_by': 'test'},
                {'id': 'test-a', 'created': 0, 'object': 'model', 'owned_by': 'test'}], 'object': 'list'})
        return httpx.Response(200, json={'id': 'test', 'created': 0, 'object': 'chat.completion',
            'model': 'test-z', 'choices': [{'index': 0, 'finish_reason': 'stop',
                                         'message': {'role': 'assistant', 'content': 'test result'}}]})
    config = APIConfig('https://custom.example/prefix/v1', 'synthetic-key', 'test-z')
    def factory(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(transport)))
    with patch('api_config.OpenAI', side_effect=factory):
        assert discover_models(config) == ['test-a', 'test-z']
    assert str(requests[0].url) == config.base_url + '/models'
    assert requests[0].headers['authorization'] == 'Bearer synthetic-key'
    with factory(api_key=config.api_key, base_url=config.base_url) as client:
        agent = Agent(client, verbose=False, model='test-z')
        with patch('agent_main.skills.close_all_sessions', return_value={}):
            assert agent.run('offline test') == 'test result'
        assert json.loads(requests[-1].content)['model'] == 'test-z'
        assert 'synthetic-key' not in agent.safe_error(ValueError('echo synthetic-key'))
    class BadClient:
        def __enter__(self):
            self.models = SimpleNamespace(list=lambda: (_ for _ in ()).throw(ValueError('echo synthetic-key')))
            return self
        def __exit__(self, *args):
            pass
    with patch('api_config.OpenAI', return_value=BadClient()):
        try:
            discover_models(config)
        except ValueError as exc:
            assert 'synthetic-key' not in str(exc)
    print('PASS selected endpoint /models, actual request model, sanitized API errors (mock HTTP)')


def entry_checks():
    empty = APIConfig()
    with patch('agent_main.load_config', return_value=empty), patch('sys.argv', ['agent_main.py']), \
            patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
            patch('tui_app.SparkTUI') as ui:
        assert agent_main.main() == 0
        call = ui.call_args
        assert call.args[0].client is None and call.kwargs['connection'] is empty
        ui.return_value.run.assert_called_once()
    with patch('agent_main.load_config', return_value=empty), \
            patch('sys.argv', ['agent_main.py', '--ask', 'test']), patch('builtins.input') as read, \
            patch('builtins.print'):
        assert agent_main.main() == 2
        read.assert_not_called()
    print('PASS first launch without key enters settings-capable UI; --ask never prompts')


async def ui_checks(folder):
    path = folder / 'ui' / 'connection.json'
    config = APIConfig('https://first.example/v1', 'first-key', 'first-model')
    saved = []
    agents = []
    def persist(value):
        save_config(value, path)
        saved.append(value)
    def factory(value):
        agent = FakeAgent()
        agent.model = value.model
        agents.append(agent)
        return agent
    for size in ((120, 40), (80, 24), (45, 22)):
        app = SparkTUI(FakeAgent(), config.model, connection=config, agent_factory=factory,
                       persist_config=persist, model_loader=lambda value: ['other-model', 'chosen-model'])
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, APISetup)
            screen = app.screen
            assert screen.query_one('#api-key', Input).password
            screen.query_one('#api-url', Input).value = 'https://second.example/v1'
            await pilot.pause()
            assert not screen.query_one('#api-key', Input).value
            assert not screen.query_one('#api-model', Input).value
            screen.query_one('#api-key', Input).value = 'second-key'
            await pilot.pause()
            screen.query_one('#fetch-models', Button).press()
            for _ in range(30):
                await pilot.pause(0.05)
                if not screen.fetching:
                    break
            screen.query_one('#api-model-list', Select).value = 'chosen-model'
            await pilot.pause()
            assert screen.query_one('#api-model', Input).value == 'chosen-model'
            screen.query_one('#api-skip', Checkbox).value = True
            button = screen.query_one('#api-save', Button)
            button.scroll_visible(animate=False)
            await pilot.pause()
            assert button.region.bottom <= size[1] and button.region.right <= size[0]
            button.press()
            await pilot.pause()
            assert not isinstance(app.screen, APISetup)
            assert app.model == 'chosen-model' and app.agent.model == 'chosen-model'
            assert 'second.example' in app.context_text() and 'first.example' not in app.context_text()
            assert not saved[-1].remember_key and 'second-key' not in path.read_text()
            app.query_one('#prompt', Input).value = '/settings'
            await pilot.press('enter')
            assert isinstance(app.screen, APISetup)
            await pilot.press('escape')
            assert not isinstance(app.screen, APISetup)
        print(f'PASS startup modal, password masking, model selection, address switch, settings reopen: {size}')
    with patch.dict(os.environ, {}, clear=True):
        restart = load_config(path)
    app = SparkTUI(FakeAgent(), restart.model, connection=restart,
                   agent_factory=factory, persist_config=persist)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not isinstance(app.screen, APISetup)
        await pilot.press('f5')
        assert isinstance(app.screen, APISetup)
        app.screen.query_one('#api-ignore', Button).press()
        await pilot.pause()
        assert app.connection.skip_setup and not isinstance(app.screen, APISetup)
    print('PASS persisted do-not-prompt without key, ignore action, F5 remains accessible')
    gate = threading.Event()
    def slow_models(value):
        gate.wait(3)
        return ['stale-model']
    app = SparkTUI(FakeAgent(), config.model, connection=config, agent_factory=factory,
                   persist_config=persist, model_loader=slow_models)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one('#fetch-models', Button).press()
        await pilot.pause(0.05)
        screen.query_one('#api-url', Input).value = 'https://third.example/v1'
        await pilot.pause()
        gate.set()
        await pilot.pause(0.2)
        assert screen.query_one('#api-model-list', Select).value is Select.BLANK
        assert not screen.query_one('#api-model', Input).value
        await pilot.press('escape')
    print('PASS stale provider model-list response is discarded')


def main():
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        config_checks(folder)
        http_checks()
        entry_checks()
        asyncio.run(ui_checks(folder))


if __name__ == '__main__':
    main()
