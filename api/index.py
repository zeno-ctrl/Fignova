"""Vercel entry point: the whole AutoFigure Studio app as one Python function."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autofigure.api.app import create_app  # noqa: E402

app = create_app()
