"""Native Windows notification-area status; no desktop webview or Electron runtime."""
from __future__ import annotations

import os
import webbrowser
from threading import Thread

from . import __version__
from .background import Background, status_label


def start_tray(background: Background, url: str, shutdown, restart):
    if os.name != 'nt':
        return None
    from PIL import Image, ImageDraw
    from pystray import Icon, Menu, MenuItem

    def image(state):
        color = '#e39a30' if state in {'starting', 'scanning', 'error', 'warning', 'paused'} else '#208879'
        canvas = Image.new('RGBA', (64, 64))
        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle((2, 2, 62, 62), radius=15, fill=color)
        for a, b in [((32, 14), (32, 50)), ((14, 32), (50, 32)),
                     ((20, 20), (44, 44)), ((20, 44), (44, 20))]:
            draw.line((a, b), fill='white', width=4)
        return canvas

    def open_page(_icon=None, _item=None):
        webbrowser.open(url)

    def toggle_pause(_icon, _item):
        background.pause(not background.snapshot()['paused'])

    icon = Icon('AgentWorkbench', image('starting'), f'Agent Workbench v{__version__}', Menu(
        MenuItem('打开用量仪表盘', open_page, default=True),
        MenuItem(lambda item: status_label(background.snapshot()), lambda icon, item: None, enabled=False),
        MenuItem('立即采集', lambda icon, item: background.request()),
        MenuItem('暂停采集', toggle_pause, checked=lambda item: background.snapshot()['paused']),
        MenuItem('重启后台', lambda icon, item: restart()),
        MenuItem('打开日志目录', lambda icon, item: os.startfile(str(background.db.path.parent))),
        Menu.SEPARATOR,
        MenuItem('退出后台（登录后会自动启动）', lambda icon, item: shutdown()),
    ))

    def setup(current):
        current.visible = True
        background.set(tray_visible=True)
        previous = None
        try:
            while not background.stop_event.wait(5):
                state = background.snapshot()
                signature = (state['collector_state'], state['last_success'])
                if signature != previous:
                    current.title = f'Agent Workbench v{__version__} · {status_label(state)}'
                    current.icon = image(state['collector_state'])
                    current.update_menu()
                    previous = signature
        finally:
            background.set(tray_visible=False)

    def run():
        try:
            icon.run(setup=setup)
        except Exception as exc:
            background.set(tray_visible=False, tray_error=str(exc)[:160])

    Thread(target=run, name='awb-tray', daemon=True).start()
    return icon
