# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Start the embedded actor host with a short resync interval for
bring-up validation. Equivalent to setting LEGBA_RUNTIME_RESYNC_INTERVAL=30
on the systemd unit; used when env-var injection at the shell level is
not available."""
import os

os.environ.setdefault("LEGBA_RUNTIME_RESYNC_INTERVAL", "30")
os.environ.setdefault("LEGBA_DATA_PG_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_NATS_URL", "nats://127.0.0.1:4222")
os.environ.setdefault("LEGBA_DATA_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("LEGBA_DATA_QDRANT_HOST", "127.0.0.1")

from legba.runtime.host import main  # noqa: E402

main()
