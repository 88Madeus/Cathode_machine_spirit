# -*- coding: utf-8 -*-
"""Экспорт журнала в Excel: содержимое, повреждённый CSV, занятый файл."""
import csv
import os
from datetime import datetime, timedelta

import pytest

import pr8_cathode_app as A

openpyxl = pytest.importorskip("openpyxl")


def make_csv(path, n=200, event_row=None):
    t0 = datetime(2026, 10, 25, 12, 0, 0)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time", "t_s", "phase", "I_set_A", "I_A", "U_V", "P_W", "R_mOhm", "event"])
        for k in range(n):
            I = min(88.0, k * 0.5)
            ev = event_row if (k == 0 and event_row) else ""
            w.writerow([(t0 + timedelta(seconds=0.5 * k)).isoformat(timespec="milliseconds"),
                        "%.1f" % (0.5 * k), "ВВЕРХ ПО КРИВОЙ", "%.3f" % I, "%.3f" % I,
                        "%.3f" % (I * 0.05), "%.1f" % (I * I * 0.05), "50.00" if I >= 1 else "", ev])
    return path


def session_sheet(path):
    ws = openpyxl.load_workbook(path)["Сессия"]
    return {r[0]: r[1] for r in ws.iter_rows(min_row=3, values_only=True) if r[0]}


def test_export_has_dates_times_and_sheets(tmp_path):
    src = make_csv(str(tmp_path / "s.csv"),
                   event_row="Прибор: ITECH Ltd.,IT6012D-300-150,X,1  (программа v9.9.9)")
    out = A.export_session_xlsx(src)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames[:4] == ["Сессия", "Журнал", "События", "Графики"]
    info = session_sheet(out)
    assert info["Дата"] == "25.10.2026"
    assert info["Время начала"] == "12:00:00"
    assert info["Время окончания"] == "12:01:39"
    assert info["Прибор (*IDN?)"] == "ITECH Ltd.,IT6012D-300-150,X,1"
    assert info["Версия программы"] == "9.9.9"
    assert wb["Журнал"].max_row == 201


def test_meta_from_engine_wins(tmp_path):
    src = make_csv(str(tmp_path / "s.csv"))
    meta = dict(start=datetime(2026, 10, 25, 11, 59, 0), end=datetime(2026, 10, 25, 13, 0, 0),
                resource="SIM", idn="IDN", out_on_end=True, I_end=88.0, phase_end="УДЕРЖАНИЕ",
                dt=0.5, version=A.__version__)
    info = session_sheet(A.export_session_xlsx(src, meta=meta, events=[]))
    assert info["Время начала"] == "11:59:00" and info["Время окончания"] == "13:00:00"
    assert "ВКЛЮЧЁН" in info["Состояние в конце"]
    assert info["Версия программы"] == A.__version__


def test_damaged_csv_with_nul_bytes(tmp_path):
    src = make_csv(str(tmp_path / "s.csv"))
    with open(src, encoding="utf-8-sig") as f:
        text = f.read()
    bad = str(tmp_path / "bad.csv")
    with open(bad, "w", encoding="utf-8-sig") as f:
        f.write(text[: len(text) // 2] + "\x00\x00\x00" + "2026-10-25T12:0")
    out = A.export_session_xlsx(bad)
    assert out and os.path.exists(out)


def test_empty_csv_gives_none(tmp_path):
    src = str(tmp_path / "e.csv")
    with open(src, "w", encoding="utf-8-sig") as f:
        f.write("time,t_s,phase,I_set_A,I_A,U_V,P_W,R_mOhm,event\n")
    assert A.export_session_xlsx(src) is None


def test_file_open_in_excel_saved_as_copy(tmp_path, monkeypatch):
    src = make_csv(str(tmp_path / "s.csv"))
    real_save = openpyxl.Workbook.save
    calls = []

    def save(self, filename):
        calls.append(filename)
        if len(calls) == 1:
            raise PermissionError("файл открыт в Excel")
        return real_save(self, filename)

    monkeypatch.setattr(openpyxl.Workbook, "save", save)
    out = A.export_session_xlsx(src)
    assert out.endswith("s (2).xlsx") and os.path.exists(out)


def test_long_session_chart_data_decimated(tmp_path):
    src = make_csv(str(tmp_path / "long.csv"), n=12000)
    wb = openpyxl.load_workbook(A.export_session_xlsx(src))
    assert wb["Журнал"].max_row == 12001
    assert wb["Данные графиков"].max_row <= 4002
