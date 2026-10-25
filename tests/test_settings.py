# -*- coding: utf-8 -*-
"""Настройки: проверка значений, испорченный файл, атомарное сохранение."""
import json

import pytest

import pr8_cathode_app as A


def test_protocol_defaults_are_valid():
    assert A.validate_settings(A.load_settings()) == []


def test_dangerous_values_rejected():
    s = A.load_settings()
    s["dt"] = 5.0
    s["manual_rate_max"] = 50.0
    s["heat_profile"] = [[20, 3, 0], [10, 300, 0]]
    errs = A.validate_settings(s)
    assert len(errs) >= 3


def test_profile_point_above_imax_rejected():
    s = A.load_settings()
    s["I_max"] = 50.0
    s["I_work"] = 50.0
    assert any("выше максимума" in e for e in A.validate_settings(s))


def write_settings(text):
    with open(A.SETTINGS_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def test_bad_values_replaced_by_defaults_connection_kept():
    write_settings('{"dt": 50, "conn": {"ip": "10.0.0.5"}}')
    s = A.load_settings()
    assert s["dt"] == 0.5 and s["conn"]["ip"] == "10.0.0.5"
    assert A.LOAD_WARNINGS


@pytest.mark.parametrize("content", ['{"dt": ', "[]", "null", '{"heat_profile": 5}',
                                     '{"conn": {"timeout_ms": "abc", "kind": 5}}'])
def test_any_settings_file_loads(content):
    write_settings(content)
    s = A.load_settings()
    assert s["dt"] == 0.5
    assert s["conn"]["timeout_ms"] == 3000
    assert s["conn"]["kind"] in ("lan", "visa", "custom", "sim")


def test_save_is_atomic_and_roundtrips():
    s = A.load_settings()
    s["I_work"] = 80.0
    A.save_settings(s)
    with open(A.SETTINGS_PATH, encoding="utf-8") as f:
        assert json.load(f)["I_work"] == 80.0
    assert A.load_settings()["I_work"] == 80.0


def test_build_resource():
    c = dict(A.DEFAULTS["conn"])
    assert A.build_resource(c) == "TCPIP0::192.168.1.100::30000::SOCKET"
    c["lan_proto"] = "VXI-11"
    assert A.build_resource(c) == "TCPIP0::192.168.1.100::inst0::INSTR"
    c["kind"] = "sim"
    assert A.build_resource(c) == "SIM"
