"""Exercise the local Compose stack through the fixture sign-in proxy."""

import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import time
import unittest
from urllib.parse import urlencode
import uuid


def request(path, *, user=None, token=None, body=None, headers=None, address="proxy:8080"):
    headers = dict(headers or {})
    if user:
        credentials = base64.b64encode(f"{user}:microtask".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["X-Microtask-Token"] = token
    connection = http.client.HTTPConnection(address, timeout=5)
    try:
        connection.request("GET" if body is None else "POST", path,
                           body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, payload
    finally:
        connection.close()


class SharedWorkspaceSmokeTest(unittest.TestCase):
    def test_two_users_share_cards_and_conflicting_saves_are_rejected(self):
        deadline = time.monotonic() + 25
        while True:
            try:
                status, payload = request("/api/items", user="alice")
                if status == 200:
                    break
                failure = f"HTTP {status}: {payload!r}"
            except OSError as error:
                failure = str(error)
            if time.monotonic() >= deadline:
                self.fail(f"Shared dashboard did not become ready: {failure}")
            time.sleep(0.25)

        self.assertEqual(request("/api/items")[0], 401)
        spoofed = {"X-Auth-Request-Email": "bob@example.test"}
        self.assertEqual(request("/api/items", address="shared:8000", headers=spoofed)[0], 403)
        status, alice = request("/api/items", user="alice", headers=spoofed)
        self.assertEqual(status, 200, alice)
        self.assertEqual(alice["meta"]["hosted"], {"user": "alice", "admin": True})
        status, bob = request("/api/items", user="bob")
        self.assertEqual(status, 200, bob)
        self.assertEqual(bob["meta"]["hosted"], {"user": "bob", "admin": False})
        tokens = {"alice": alice["meta"]["api_token"], "bob": bob["meta"]["api_token"]}

        fixture = uuid.uuid4().hex
        status, payload = request("/api/draft-save", user="alice", token=tokens["alice"], body={
            "kind": "task", "title": f"Compose smoke test {fixture}", "body": "Original body",
            "capture_id": str(uuid.uuid4()),
        })
        self.assertEqual(status, 200, payload)
        draft = payload["saved_draft"]
        status, payload = request("/api/drafts", user="bob")
        self.assertEqual(status, 200, payload)
        self.assertNotIn(draft["id"], {item["id"] for item in payload["drafts"]})
        status, payload = request("/api/draft-finish", user="alice", token=tokens["alice"], body={
            "id": draft["id"], "expected_hash": draft["content_hash"],
        })
        self.assertEqual(status, 200, payload)
        ref = payload["saved_ref"]
        card = next(item for item in payload["tasks"] if item["ref"] == ref)
        path = "/api/item?" + urlencode({"id": card["id"]})
        hashes = {}
        for user in tokens:
            status, payload = request(path, user=user)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["item"]["body"].strip(), "Original body")
            hashes[user] = payload["item"]["content_hash"]
        self.assertEqual(hashes["alice"], hashes["bob"])

        def save(user):
            return user, request("/api/card-edit", user=user, token=tokens[user], body={
                "ref": ref, "expected_hash": hashes[user], "body": f"Saved by {user}",
            })

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = dict(pool.map(save, tokens))
        self.assertEqual(sorted(status for status, _ in results.values()), [200, 409], results)
        winner = next(user for user, (status, _) in results.items() if status == 200)
        for user in tokens:
            status, payload = request(path, user=user)
            self.assertEqual(status, 200, payload)
            self.assertEqual(payload["item"]["body"].strip(), f"Saved by {winner}")


if __name__ == "__main__":
    unittest.main()
