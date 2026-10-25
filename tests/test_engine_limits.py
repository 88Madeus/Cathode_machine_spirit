# -*- coding: utf-8 -*-
"""Ограничение тока источником, предел напряжения, P_max, смена пределов на ходу.
Главный инвариант: снятие любого ограничения даёт скачок тока не больше LIM_MARGIN."""
import time

import pr8_cathode_app as A
from faults import alarms, hold_at, wait

E_ = A.Engine
JUMP = A.LIM_MARGIN + 0.05


def test_instrument_limit_detected_and_setpoint_pulled_down(bench):
    E, ev, sim, ok = bench()
    sim.i_cap = 50.0
    E.send("up", 88)
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD and s["limiting"], 60)
    assert any("ограничивает" in m.lower() for m in alarms(ev))
    time.sleep(0.5)
    assert E.snap["I_set"] <= 50.0 + JUMP


def test_second_up_while_limited_cannot_run_away(bench):
    E, ev, sim, ok = bench(speed=1500)
    sim.i_cap = 50.0
    E.send("up", 88)
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD and s["limiting"], 120)
    E.send("up", 88)
    time.sleep(1.0)
    assert E.snap["I_set"] < 50.7
    i_before = sim._iv()[0]
    sim.i_cap = None
    assert sim._iv()[0] - i_before <= JUMP


def test_voltage_limit_and_second_up(bench):
    E, ev, sim, ok = bench(speed=1500, cfg={"V_limit": 4.0})
    E.send("up", 88)
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD and s["I_set"] > 30, 120)
    E.send("up", 88)
    # ждём, пока уставка подтянется к ограниченному току (условие, а не время: CI бывает медленным)
    assert wait(E, lambda s: s["phase"] == E_.PH_HOLD and s["I_set"] - s["I"] <= JUMP - 0.02, 60)
    time.sleep(0.2)
    i_before = sim._iv()[0]
    sim.R0 *= 0.7                                    # нагреватель коснулся корпуса — предел U «отпускает»
    assert E.snap["I_set"] - i_before <= JUMP
    assert sim._iv()[0] - i_before <= JUMP


def test_takeover_in_voltage_limit_does_not_touch_volt_lim(bench):
    E, ev, sim, ok = bench(speed=50, preset=dict(on=True, func="CURR", I_set=88.0, T=2600.0, vlim=5.06))
    assert wait(E, lambda s: s["limiting"] and s["I_set"] - s["I"] <= JUMP - 0.02, 60)
    writes = [w for w in sim.writes if not w.endswith("?")]
    assert not any(w.startswith("VOLT:LIM") for w in writes), writes
    vals = [88.0] + [v for _, v in sim.curr_log]
    assert all(b <= a + 1e-9 for a, b in zip(vals, vals[1:])), "при подключении уставка только подтягивается вниз"
    i0 = sim._iv()[0]
    sim.R0 *= 0.7
    assert sim._iv()[0] - i0 <= JUMP


def test_higher_device_volt_lim_synced_only_on_first_motion(bench):
    E, ev, sim, ok = bench(speed=50, preset=dict(on=True, func="CURR", I_set=88.0, T=2600.0, vlim=12.0))
    time.sleep(0.5)
    assert not any(w.startswith("VOLT:LIM") for w in sim.writes) and abs(sim.vlim - 12) < 1e-9
    E.send("down", 80)
    assert wait(E, lambda s: abs(sim.vlim - 8) < 1e-9, 10)


def test_fault_ramp_continues_under_instrument_limit(bench):
    E, ev, sim, ok = bench()
    E.send("manual", 60, 10)
    assert hold_at(E, 60)
    E.send("fault")
    assert wait(E, lambda s: s["I_set"] < 57, 60)
    sim.i_cap = 45.0
    assert wait(E, lambda s: s["phase"] == E_.PH_OFF, 120)


def test_lowering_pmax_in_hold_one_message_power_at_limit(bench):
    E, ev, sim, ok = bench(preset=dict(on=True, func="CURR", I_set=88.0, T=2600.0, vlim=8.0))
    time.sleep(0.3)
    cfg = dict(E.cfg)
    cfg["P_max"] = 400.0
    n = len(ev)
    E.send("cfg", cfg)
    assert wait(E, lambda s: s["P"] <= 403, 60)
    time.sleep(1.0)
    msgs = [m for _, m in ev[n:] if "P_max" in m]
    assert len(msgs) == 1, msgs


def test_lowering_imax_during_manual_ramp(bench):
    E, ev, sim, ok = bench(speed=1500)
    E.send("manual", 90, 10)
    assert wait(E, lambda s: s["I_set"] > 85, 120)
    cfg = dict(E.cfg)
    cfg.update(I_max=80.0, I_work=80.0,
               heat_profile=[(20, 3.0, 0), (40, 1.8, 0), (60, 2.0, 0), (70, 2.0, 180), (80, 1.1, 0)])
    E.send("cfg", cfg)
    time.sleep(0.5)
    assert E.snap["phase"] == E_.PH_HOLD and E.snap["I_set"] > 84
    assert not any("СКАЧКА" in m for m in alarms(ev))
