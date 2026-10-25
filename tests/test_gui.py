# -*- coding: utf-8 -*-
"""Окно программы на модели: подключение, ручная уставка, СТОП, вкладка кривой, закрытие."""
import os
import sys

import pytest

import pr8_cathode_app as A

pytestmark = pytest.mark.gui

tk = pytest.importorskip("tkinter")
pytest.importorskip("matplotlib")
if not (sys.platform.startswith("win") or os.environ.get("DISPLAY")):
    pytest.skip("нет дисплея (в CI запускать через xvfb-run)", allow_module_level=True)


def test_window_smoke(monkeypatch):
    import tkinter.messagebox as mb
    monkeypatch.setattr(mb, "askyesno", lambda *a, **k: True)
    errors = []
    monkeypatch.setattr(mb, "showerror", lambda *a, **k: errors.append(a))
    seen = {}

    def hook(app):
        steps = [
            (200, lambda: (app.v_kind.set("sim"), app._kind_changed(), app.v_speed.set("60"))),
            (400, app._connect),
            (1500, lambda: (app.v_man.set("20"), app.v_manrate.set("10"), app._manual())),
            (5000, app._stop),
            (5500, lambda: seen.update(phase=app.l_phase.cget("text"), title=app.title(),
                                       log=app.log.get("1.0", "end"))),
            (6000, app._on_close),
        ]
        for ms, fn in steps:
            app.after(ms, fn)

    A.run_gui(hook)
    assert not errors
    assert "УДЕРЖАНИЕ" in seen["phase"]
    assert A.__version__ in seen["title"]
    assert "Ошибка обновления окна" not in seen["log"]
    assert "Внутренняя ошибка" not in seen["log"]
