"""Headless UI regression tests: no API calls, generated test data only."""
import asyncio
import tempfile
import threading
from pathlib import Path

from textual.widgets import Input, Markdown, Static
from tui_app import SparkTUI


class FakeAgent:
    def __init__(self, answer='## 测试结果\n\n| 地区 | 收入 |\n| --- | --- |\n| 测试地区 | 42 |'):
        self.event_sink = None
        self.answer = answer
        self.prompts = []
        self.release = threading.Event()
        self.release.set()

    def run(self, prompt):
        self.prompts.append(prompt)
        self.event_sink('  [round 1] -> analyze_dataset({})')
        self.event_sink('OK engine=pandas rows=8 | CPU BY CHOICE: test fixture')
        self.release.wait(5)
        return self.answer


async def wait_done(app, pilot):
    for _ in range(50):
        await pilot.pause(0.05)
        if not app.busy:
            return
    raise AssertionError('UI did not finish')


async def main():
    with tempfile.TemporaryDirectory() as folder:
        data = Path(folder) / 'fixture.csv'
        data.write_text('region,revenue\nTest,42\n', encoding='utf-8')
        for size in ((120, 38), (80, 24), (45, 22)):
            agent = FakeAgent()
            app = SparkTUI(agent, 'test-model', str(data))
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                assert app.selected_file == str(data.resolve())
                assert app.engine == '尚未执行'
                app.query_one('#prompt', Input).value = '按地区统计收入'
                await pilot.press('enter')
                await wait_done(app, pilot)
                assert len(agent.prompts) == 1 and str(data.resolve()) in agent.prompts[0]
                assert app.engine == 'CPU · pandas' and app.turns == 1
                assert app.elapsed > 0
                assert len(app.query(Markdown)) >= 2
                assert not app.query_one('#details').display
                await pilot.press('f2')
                assert app.query_one('#details').display
                assert app.query_one('#prompt').region.bottom <= size[1]
                assert app.query_one('#prompt').region.right <= size[0]
                assert not app.busy and not app.query_one('#send').disabled
            print(f'PASS layout, file selection, submit, actual-engine status, log toggle: {size}')
        agent = FakeAgent('[model call failed] test-only error')
        app = SparkTUI(agent, 'test-model')
        async with app.run_test() as pilot:
            app.query_one('#prompt', Input).value = 'test'
            await pilot.press('enter')
            await wait_done(app, pilot)
            assert app.phase == '分析失败' and not app.query_one('#prompt').disabled
        print('PASS error returns UI to ready input')
        agent = FakeAgent()
        agent.release.clear()
        app = SparkTUI(agent, 'test-model')
        async with app.run_test() as pilot:
            app.query_one('#prompt', Input).value = 'test'
            await pilot.press('enter')
            await pilot.pause(0.05)
            assert app.busy
            await app.submit_question()
            assert len(agent.prompts) == 1
            await pilot.press('ctrl+q')
            assert app.quit_pending and app.busy
            agent.release.set()
            await wait_done(app, pilot)
        print('PASS duplicate submission blocked; quit waits for backend completion')


if __name__ == '__main__':
    asyncio.run(main())
