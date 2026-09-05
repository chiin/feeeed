from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path


STATE_NAMESPACES = {"streams", "programs", "generation"}
STATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
MIGRATION_SCHEMA_VERSION = 1


class StateStore:
    def __init__(
        self,
        repository_root: Path,
        legacy_history_path: Path | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve()
        self.legacy_history_path = self._repository_path(
            legacy_history_path or Path("history.json")
        )
        self._legacy_history: dict | None = None
        self._states: dict[str, tuple[Path, dict]] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._migration_path = self.repository_root / "state/migration.json"
        self._migration_manifest: dict | None = None
        self._migrated_stream_ids: set[str] | None = None
        self._migration_changed = False

    def load_stream(
        self, stream_id: str, state_file: str | Path | None = None
    ) -> dict:
        return self.load("streams", stream_id, state_file)

    def load_program(
        self, program_id: str, state_file: str | Path | None = None
    ) -> dict:
        return self.load("programs", program_id, state_file)

    def load_generation(
        self, program_id: str, state_file: str | Path | None = None
    ) -> dict:
        return self.load("generation", program_id, state_file)

    def load(
        self,
        namespace: str,
        state_id: str,
        state_file: str | Path | None = None,
    ) -> dict:
        if namespace not in STATE_NAMESPACES:
            raise ValueError(f"unsupported state namespace: {namespace}")
        if not isinstance(state_id, str) or not STATE_ID_PATTERN.fullmatch(state_id):
            raise ValueError(f"invalid state identifier: {state_id!r}")

        path = self._state_path(namespace, state_id, state_file)
        owner = (namespace, state_id)
        path_key = self._path_key(path)
        existing_owner = self._owners.get(path_key)
        if existing_owner and existing_owner != owner:
            raise ValueError(
                f"state file '{path}' is already assigned to "
                f"{existing_owner[0]}/{existing_owner[1]}"
            )
        self._owners[path_key] = owner

        if path_key not in self._states:
            if path.exists():
                state = self._load_object(path)
                if namespace == "streams":
                    migrated_streams = self._migrated_streams()
                    if state_id not in migrated_streams:
                        migrated_streams.add(state_id)
                        self._migration_changed = True
            elif namespace == "streams":
                migrated_streams = self._migrated_streams()
                if state_id in migrated_streams:
                    raise FileNotFoundError(
                        f"state file for migrated stream '{state_id}' is missing: {path}"
                    )
                state = copy.deepcopy(self._load_legacy_history().get(state_id, {}))
                if not isinstance(state, dict):
                    raise ValueError(
                        f"legacy state for stream '{state_id}' must be an object"
                    )
                migrated_streams.add(state_id)
                self._migration_changed = True
            else:
                state = {}
            self._states[path_key] = (path, state)
        return self._states[path_key][1]

    def save_all(self) -> None:
        for path, state in self._states.values():
            self._write_object(path, state)
        if self._migration_changed:
            manifest = self._load_migration_manifest()
            manifest["migrated_streams"] = sorted(self._migrated_streams())
            self._write_object(self._migration_path, manifest)
            self._migration_changed = False

    def _state_path(
        self,
        namespace: str,
        state_id: str,
        state_file: str | Path | None,
    ) -> Path:
        relative_path = (
            Path(state_file)
            if state_file is not None
            else Path("state") / namespace / f"{state_id}.json"
        )
        path = self._repository_path(relative_path)
        path_within_repository = path.relative_to(self.repository_root)
        if not path_within_repository.parts or path_within_repository.parts[0] != "state":
            raise ValueError(f"state file must be inside state/: {relative_path}")
        if self._path_key(path) == self._path_key(self._migration_path):
            raise ValueError("state/migration.json is reserved for migration metadata")
        if path.suffix.lower() != ".json":
            raise ValueError(f"state file must use a .json extension: {relative_path}")
        return path

    @staticmethod
    def _path_key(path: Path) -> str:
        return str(path).casefold()

    def _repository_path(self, relative_path: Path) -> Path:
        if relative_path.is_absolute():
            raise ValueError(f"state path must be repository-relative: {relative_path}")
        path = (self.repository_root / relative_path).resolve()
        try:
            path.relative_to(self.repository_root)
        except ValueError as error:
            raise ValueError(
                f"state path escapes repository root: {relative_path}"
            ) from error
        return path

    def _load_legacy_history(self) -> dict:
        if self._legacy_history is None:
            self._legacy_history = (
                self._load_object(self.legacy_history_path)
                if self.legacy_history_path.exists()
                else {}
            )
        return self._legacy_history

    def _load_migration_manifest(self) -> dict:
        if self._migration_manifest is None:
            if self._migration_path.exists():
                manifest = self._load_object(self._migration_path)
                if manifest.get("schema_version") != MIGRATION_SCHEMA_VERSION:
                    raise ValueError("unsupported state migration schema version")
                migrated = manifest.get("migrated_streams")
                if not isinstance(migrated, list) or not all(
                    isinstance(stream_id, str) for stream_id in migrated
                ):
                    raise ValueError(
                        "state migration manifest has invalid migrated_streams"
                    )
                self._migration_manifest = manifest
            else:
                self._migration_manifest = {
                    "schema_version": MIGRATION_SCHEMA_VERSION,
                    "legacy_source": self.legacy_history_path.name,
                    "migrated_streams": [],
                }
        return self._migration_manifest

    def _migrated_streams(self) -> set[str]:
        if self._migrated_stream_ids is None:
            manifest = self._load_migration_manifest()
            self._migrated_stream_ids = set(manifest["migrated_streams"])
        return self._migrated_stream_ids

    @staticmethod
    def _load_object(path: Path) -> dict:
        with path.open(mode="r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError(f"state file must contain a JSON object: {path}")
        return data

    @staticmethod
    def _write_object(path: Path, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError(f"state must be a JSON object: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, mode="w", encoding="utf-8") as file:
                json.dump(data, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
