import copy
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

host = types.ModuleType("astrbot")
host.logger = Mock()
api = types.ModuleType("astrbot.api")
api.logger = host.logger
api.AstrBotConfig = dict
with patch.dict(
    "sys.modules",
    {
        "astrbot": host,
        "astrbot.api": api,
        "astrbot.api.message_components": types.ModuleType(
            "astrbot.api.message_components"
        ),
    },
):
    from core.jx3api_data import JX3APIService
    from core.request import APIErrorResponse


PREVIEW = {
    "server": "眉间雪",
    "mapName": "阴山大草原",
    "horse": "赤兔",
    "date": "2026-10-24",
    "time": 1792851870,
    "window": "22:24~22:29",
    "status": "预告",
}
SPAWNED = {
    "server": "山海相逢",
    "mapName": "阴山大草原",
    "horse": "赤兔",
    "date": "2026-10-23",
    "time": 1792766912,
    "window": "22:48~22:53",
    "status": "出世",
}
QUERIES = (
    ("benrichitu", "/chitu/records", "今日赤兔"),
    ("benzhouchitu", "/chitu/week/records", "本周赤兔"),
)


class ChituTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = JX3APIService({"jx3api_token": "test-token"}, None)
        self.api_get = AsyncMock()
        self.service._api.get = self.api_get

    async def asyncTearDown(self):
        await self.service.close()

    async def test_today_displays_new_fields_and_uses_existing_endpoint(self):
        self.api_get.return_value = [copy.deepcopy(PREVIEW)]
        result = await self.service.benrichitu()
        self.assertEqual(result["code"], 200)
        self.assertEqual(
            result["data"],
            "今日赤兔\n时间：2026-10-24 22:24~22:29\n区服：眉间雪\n"
            "地图：阴山大草原\n马匹：赤兔\n状态：预告",
        )
        self.assertEqual(
            self.api_get.await_args.args, ("https://www.jx3api.com/chitu/records",)
        )
        self.assertEqual(
            self.api_get.await_args.kwargs["params"], {"token": "test-token"}
        )

    async def test_week_keeps_both_statuses_and_separates_records(self):
        self.api_get.return_value = [copy.deepcopy(PREVIEW), copy.deepcopy(SPAWNED)]
        result = await self.service.benzhouchitu()
        self.assertEqual(result["code"], 200)
        self.assertIn("时间：2026-10-24 22:24~22:29", result["data"])
        self.assertIn("状态：预告\n\n时间：2026-10-23 22:48~22:53", result["data"])
        self.assertIn("区服：山海相逢", result["data"])
        self.assertTrue(result["data"].endswith("状态：出世"))
        self.assertEqual(result["data"].count("马匹：赤兔"), 2)
        self.assertEqual(
            self.api_get.await_args.args, ("https://www.jx3api.com/chitu/week/records",)
        )

    async def test_missing_window_falls_back_to_shanghai_timestamp(self):
        record = {**PREVIEW, "window": None}
        for method, _, _ in QUERIES:
            with self.subTest(method=method):
                self.api_get.return_value = [record]
                result = await getattr(self.service, method)()
                self.assertIn("时间：2026-10-24 22:24:30", result["data"])
                self.assertNotIn("None", result["data"])

    async def test_legacy_record_without_new_fields_still_displays(self):
        record = {key: PREVIEW[key] for key in ("date", "server", "mapName")}
        for method, _, _ in QUERIES:
            with self.subTest(method=method):
                self.api_get.return_value = [record]
                result = await getattr(self.service, method)()
                self.assertEqual(result["code"], 200)
                self.assertIn("时间：2026-10-24\n区服：眉间雪", result["data"])
                self.assertIn("状态：未知", result["data"])

    async def test_cross_midnight_window_preserves_api_date(self):
        self.api_get.return_value = [{**PREVIEW, "window": "23:59~00:04"}]
        result = await self.service.benzhouchitu()
        self.assertIn("时间：2026-10-24 23:59~00:04", result["data"])

    async def test_empty_results_and_api_errors_remain_readable(self):
        for method, _, title in QUERIES:
            with self.subTest(method=method):
                self.api_get.return_value = []
                result = await getattr(self.service, method)()
                self.assertEqual(result["data"], f"{title} 暂无数据")
                self.api_get.return_value = APIErrorResponse(403, "令牌权限不足")
                result = await getattr(self.service, method)()
                self.assertNotEqual(result["code"], 200)
                self.assertEqual(result["msg"], "令牌权限不足")


if __name__ == "__main__":
    unittest.main()
