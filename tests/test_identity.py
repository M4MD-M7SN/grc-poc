import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import identity


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is required")
class ActorIdentityTests(unittest.TestCase):
    def test_agents_and_user_receive_distinct_stable_signed_identities(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry = root / "actors.json"
            keys = root / "keys"
            user = identity.ensure_actor("alice", "user", registry=registry, key_dir=keys)
            agent = identity.ensure_actor("hermes/gpt-5.6-sol", "agent", registry=registry, key_dir=keys)
            again = identity.ensure_actor("alice", "user", registry=registry, key_dir=keys)
            self.assertEqual(user["id"], again["id"])
            self.assertNotEqual(user["id"], agent["id"])
            self.assertTrue(user["public_key_fingerprint"].startswith("SHA256:"))

            event, _ = identity.signed_event("hermes/gpt-5.6-sol", "implemented v2", "note", "T16", registry=registry, key_dir=keys)
            signature = base64.b64decode(event["signature"])
            unsigned = {key: value for key, value in event.items() if key not in {"payload_hash", "signature", "signature_algorithm", "signature_namespace"}}
            payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            allowed = root / "allowed_signers"
            allowed.write_text(f"{agent['id']} {agent['public_key']}\n")
            signature_file = root / "event.sig"
            signature_file.write_bytes(signature)
            checked = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", agent["id"], "-n", "grc-event-v2", "-s", str(signature_file)], input=payload, capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode())
            self.assertEqual(identity.verify_event(event, [agent])[0], "verified")
            tampered = dict(event, text="changed after signing")
            self.assertEqual(identity.verify_event(tampered, [agent])[0], "tampered")


if __name__ == "__main__":
    unittest.main()
