import ast
import copy
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

from core.sqlite import AsyncSQLiteDB
from core.template import secure_render_template
from jinja2 import Environment, StrictUndefined

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
    from core.cache import CacheService
    from core.jx3api_data import JX3APIService
    from core.request import APIErrorResponse


class MessageChain:
    def message(self, text):
        self.text = text
        return self


class Event:
    def __init__(self, text="装备 江南 旧角色", sender="requester"):
        self.text = text
        self.sender = sender
        self.send = AsyncMock()

    def get_sender_id(self):
        return self.sender

    def get_message_str(self):
        return self.text

    def plain_result(self, text):
        return text

    def image_result(self, path):
        return ("image", path)


class Waiter:
    def __init__(self):
        self.replies = [Event("2")]
        self.timeout = False
        self.calls = 0
        self.controller = types.SimpleNamespace(stop=Mock())

    def __call__(self, timeout):
        self.configured_timeout = timeout

        def decorate(callback):
            async def wait(event):
                self.calls += 1
                self.controller.stop.reset_mock()
                if self.timeout:
                    raise TimeoutError
                for reply in self.replies:
                    await callback(self.controller, reply)
                    if self.controller.stop.called:
                        return
                raise TimeoutError

            return wait

        return decorate


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / "core" / "message.py").read_text(encoding="utf-8"))
builder_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
builder_class.body = [
    node
    for node in builder_class.body
    if getattr(node, "name", "") in {"handler_plain_image_msg", "zhuangbei"}
]
namespace = {
    "AstrMessageEvent": object,
    "SessionController": object,
    "MessageChain": MessageChain,
    "logger": Mock(),
    "Any": Any,
}
exec(
    compile(ast.Module(body=[builder_class], type_ignores=[]), "message.py", "exec"),
    namespace,
)
MessageBuilder = namespace["MessageBuilder"]


class EquipmentTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = AsyncSQLiteDB(":memory:")
        await self.db.connect()
        self.cache = CacheService(self.db, Path(self.temp_dir.name) / "images")
        await self.cache.initialize()
        self.command_token = self.cache.enter_command("装备", ["江南", "旧角色"])
        self.service = JX3APIService(
            {"jx3api_token": "test-token"}, self.db, self.cache
        )
        self.records = {
            "serverName": "梦江南",
            "roleName": "测试角色",
            "equipList": [
                {
                    "kungfuName": "易筋经",
                    "equipType": "竞技对抗",
                    "cacheTime": 1792502494,
                },
                {
                    "kungfuName": "洗髓经",
                    "equipType": "秘境挑战",
                    "cacheTime": 1792502495,
                },
            ],
        }

        async def api_get(url, *, params, **kwargs):
            if url.endswith("/calcd/records"):
                return copy.deepcopy(self.records)
            if url.endswith("/card/record"):
                return {"showAvatar": "https://example.com/card.png"}
            self.assertTrue(url.endswith("/calcd/detail"))
            return {
                "serverName": params["server"],
                "roleName": params["name"],
                "forceName": "少林",
                "kungfuName": params["kungfu"],
                "equipType": params["type"],
                "cacheTime": 1792502494,
                "equipScore": 670564,
                "equipList": [],
                "panelList": [{"name": "会心等级", "value": 66497, "percent": 33.63}],
            }

        self.api_get = AsyncMock(side_effect=api_get)
        self.service._api.get = self.api_get
        self.waiter = Waiter()
        namespace["session_waiter"] = self.waiter
        self.builder = MessageBuilder()
        self.builder.jx3api = self.service
        self.builder.cache = self.cache
        self.results = []

        async def image_result(event, action, **kwargs):
            if not self.waiter.timeout:
                self.waiter.controller.stop.assert_called_once()
            result = await action()
            self.results.append(result)
            if result["code"] == 200:
                await event.send(event.image_result("rendered.png"))
            else:
                await event.send(event.plain_result(result["msg"]))

        self.builder.T2I_image_msg = AsyncMock(side_effect=image_result)
        self.event = Event()

    async def asyncTearDown(self):
        self.cache.leave_command(self.command_token)
        await self.service.close()
        await self.cache.stop()
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_menu_selection_queries_detail_and_reuses_attribute_template(self):
        await self.builder.zhuangbei(self.event, "江南", "旧角色")
        self.event.send.assert_awaited_once_with(
            "梦江南-测试角色\n1、易筋经 竞技对抗\n2、洗髓经 秘境挑战"
        )
        self.assertEqual(self.waiter.configured_timeout, 15)
        first, detail, card = self.api_get.await_args_list
        self.assertEqual(first.args, ("https://www.jx3api.com/calcd/records",))
        self.assertEqual(
            first.kwargs["params"],
            {"server": "江南", "name": "旧角色", "token": "test-token"},
        )
        self.assertEqual(detail.args, ("https://www.jx3api.com/calcd/detail",))
        self.assertEqual(
            detail.kwargs["params"],
            {
                "server": "梦江南",
                "name": "测试角色",
                "kungfu": "洗髓经",
                "type": "秘境挑战",
                "format": "calcd",
                "token": "test-token",
            },
        )
        self.assertEqual(card.kwargs["params"]["server"], "梦江南")
        result = self.results[0]
        self.assertEqual(result["code"], 200)
        html = (
            Environment(undefined=StrictUndefined)
            .from_string(secure_render_template(result["temp"]))
            .render(
                **result["data"], icons={"sect": {}}, data_time="2026-10-24 18:00:00"
            )
        )
        self.assertIn("洗髓经", html)
        self.assertIn("670,564", html)
        self.assertIn("33.63%", html)
        self.assertNotIn("套装效果", html)
        self.assertIn("attribute-layout", html)
        self.waiter.replies[0].send.assert_awaited_once_with(("image", "rendered.png"))

    async def test_timeout_selects_first_record_once(self):
        self.waiter.timeout = True
        await self.builder.zhuangbei(self.event, "江南", "旧角色")
        detail = self.api_get.await_args_list[1]
        self.assertEqual(detail.kwargs["params"]["kungfu"], "易筋经")
        self.assertEqual(detail.kwargs["params"]["type"], "竞技对抗")
        self.builder.T2I_image_msg.assert_awaited_once()
        self.assertEqual(len(self.results), 1)

    async def test_invalid_replies_do_not_request_details(self):
        for text in ("999", "文字", "-1"):
            with self.subTest(text=text):
                self.waiter.replies = [Event(text)]
                await self.builder.zhuangbei(Event(), "江南", "旧角色")
                self.waiter.controller.stop.assert_called_once()
                self.builder.T2I_image_msg.assert_not_awaited()
        self.assertEqual(self.api_get.await_count, 1)

    async def test_other_users_cannot_select_for_requester(self):
        other = Event("1", sender="someone-else")
        self.waiter.replies.insert(0, other)
        await self.builder.zhuangbei(self.event, "江南", "旧角色")
        other.send.assert_not_awaited()
        self.assertEqual(
            self.api_get.await_args_list[1].kwargs["params"]["kungfu"], "洗髓经"
        )

    async def test_empty_records_or_api_error_do_not_start_waiting(self):
        for response in ({"equipList": []}, APIErrorResponse(403, "令牌权限不足")):
            with self.subTest(response=type(response).__name__):
                await self.cache.clear_item("api", "/calcd/records")
                self.records = response
                await self.builder.zhuangbei(Event(), "江南", "旧角色")
                self.assertEqual(self.waiter.calls, 0)
                self.builder.T2I_image_msg.assert_not_awaited()

    async def test_invalid_selected_records_do_not_fall_back_to_current_attributes(
        self,
    ):
        for selected in ({}, {"1": "易筋经 竞技对抗"}, {"1": {}}, {"1": {}, "2": {}}):
            with self.subTest(selected=selected):
                result = await self.service.shuxing("江南", "旧角色", selected)
                self.assertNotEqual(result["code"], 200)
                self.assertEqual(result["msg"], "装备选项数据格式异常")
        self.api_get.assert_not_awaited()

    async def test_legacy_string_menus_still_pass_one_selected_entry(self):
        action1 = AsyncMock(return_value={"1": "总览", "2": "武学"})
        action2 = AsyncMock(return_value={"code": 200})
        await self.builder.handler_plain_image_msg(self.event, action1, action2)
        self.event.send.assert_awaited_once_with(
            "请选择序号（15后自动选择第一条）\n1、总览\n2、武学"
        )
        action2.assert_awaited_once_with({"2": "武学"})

    async def test_selected_sets_have_distinct_cache_variants_and_api_entries(self):
        for choice in ("1", "2", "1"):
            self.waiter.replies = [Event(choice)]
            await self.builder.zhuangbei(Event(), "江南", "旧角色")
        calls = self.builder.T2I_image_msg.await_args_list
        self.assertEqual(calls[0].kwargs["cache_name"], "装备")
        self.assertNotEqual(
            calls[0].kwargs["cache_variant"], calls[1].kwargs["cache_variant"]
        )
        self.assertEqual(
            calls[0].kwargs["cache_variant"], calls[2].kwargs["cache_variant"]
        )
        details = [
            call
            for call in self.api_get.await_args_list
            if call.args[0].endswith("/calcd/detail")
        ]
        self.assertEqual(len(details), 2)
        dashboard = await self.cache.dashboard()
        self.assertTrue(
            {"/calcd/records", "/calcd/detail"}
            <= {item["name"] for item in dashboard["api"]}
        )


if __name__ == "__main__":
    unittest.main()
