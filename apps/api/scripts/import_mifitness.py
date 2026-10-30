#!/usr/bin/env python
"""CLI: import a Mi Fitness manual-export ZIP from the inbox.

Thin wrapper around ``drhiro_api.services.mifitness_import.main``.

Examples::

    # reconcile only (writes nothing)
    python scripts/import_mifitness.py --zip /opt/drhiro/imports/incoming/x.zip \
        --user-id <uuid> --password-file /opt/drhiro/imports/incoming/.zip_pw --dry-run

    # real import
    python scripts/import_mifitness.py --zip /opt/drhiro/imports/incoming/x.zip \
        --user-id <uuid> --password-file /opt/drhiro/imports/incoming/.zip_pw
"""
from __future__ import annotations

import sys

from drhiro_api.services.mifitness_import import main

if __name__ == "__main__":
    sys.exit(main())