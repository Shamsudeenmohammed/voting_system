"""Backwards-compatible settings shim.

The settings package moved to ``config.settings`` (see manage.py).  This
module re-exports the default local profile so that older deployments using
``DJANGO_SETTINGS_MODULE=voting_system.settings`` keep working.
"""

from config.settings import *  # noqa: F401,F403
