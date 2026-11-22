# uncut-bridge sidecar

- `oai-bridge.py` and `LICENSE-uncut-bridge` are copied verbatim from
  https://github.com/meowmana/uncut-bridge at commit `be07a18781eaee744604e97df9affa31b72b5db6` (MIT License, Copyright (c) 2026 meowmana).
- `router.py` exposes a single public `$PORT` ASGI reverse proxy:
  - `/v1/*` -> `oai-bridge.py` (`127.0.0.1:8765`)
  - everything else (`/`, `/health`, `/v1beta/*`, admin UI, etc.) -> `aistudio-api` (`127.0.0.1:8080`)
- `launcher.py` runs `aistudio-api`, `oai-bridge.py`, and `router.py`, forwarding `SIGTERM`/`SIGINT` to `aistudio-api` and waiting for its graceful shutdown (including the final GitHub state push) before exiting.
