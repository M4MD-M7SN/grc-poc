"""GRC v2 actor identities and SSH/Ed25519 event signatures.

Private keys live outside repositories in Application Support. Repositories only
receive public actor projections and signed event envelopes.
"""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

APP_DIR = Path.home() / "Library/Application Support/GRC"
REGISTRY = APP_DIR / "actors.json"
KEY_DIR = APP_DIR / "actor-keys"


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-") or "actor"


def actor_kind(name: str, requested: Optional[str] = None) -> str:
    if requested in {"user", "agent", "service"}:
        return requested
    return "user" if name.strip().lower() in {"user", "you", os.environ.get("USER", "").lower()} else "agent"


def actor_id(name: str, kind: str) -> str:
    digest = hashlib.sha256(f"grc:v2:{kind}:{name.strip().lower()}".encode()).hexdigest()[:16]
    return f"{kind}-{_slug(name)[:24]}-{digest}"


def _read_registry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {"version": 2, "actors": []}
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 2, "actors": []}


def _write_registry(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


def ensure_actor(name: str, kind: Optional[str] = None, *, registry: Path = REGISTRY, key_dir: Path = KEY_DIR) -> dict[str, Any]:
    kind = actor_kind(name, kind)
    identifier = actor_id(name, kind)
    data = _read_registry(registry)
    existing = next((row for row in data.get("actors", []) if row.get("id") == identifier), None)
    key_dir.mkdir(parents=True, exist_ok=True)
    private_key = key_dir / identifier
    public_key = private_key.with_suffix(".pub")
    if not private_key.exists():
        completed = subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"grc:{identifier}", "-f", str(private_key)],
            capture_output=True, text=True, timeout=20,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "ssh-keygen failed").strip())
        private_key.chmod(0o600)
    fingerprint_result = subprocess.run(
        ["ssh-keygen", "-lf", str(public_key)], capture_output=True, text=True, timeout=10,
    )
    if fingerprint_result.returncode != 0:
        raise RuntimeError((fingerprint_result.stderr or "could not fingerprint actor key").strip())
    parts = fingerprint_result.stdout.split()
    fingerprint = parts[1] if len(parts) > 1 else ""
    public_text = public_key.read_text().strip()
    actor = {
        "id": identifier,
        "kind": kind,
        "name": name,
        "signature_algorithm": "ssh-ed25519",
        "public_key_fingerprint": fingerprint,
        "public_key": public_text,
        "created_at": (existing or {}).get("created_at") or _now(),
        "updated_at": _now(),
    }
    actors = [row for row in data.get("actors", []) if row.get("id") != identifier]
    actors.append(actor)
    data.update({"version": 2, "actors": sorted(actors, key=lambda row: str(row.get("id")))})
    _write_registry(registry, data)
    return actor


def sign_payload(payload: dict[str, Any], actor: dict[str, Any], *, key_dir: Path = KEY_DIR) -> dict[str, str]:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    payload_hash = hashlib.sha256(canonical).hexdigest()
    private_key = key_dir / str(actor["id"])
    with tempfile.TemporaryDirectory(prefix="grc-sign-") as temp:
        message = Path(temp) / "event.json"
        message.write_bytes(canonical)
        completed = subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(private_key), "-n", "grc-event-v2", str(message)],
            capture_output=True, text=True, timeout=20,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or "ssh-keygen signing failed").strip())
        signature = base64.b64encode(message.with_suffix(".json.sig").read_bytes()).decode()
    return {
        "payload_hash": f"sha256:{payload_hash}",
        "signature": signature,
        "signature_algorithm": "ssh-ed25519",
        "signature_namespace": "grc-event-v2",
    }


def signed_event(agent: str, text: str, kind: str = "note", task: Optional[str] = None,
                 *, registry: Path = REGISTRY, key_dir: Path = KEY_DIR) -> tuple[dict[str, Any], dict[str, Any]]:
    actor = ensure_actor(agent, registry=registry, key_dir=key_dir)
    event = {
        "version": 2,
        "event_id": str(uuid.uuid4()),
        "ts": _now(),
        "agent": agent,
        "actor_id": actor["id"],
        "actor_fingerprint": actor["public_key_fingerprint"],
        "run_id": os.environ.get("GRC_RUN_ID", ""),
        "type": kind,
        "text": text,
    }
    if task:
        event["task"] = task
    event.update(sign_payload(event, actor, key_dir=key_dir))
    return event, actor


def verify_event(event: dict[str, Any], actors: list[dict[str, Any]]) -> tuple[str, str]:
    """Verify a projected event envelope; never infer trust from identity metadata alone."""
    if not event.get("signature") or not event.get("actor_id"):
        return "unverified", "legacy or unsigned event"
    actor = next((row for row in actors if row.get("id") == event.get("actor_id")), None)
    if not actor or not actor.get("public_key"):
        return "unverified", "actor public key is unavailable"
    if event.get("actor_fingerprint") != actor.get("public_key_fingerprint"):
        return "tampered", "actor fingerprint does not match the projected key"
    signed_fields = {"payload_hash", "signature", "signature_algorithm", "signature_namespace"}
    payload = {key: value for key, value in event.items() if key not in signed_fields}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    expected_hash = "sha256:" + hashlib.sha256(canonical).hexdigest()
    if event.get("payload_hash") != expected_hash:
        return "tampered", "payload hash mismatch"
    try:
        signature = base64.b64decode(str(event["signature"]), validate=True)
        with tempfile.TemporaryDirectory(prefix="grc-verify-") as temp:
            signature_path = Path(temp) / "event.sig"
            allowed_path = Path(temp) / "allowed_signers"
            signature_path.write_bytes(signature)
            allowed_path.write_text(f"{actor['id']} {actor['public_key']}\n")
            completed = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", str(allowed_path), "-I", str(actor["id"]),
                 "-n", str(event.get("signature_namespace") or "grc-event-v2"), "-s", str(signature_path)],
                input=canonical, capture_output=True, timeout=20,
            )
        if completed.returncode == 0:
            return "verified", "signature and payload verified"
        return "tampered", "signature verification failed"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "unverified", "signature verification unavailable"


def project_actor_projection(grc_dir: Path, actor: dict[str, Any]) -> None:
    path = grc_dir / "actors.json"
    data = _read_registry(path)
    actors = [row for row in data.get("actors", []) if row.get("id") != actor.get("id")]
    actors.append(actor)
    data.update({"version": 2, "actors": sorted(actors, key=lambda row: str(row.get("id")))})
    _write_registry(path, data)
