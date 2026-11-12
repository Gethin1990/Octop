"""Pytest fixtures for the scrum control plane.

``tmp_path`` is avoided on purpose: the sandboxed file broker used during
development rejects ``mkdir`` on an existing directory (EEXIST) instead of
honouring ``exist_ok``, which makes pytest's tmp factory flaky. ``mkdtemp``
always picks a fresh name, so it is stable in both environments.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from octop.scrum.store import BoardStore

from .helpers import seed_agents


@pytest.fixture()
def board_dir():
    path = Path(tempfile.mkdtemp(prefix="octop-scrum-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture()
def store(board_dir):
    s = BoardStore(board_dir / "board.db")
    s.init_schema()
    seed_agents(s)
    yield s
    s.close()
