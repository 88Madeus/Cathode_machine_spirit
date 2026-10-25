# -*- coding: utf-8 -*-
"""Штатная работа: движение по кривой, перехват, подхват горячего катода."""
import os
import time

import pr8_cathode_app as A
from faults import alarms, hold_at, max_jump, stop, wait

E_ = A.Engine


def test_full_operator_scenario(bench):
    """▲ → разворот ▼ → снова ▲ → СТОП → ручное → панель → возврат → авария."""
    E, ev, sim, ok = bench()
    assert ok and E.snap["phase"] == E_.PH_OFF

    E.send("up", 88)
    assert wait(E, lambda s: s["I_set"] > 50)

    E.send("down", 0)
    assert wait(E, lambda s: s["phase"] == E_.PH_DOWN)
    t1, i1 = E.snap["t"], E.snap["I_set"]
    assert wait(E, lambda s: s["I_set"] < 42)
    rate = (i1 - E.snap["I_set"]) / (E.snap["t"] - t1) * 60
    assert abs(rate - 2.0) < 0.1, "разворот вниз должен идти со скоростью кривой 2,0 А/мин"

    E.send("up", 88)
    assert wait(E, lambda s: s["phase"] == E_.PH_UP and s["I_set"] > 44)
    E.send("stop")
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD)
    i0 = E.snap["I_set"]
    time.sleep(0.3)
    assert abs(E.snap["I_set"] - i0) < 1e-9, "в удержании уставка не меняется"

    E.send("manual", 60, 5)
    assert hold_at(E, 60)

    E.send("local")
    assert wait(E, lambda s: s["phase"] == E_.PH_PANEL)
    sim.I_set = 63.0                              # оператор изменил ток на панели
    E.send("remote")
    assert wait(E, lambda s: s["control"] and abs(s["I_set"] - 63) < 1e-6)

    E.send("fault")
    assert wait(E, lambda s: s["phase"] == E_.PH_OFF, 60)
    assert max_jump(sim) <= 0.5
    assert not alarms(ev)

    stop(E)
    assert E.last_export and os.path.exists(E.last_export)


def test_hot_takeover_changes_nothing(bench):
    E, ev, sim, ok = bench(preset=dict(on=True, func="CURR", I_set=88.0, T=2600.0, vlim=8.0))
    assert ok and E.snap["phase"] == E_.PH_HOLD and abs(E.snap["I_set"] - 88) < 1e-6
    time.sleep(0.5)
    assert not sim.curr_log, "при подключении не должно быть ни одной команды CURR"
    writes = [w for w in sim.writes if not w.endswith("?")]
    assert writes == ["SYST:REM"], writes


def test_takeover_in_cv_mode_is_observe_only(bench):
    E, ev, sim, ok = bench(preset=dict(on=True, func="VOLT", I_set=50.0, T=1800.0, vlim=8.0))
    assert ok and E.snap["phase"] == E_.PH_OBS and not E.snap["control"]
    E.send("down", 0)
    time.sleep(0.3)
    assert not sim.curr_log


def test_takeover_with_unexplained_mismatch_is_observe_only(bench):
    E, ev, sim, ok = bench(preset=dict(on=True, func="CURR", I_set=88.0, T=1500.0, vlim=8.0, i_cap=40.0))
    assert ok and E.snap["phase"] == E_.PH_OBS


def test_curve_change_reschedules_current_ramp(bench):
    E, ev, sim, ok = bench()
    E.send("up", 88)
    assert wait(E, lambda s: s["I_set"] > 25)
    cfg = A.cfg_from_settings(A.load_settings())
    cfg["heat_profile"] = [(20, 3.0, 0), (40, 1.0, 0), (60, 2.0, 0), (70, 2.0, 180), (80, 1.1, 0), (88, 0.9, 0)]
    E.send("cfg", cfg)
    assert wait(E, lambda s: abs((s["rate"] or 0) - 1.0) < 1e-6, 5)


def test_set_curr_refuses_jump():
    E = A.Engine(A.cfg_from_settings(A.load_settings()), lambda lvl, msg: None)

    class Dummy:
        def __init__(self):
            self.calls = []

        def w(self, key, *a):
            self.calls.append((key, a))

    E.sup = Dummy()
    E.I_sent = E.I_set = 10.0
    E.phase, E.control, E.out_on = E_.PH_MAN, True, True
    E._set_curr(12.0)
    assert not E.sup.calls and abs(E.I_set - 10.0) < 1e-9 and E.phase == E_.PH_HOLD
    E._set_curr(10.3)
    assert E.sup.calls == [("curr", (10.3,))]


def test_wrong_direction_commands_do_nothing(bench):
    E, ev, sim, ok = bench()
    E.send("up", 0)
    time.sleep(0.4)
    assert not sim.on, "«▲ до 0» не должен включать выход"
    E.send("manual", 50, 10)
    assert hold_at(E, 50)
    n = len(sim.curr_log)
    E.send("down", 60)
    time.sleep(0.6)
    assert len(sim.curr_log) == n and abs(sim.I_set - 50) < 0.01, "«▼ до 60» на 50 А не поднимает ток"


def test_command_after_disconnect_ignored(bench):
    E, ev, sim, ok = bench()
    E.send("disconnect")
    E.send("up", 20)
    t0 = time.time()
    while E.busy() and time.time() - t0 < 30:
        time.sleep(0.05)
    assert not sim.on
