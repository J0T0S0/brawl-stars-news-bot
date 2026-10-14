import io
import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import links

UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"


class FakeResponse(io.BytesIO):

    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


class StoreTests(unittest.TestCase):

    def setUp(self):
        self.store = links.LinkStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_link_get_unlink(self):
        self.store.link(1, 10, UUID, "Notch")
        self.assertEqual(self.store.get(1, 10), {"uuid": UUID, "name": "Notch"})
        self.assertIsNone(self.store.get(2, 10))  # scoped per server
        self.assertTrue(self.store.unlink(1, 10))
        self.assertIsNone(self.store.get(1, 10))
        self.assertFalse(self.store.unlink(1, 10))

    def test_relink_replaces(self):
        self.store.link(1, 10, UUID, "Notch")
        self.store.link(1, 10, "other", "Jeb_")
        self.assertEqual(self.store.get(1, 10)["name"], "Jeb_")
        self.assertIsNone(self.store.find_by_minecraft(1, UUID))

    def test_account_cannot_be_claimed_twice(self):
        self.store.link(1, 10, UUID, "Notch")
        with self.assertRaises(links.AlreadyLinked) as ctx:
            self.store.link(1, 11, UUID, "Notch")
        self.assertEqual(ctx.exception.discord_id, 10)
        self.store.link(2, 11, UUID, "Notch")  # fine on another server

    def test_prune_removes_former_members(self):
        self.store.link(1, 10, "a", "A_")
        self.store.link(1, 11, "b", "B_")
        self.store.link(2, 11, "b", "B_")
        self.assertEqual(self.store.prune(1, [10, 99]), 1)
        self.assertIsNone(self.store.get(1, 11))
        self.assertIsNotNone(self.store.get(1, 10))
        self.assertIsNotNone(self.store.get(2, 11))

    def test_forget_guild(self):
        self.store.link(1, 10, "a", "A_")
        self.store.link(2, 10, "a", "A_")
        self.assertEqual(self.store.guild_ids(), {1, 2})
        self.assertEqual(self.store.forget_guild(1), 1)
        self.assertEqual(self.store.guild_ids(), {2})


class LookupTests(unittest.TestCase):

    def test_valid_username(self):
        self.assertTrue(links.valid_username("Notch_123"))
        for bad in ("ab", "a" * 17, "bad name", "x/../y", "", None):
            self.assertFalse(links.valid_username(bad))

    def test_found(self):
        body = json.dumps({"id": UUID.replace("-", ""), "name": "Notch"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(body)):
            self.assertEqual(links.lookup_minecraft("notch"), {"uuid": UUID, "name": "Notch"})

    def test_not_found(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"", 204)):
            self.assertIsNone(links.lookup_minecraft("nobody_here"))

        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=error):
            self.assertIsNone(links.lookup_minecraft("nobody_here"))

    def test_invalid_name_skips_request(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(links.lookup_minecraft("bad name"))
        urlopen.assert_not_called()

    def test_outage(self):
        error = urllib.error.HTTPError("u", 503, "Unavailable", {}, io.BytesIO())
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(links.MojangUnavailable):
                links.lookup_minecraft("Notch")


if __name__ == "__main__":
    unittest.main()
