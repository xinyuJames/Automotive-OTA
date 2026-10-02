# Automotive OTA simulator

This simulator delivers real files to two simulated ECUs in one car. Each car owns its gateway, installed firmware, update state, and CAN bus. The demo firmware is readable text; installing it proves file delivery and version tracking, not execution of real vehicle control code.

From this directory, start the services with:

```bash
docker compose up --build
```

Open http://localhost:8080. The OTA page shows versions read from the ECUs. Click **Upgrade to newest** to approve the current signed campaign. The **Car registration** page saves vehicle details and features, records crash/diagnostic events, and displays installed versions and update history. Crash data must be submitted through that page or API; no crash sensor or CARLA integration is implemented.

## Repeat the demo from the beginning

From `simulator/`, run:

```bash
./reset-demo
```

This stops the demo, restores both ECUs to **A**, deletes the registry database (vehicle records, jobs, approvals, and events), removes gateway state and downloaded files, and clears old traces while keeping an empty `simulator/traces/simulation_trace.jsonl`. Initial registration is reloaded from `registrations/VIN_SIM_0001.json` on the next startup. Firmware source files, published blobs, the catalog, and service logs outside `simulator/traces/` are preserved.

It does not build or restart services. Start again with:

```bash
docker compose up --build
```

From the repository root, use `./simulator/reset-demo`. The reset uses the existing ECU image to handle Docker-owned files, so run the simulator at least once before using it.

## Live traces

The backend, gateway, and ECUs append campaign, state-transition, verification, activation, and confirmation events to `simulator/traces/simulation_trace.jsonl`. After reset, this file stays empty until new events occur. From `simulator/`, follow it with `tail -F traces/simulation_trace.jsonl`.

## Service boundaries

```text
Backend --gRPC--> Artifact server: read catalog/firmware, publish immutable artifacts
Backend --gRPC--> Control plane --gRPC--> Artifact server: manifests and jobs
Backend --MQTT--> Vehicle gateway: update notification
Vehicle gateway --gRPC--> Control plane: jobs, registration, inventory, history
Vehicle gateway --HTTP--> Artifact server: firmware and delta downloads
Vehicle gateway --CAN--> Engine ECU / ADAS ECU: programming and inventory
```

These are the existing services; the artifact server now also owns a SQLite registry. No other service mounts its filesystem or opens its database. The control plane exposes the existing gRPC API and forwards persistent record operations to the registry. The backend has no artifact volume. Firmware never reaches an ECU through a shared backend directory.

The supplied networking is for this local simulator: gRPC and HTTP are plaintext, MQTT allows anonymous clients, and signing keys are demo keys. It is not configured as a public Internet deployment. CAN is simulated using UDP multicast, not a complete UDS/ISO-TP implementation.

## Visible storage

All application state uses explicit host bind mounts; there is no `ota-artifacts` Docker named volume. Container paths are just mount points for these visible directories:

| Host path (relative to Automotive-OTA) | Owner and content |
| --- | --- |
| `simulator/artifact-server/engine_firmware/version_a`, `version_b` | Engine source releases |
| `simulator/artifact-server/adas_firmware/version_a`, `version_b` | ADAS source releases |
| `simulator/artifact-server/catalog.json` | ECU descriptions, SOTA/FOTA classification, optional release notes |
| `simulator/artifact-server/registrations/VIN_SIM_0001.json` | Initial vehicle registration; imported only if that vehicle is absent |
| `simulator/artifact-server/data/registry.sqlite3` | Persistent vehicles, observed ECU inventory, manifests, jobs, approvals, crash/diagnostic events, version identities |
| `simulator/artifact-server/data/blobs/<sha256>` | Published full firmware, base images, and patches, addressed by content hash |
| `vehicle_0001/engine_firmware/` | Engine ECU installation storage |
| `vehicle_0001/adas_firmware/` | ADAS ECU installation storage |
| `vehicle_0001/gateway/` | This car's persisted job/approval state and verified download cache |
| `vehicle_0001/logs/` | Separate gateway and ECU logs |
| `simulator/data/` | Backend logs and MQTT runtime directories |

In each ECU directory, inspect:

```text
identity.json                       vehicle ID and ECU ID
current/firmware.bin                 actual installed firmware
current/metadata.json                version, measured hash, size, update type
previous/firmware.bin                previous installed firmware, after an update
versions/<hash>-<id>/                retained complete versions
staged/                             verified candidate before activation
```

`current` and `previous` are relative directory symlinks. Activation atomically redirects `current` to a complete verified version directory. The ECU reads back actual bytes when it reports inventory. The gateway does not mount either ECU's installation directory.

The supplied `vehicle_0001` was provisioned once with independent version A copies. Changing a source file on the artifact server does **not** change installed ECU files. After approval and a successful update, both `current/firmware.bin` files match the corresponding artifact-server `version_b`, and `previous/firmware.bin` retains A. The folder represents vehicle ID `VIN_SIM_0001` throughout the services.

## Catalog and version generation

`backend/orchestrator.py` no longer generates random firmware. Every 10 seconds it reads available releases and vehicle inventory through gRPC, fetches real firmware bytes, and signs a vehicle-specific manifest when newer releases are available. If the reported installed version/hash matches an available source release, it generates and uploads a delta; otherwise it selects a full image. Both download and final firmware have separate signed hashes and sizes.

