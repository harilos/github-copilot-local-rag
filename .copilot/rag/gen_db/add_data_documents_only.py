#!/usr/bin/env python3
from __future__ import annotations

import os
import runpy
from pathlib import Path


os.environ["LOCAL_RAG_FILE_SELECTION"] = "documents_only"
runpy.run_path(str(Path(__file__).with_name("add_data.py")), run_name="__main__")
