#!/usr/bin/env python3
"""Test runner for the DeepSeek companion. Exercises the pure-unit fail-closed suite
(no `claude` call, no API spend) by invoking the companion's built-in --selftest, and
gates on its exit code."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANION = os.path.join(HERE, "..", "scripts", "deepseek_companion.py")

if __name__ == "__main__":
    r = subprocess.run([sys.executable, COMPANION, "--selftest"])
    if r.returncode != 0:
        print("FAIL: companion --selftest returned", r.returncode)
        sys.exit(1)
    print("PASS: companion selftest")
