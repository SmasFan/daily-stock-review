#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""macOS 调度入口：python3 scripts/us_sim.py [--export-only]。"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from usim.web import main


if __name__ == "__main__":
    main()
