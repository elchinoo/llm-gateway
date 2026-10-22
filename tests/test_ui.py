"""Runs the fake-DOM tests of the admin UI under node (skipped when node is not installed)."""
import os
import shutil
import subprocess
from pathlib import Path

import apgkit
import pytest

JS = Path(__file__).resolve().parent / "js"
STATIC = Path(__file__).resolve().parents[1] / "src" / "llmgw" / "static"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("script", ["models_dom.test.mjs"])
def test_ui_fake_dom(script):
    env = {**os.environ, "STATIC_DIR": str(STATIC), "KIT_DIR": str(Path(apgkit.__file__).resolve().parent / "static")}
    out = subprocess.run(["node", str(JS / script)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
