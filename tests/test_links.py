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
BS = links.BRAWL_STARS
MC = links.MINECRAFT


class FakeResponse(io.BytesIO):

    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status


def http_error(code):
    return urllib.error.HTTPError("u", code, "err", {}, io.BytesIO())


class StoreTests(unittest.TestCase):

    def setUp(self):
        self.store = links.LinkStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_link_both_games(self):
        self.store.link(1, 10, MC, UUID, "Notch")
        self.store.link(1, 10, BS, "#2PP", "Player")
        self.assertEqual(self.store.get(1, 10), {
            MC: {"id": UUID, "name": "Notch"},
            BS: {"id": "#2PP", "name": "Player"},
        })
        self.assertEqual(self.store.get(2, 10), {})  # scoped per server

    def test_unlink_one_or_all(self):
        self.store.link(1, 10, MC, UUID, "Notch")
        self.store.link(1, 10, BS, "#2PP", None)
        self.assertEqual(self.store.unlink(1, 10, BS), 1)
        self.assertEqual(list(self.store.get(1, 10)), [MC])
        self.assertEqual(self.store.unlink(1, 10, BS), 0)
        self.store.link(1, 10, BS, "#2PP", None)
        self.assertEqual(self.store.unlink(1, 10), 2)
        self.assertEqual(self.store.get(1, 10), {})

    def test_relink_replaces(self):
        self.store.link(1, 10, BS, "#2PP", "A")
        self.store.link(1, 10, BS, "#8QU", "B")
        self.assertEqual(self.store.get(1, 10)[BS]["id"], "#8QU")
        self.assertIsNone(self.store.find_owner(1, BS, "#2PP"))

    def test_account_cannot_be_claimed_twice(self):
        self.store.link(1, 10, BS, "#2PP", None)
        with self.assertRaises(links.AlreadyLinked) as ctx:
            self.store.link(1, 11, BS, "#2PP", None)
        self.assertEqual(ctx.exception.discord_id, 10)
        self.store.link(2, 11, BS, "#2PP", None)  # fine on another server
        self.store.link(1, 11, MC, "#2PP", None)  # ids are per game

    def test_prune_removes_former_members(self):
        self.store.link(1, 10, MC, "a", "A_")
        self.store.link(1, 11, MC, "b", "B_")
        self.store.link(1, 11, BS, "#2PP", None)
        self.store.link(2, 11, MC, "b", "B_")
        self.assertEqual(self.store.prune(1, [10, 99]), 1)
        self.assertEqual(self.store.get(1, 11), {})
        self.assertTrue(self.store.get(1, 10))
        self.assertTrue(self.store.get(2, 11))

    def test_forget_guild(self):
        self.store.link(1, 10, MC, "a", "A_")
        self.store.link(2, 10, MC, "a", "A_")
        self.assertEqual(self.store.guild_ids(), {1, 2})
        self.assertEqual(self.store.forget_guild(1), 1)
        self.assertEqual(self.store.guild_ids(), {2})


class MinecraftLookupTests(unittest.TestCase):

    def test_valid_username(self):
        self.assertTrue(links.valid_username("Notch_123"))
        for bad in ("ab", "a" * 17, "bad name", "x/../y", "", None):
            self.assertFalse(links.valid_username(bad))

    def test_found(self):
        body = json.dumps({"id": UUID.replace("-", ""), "name": "Notch"}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(body)):
            self.assertEqual(links.lookup_minecraft("notch"), {"id": UUID, "name": "Notch"})

    def test_not_found(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"", 204)):
            self.assertIsNone(links.lookup_minecraft("nobody_here"))
        with mock.patch("urllib.request.urlopen", side_effect=http_error(404)):
            self.assertIsNone(links.lookup_minecraft("nobody_here"))

    def test_invalid_name_skips_request(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertIsNone(links.lookup_minecraft("bad name"))
        urlopen.assert_not_called()

    def test_outage(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(503)):
            with self.assertRaises(links.LookupUnavailable):
                links.lookup_minecraft("Notch")


class BrawlStarsLookupTests(unittest.TestCase):

    def test_normalize_tag(self):
        self.assertEqual(links.normalize_tag("#2pp"), "2PP")
        self.assertEqual(links.normalize_tag(" #8q uo "), "8QU0")  # O -> 0
        for bad in ("", "#", "#ABC", "#2P", None, "#2PP/../x"):
            self.assertIsNone(links.normalize_tag(bad))

    def test_without_api_key_only_validates(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertEqual(
                links.lookup_brawl_stars("#2pp", ""),
                {"id": "#2PP", "name": None, "trophies": None},
            )
            self.assertIsNone(links.lookup_brawl_stars("#nope", ""))
        urlopen.assert_not_called()

    def test_found(self):
        body = json.dumps({"tag": "#2PP", "name": "Player", "trophies": 50000}).encode()
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(body)) as urlopen:
            profile = links.lookup_brawl_stars("2pp", "KEY")
        self.assertEqual(profile, {"id": "#2PP", "name": "Player", "trophies": 50000})
        request = urlopen.call_args[0][0]
        self.assertTrue(request.full_url.endswith("/players/%232PP"))
        self.assertEqual(request.get_header("Authorization"), "Bearer KEY")

    def test_not_found(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(404)):
            self.assertIsNone(links.lookup_brawl_stars("#2PP", "KEY"))

    def test_bad_key_is_reported(self):
        with mock.patch("urllib.request.urlopen", side_effect=http_error(403)):
            with self.assertRaises(links.LookupUnavailable):
                links.lookup_brawl_stars("#2PP", "KEY")


if __name__ == "__main__":
    unittest.main()
