import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core.sqlite import AsyncSQLiteDB

with patch.dict(
    "sys.modules",
    {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.SimpleNamespace(logger=Mock()),
    },
):
    from core.cache import CacheService


class CardCacheTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = AsyncSQLiteDB(":memory:")
        await self.db.connect()
        self.cache = CacheService(self.db, Path(self.temp_dir.name) / "images")
        await self.cache.initialize()
        await self.cache.stop()

    async def asyncTearDown(self):
        await self.cache.stop()
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_dashboard_lists_current_card_endpoints(self):
        dashboard = await self.cache.dashboard()
        endpoints = {item["name"] for item in dashboard["api"]}
        self.assertIn("/card/record", endpoints)
        self.assertIn("/card/records", endpoints)
        self.assertIn("/card/random", endpoints)
        self.assertNotIn("/card/cached", endpoints)
        self.assertEqual(self.cache.get_ttl("api", "/card/random"), 0)

    async def test_legacy_ttl_migrates_and_old_payload_is_discarded(self):
        for ttl in (0, 1234):
            with self.subTest(ttl=ttl):
                await self.cache.set_ttl("api", "/card/record", inherit=True)
                await self.cache.set_ttl("api", "/card/cached", ttl)
                await self.cache._save_api_payload(
                    "old-card", "/card/cached", {"showAvatar": "old-image"}, 300
                )
                await self.cache.initialize()
                await self.cache.stop()
                self.assertEqual(self.cache.get_ttl("api", "/card/record"), ttl)
                self.assertIsNone(
                    await self.db.fetch_one(
                        "SELECT * FROM cache_settings WHERE cache_name=?",
                        ("/card/cached",),
                    )
                )
                self.assertIsNone(
                    await self.db.fetch_one(
                        "SELECT * FROM api_response_cache WHERE endpoint=?",
                        ("/card/cached",),
                    )
                )
                await self.cache.initialize()
                await self.cache.stop()
                self.assertEqual(self.cache.get_ttl("api", "/card/record"), ttl)

    async def test_explicit_new_ttl_wins_and_other_settings_survive(self):
        await self.cache.set_ttl("api", "/card/cached", 100)
        await self.cache.set_ttl("api", "/card/record", 200)
        await self.cache.set_ttl("api", "/card/records", 400)
        await self.cache.set_ttl("api", "*", 500)
        await self.cache.set_ttl("image", "/card/cached", 600)
        await self.cache.initialize()
        self.assertEqual(self.cache.get_ttl("api", "/card/record"), 200)
        self.assertEqual(self.cache.get_ttl("api", "/card/records"), 400)
        self.assertEqual(self.cache.get_ttl("api", "*"), 500)
        self.assertEqual(self.cache.get_ttl("image", "/card/cached"), 600)

    async def test_new_endpoint_hits_cache_then_respects_disabled_ttl(self):
        await self.cache.set_ttl("api", "/card/cached", 1200)
        await self.cache.initialize()
        fetcher = AsyncMock(return_value={"showAvatar": "new-image"})
        params = {"server": "test-server", "name": "test-role"}
        first, miss = await self.cache.request_api(
            "/card/record", params, fetcher, bool
        )
        second, hit = await self.cache.request_api(
            "/card/record", params, fetcher, bool
        )
        self.assertEqual(first, second)
        self.assertFalse(miss["hit"])
        self.assertTrue(hit["hit"])
        self.assertEqual(hit["ttl_seconds"], 1200)
        fetcher.assert_awaited_once()
        await self.cache.set_ttl("api", "/card/record", 0)
        for _ in range(2):
            _, metadata = await self.cache.request_api(
                "/card/record", params, fetcher, bool
            )
            self.assertFalse(metadata["hit"])
            self.assertEqual(metadata["ttl_seconds"], 0)
        self.assertEqual(fetcher.await_count, 3)


if __name__ == "__main__":
    unittest.main()