The gateway verifies downloaded bytes, checks the installed base for a delta, reconstructs and verifies the final image, and transfers it over CAN. The installation handler passes the prepared bytes to the ECU and records its confirmation response, without repeating gateway hash/version comparisons. The ECU still checks the transferred bytes before activation, and each acknowledgement is checked for success. Failed activation leaves the current file untouched and reports `FAILED`, not a fictitious rollback.

To publish a subsequent version, add a nonempty file named `version_c`, `version_d`, etc. to the relevant ECU folder. The artifact server discovers these filenames on each request; no catalog edit or service restart is needed. Names use lowercase letters, ordered A through Z, then AA, AB, and so on. Files with other names (including temporary files with extensions) are ignored. Finish writing a release before moving it into place under its final name.

Versions are selected independently for each ECU. If both ECUs run B and only `adas_firmware/version_c` is added, the next manifest targets only ADAS C; engine stays on B. The newest available version is selected, so several files added together produce one update to the newest version. Removing a newer source does not trigger a downgrade.

`catalog.json` still supplies component names, SOTA/FOTA labels, and optional version-specific release notes. Filename discovery determines available versions and the latest version; its old `latest` setting is no longer used. Published version contents remain immutable: use a new filename/version instead of changing an already-published release.

An unfinished campaign is kept until completion; a new file does not silently change a manifest already awaiting approval. The backend then checks for further updates. It avoids repeatedly creating the same failed offer during one backend run; restart the backend if you deliberately want to retry it.


Registration data and installed observations are distinct. Changing the vehicle profile cannot set an installed version. Installed inventory is reported by the vehicle gateway after querying its ECUs. Profiles, crashes, SOTA/FOTA inventory, and update outcomes survive service restarts. The read API returns the latest 100 jobs/events per vehicle; older entries remain in SQLite.

## Local interfaces

- `POST /api/approve` on the gateway approves the specific current manifest.
- `GET/POST /api/registration` reads or edits this gateway's vehicle profile: `make`, `model`, `model_year`, `vehicle_type`, and a list of `features`.
- `POST /api/vehicle/events` records `{ "kind": "crash", "event_id": "unique-id", "details": { "description": "..." } }`. Kind can also be `diagnostic`; supplying a stable event ID makes retries idempotent.
- `GET /api/status` reports actual observed current/previous versions plus the proposed update.
- gRPC services are declared in `ota.proto`. Artifact gRPC is port 50052; control-plane gRPC is 50051. HTTP port 8082 serves only `/blobs/<sha256>`, not database or registration files.

Rollback and Force stop buttons remain placeholders. Previous firmware preservation is implemented; automatic rollback of an already-activated multi-ECU campaign is not. File installation does not load code into CARLA. The database supports multiple vehicle IDs, but the supplied Compose file still runs one vehicle: adding another requires its own gateway, ECU storage, IDs and CAN multicast channel.

## Validation

Build the images with `docker compose build`, then run from the repository root:

```bash
docker run --rm --network none -v "$PWD:/repo:ro" -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 simulator-gateway python /repo/simulator/tests/test_storage_flow.py
python3 simulator/tests/run_integration.py
python3 -B simulator/tests/test_demo_reset.py
```

The integration test launches an isolated Compose project with disposable copies and no published ports. It verifies delta and full installation, file bytes, previous versions, approval/restart behavior, failure propagation, registration/crash persistence, and job history. It does not upgrade your actual `vehicle_0001`.

## Implementation file summary

| Files | Change |
| --- | --- |
| `artifact-server/server.py`, `requirements.txt`, `Dockerfile` | gRPC registry/catalog/upload service, SQLite persistence, HTTP blob downloads |
| `artifact-server/catalog.json`, `registrations/VIN_SIM_0001.json`, four `*_firmware/version_*` files | Real A/B source fixtures, release metadata, initial registration |
| `backend/orchestrator.py` | Network-only release creation from catalog files and observed vehicle versions |
| `control-plane/main.py` | Forward persistent record operations through gRPC; stop API explicitly unimplemented |
| `ota.proto` and its `backend/`, `gateway/`, `control-plane/` copies | Artifact transfer, vehicle profile, inventory, events, and pending-job contracts |
| `ecu/ecu_app.py`, new `ecu/firmware_store.py`, `ecu/Dockerfile` | Verified CAN programming, visible file installation, current/previous metadata and read-back |
| `gateway/ota_agent.py`, `downloader.py`, `control_plane_client.py`, `mqtt_client.py` | Real full/delta downloads, matching CAN replies, persistent approval/recovery state, discovery and inventory reporting |
| `gateway/can_bus.py`, `ecu/can_bus.py` | Configurable per-vehicle multicast channel and fragment-order checks |
| `gateway/gateway_app.py`, `templates/index.html`, `static/js/app.js` | Actual ECU versions, registration editor, crash/diagnostic records and history |
| `docker-compose.yml`, repository `.gitignore` | Explicit service-owned host storage; remove shared artifact named volume; ignore runtime caches/database/logs |
| `../vehicle_0001/` | Independently provisioned A firmware, ECU identity/metadata, documented installation folders |
| `reset-demo`, `tests/test_demo_reset.py` | One-command A→B demo reset and temporary-fixture regression tests |
| `tests/test_storage_flow.py`, `tests/run_integration.py` | Focused regression tests and isolated full-stack verification |
| This README and `../ECU_OTA_IMPLEMENTATION_PLAN.md` | Architecture/storage guide, commands, implementation summary, updated bug statuses |
