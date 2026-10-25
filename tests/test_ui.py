"""Runs the fake-DOM tests of the admin UI under node (skipped when node is not installed)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import apgkit
import pytest

JS = Path(__file__).resolve().parent / "js"
STATIC = Path(__file__).resolve().parents[1] / "src" / "llmgw" / "static"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("script", ["models_dom.test.mjs", "dialogs_dom.test.mjs"])
def test_ui_fake_dom(script):
    env = {**os.environ, "STATIC_DIR": str(STATIC), "KIT_DIR": str(Path(apgkit.__file__).resolve().parent / "static")}
    out = subprocess.run(["node", str(JS / script)], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr


def test_no_native_dialogs_in_static_js():
    """Native confirm()/alert()/prompt() freeze browser automation and cannot be styled: use confirmDialog/promptDialog from /kit/ui.js."""
    native = re.compile(r"(?<![\w.$])(?:window\.)?(?:confirm|alert|prompt)\s*\(")
    offenders = []
    for f in sorted([*STATIC.rglob("*.js"), *STATIC.rglob("*.html")]):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            if native.search(line):
                offenders.append(f"{f.name}:{n}")
    assert not offenders, f"native dialogs in {offenders}"
