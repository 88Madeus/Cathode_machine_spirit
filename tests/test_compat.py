# -*- coding: utf-8 -*-
"""Программа должна запускаться на Python 3.8 (лабораторные ноутбуки бывают старыми)."""
import ast
import glob
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_python38_syntax():
    files = glob.glob(os.path.join(ROOT, "*.py")) + glob.glob(os.path.join(ROOT, "tools", "*.py"))
    assert files
    for path in files:
        with open(path, encoding="utf-8") as f:
            ast.parse(f.read(), filename=path, feature_version=(3, 8))


def test_no_py39_plus_builtins_in_app():
    with open(os.path.join(ROOT, "pr8_cathode_app.py"), encoding="utf-8") as f:
        src = f.read()
    for bad in (".removeprefix(", ".removesuffix(", "math.lcm(", "zip(strict", "| None"):
        assert bad not in src, bad
