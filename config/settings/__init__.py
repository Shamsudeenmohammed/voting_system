"""Settings profile selector.

``DJANGO_ENV`` chooses the profile:

* ``local`` (default)   - LAN / development operation, SQLite unless overridden.
* ``production``        - hardened online operation, PostgreSQL required.
* ``test``              - automated test runs (fast hashes, file based test DB).
"""

import os

_PROFILE = os.environ.get("DJANGO_ENV", "local").strip().lower() or "local"

if _PROFILE == "production":
    from .production import *  # noqa: F401,F403
elif _PROFILE == "test":
    from .test import *  # noqa: F401,F403
else:
    from .local import *  # noqa: F401,F403
