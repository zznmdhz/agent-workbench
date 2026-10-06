"""Release provenance included in a frozen binary and available without Git."""
import json
from pathlib import Path

from . import __version__


def info() -> dict:
    try:
        value = json.loads(Path(__file__).with_name('build.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        value = {'commit': 'development', 'built_at': None}
    return {'version': __version__, **value}
