"""Exercise the actual dispatch methods without starting AstrBot or its services."""

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def load_dispatch_plugin(source_path: Path):
    # Compile the production methods, rather than reproducing their parser in
    # tests. This keeps the regression runnable with the Python standard library
    # and avoids registering a plugin or loading real credentials/databases.
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    plugin = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Jx3ApiPlugin"
    )
    names = {"parse_message", "resolve_command", "on_all_message"}
    methods = [node for node in plugin.body if getattr(node, "name", "") in names]
    if {node.name for node in methods} != names:
        raise AssertionError("Production command dispatch methods are missing")
    for method in methods:
        method.decorator_list = []
    plugin.bases = []
    plugin.keywords = []
    plugin.decorator_list = []
    plugin.body = methods
    module = ast.Module(body=[plugin], type_ignores=[])
    namespace = {"AstrMessageEvent": object, "logger": Mock()}
    exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), namespace)
    return namespace["Jx3ApiPlugin"]


class CommandRoutingTest(unittest.IsolatedAsyncioTestCase):
    plugin_class = load_dispatch_plugin(Path(__file__).resolve().parents[1] / "main.py")

    def setUp(self):
        self.plugin = self.plugin_class()
        self.plugin.prefix = {"enable": True, "text": "jx3"}
        self.handler = Mock()
        self.plugin.command_map = {"日常": self.handler}

    @staticmethod
    def event(processed, *, awake=True, original=None):
        return SimpleNamespace(
            is_at_or_wake_command=awake,
            message_str=processed,
            message_obj=SimpleNamespace(
                message_str=processed if original is None else original
            ),
            unified_msg_origin="test:GroupMessage:example",
            stop_event=Mock(),
            should_call_llm=Mock(),
            plain_result=lambda message: message,
        )

    def test_unawakened_bot_rejects_matching_plugin_command(self):
        event = self.event("jx3 日常", awake=False)
        self.assertIsNone(self.plugin.resolve_command(event))

    def test_host_wake_then_plugin_prefix_preserves_arguments(self):
        event = self.event("jx3 日常 梦江南", original="/jx3 日常 梦江南")
        self.assertEqual(
            self.plugin.resolve_command(event), ("日常", ["梦江南"], self.handler)
        )

    def test_awake_event_still_requires_enabled_plugin_prefix(self):
        self.assertIsNone(self.plugin.resolve_command(self.event("日常")))

    def test_raw_text_cannot_override_processed_text(self):
        event = self.event("普通聊天", original="jx3 日常")
        self.assertIsNone(self.plugin.resolve_command(event))

    def test_disabled_prefix_does_not_disable_host_wake_gate(self):
        self.plugin.prefix["enable"] = False
        self.assertIsNone(
            self.plugin.resolve_command(self.event("日常", awake=False))
        )
        self.assertEqual(
            self.plugin.resolve_command(self.event("日常", original="/日常")),
            ("日常", [], self.handler),
        )

    def test_blank_plugin_prefix_retains_existing_behavior(self):
        self.plugin.prefix["text"] = "   "
        self.assertEqual(
            self.plugin.resolve_command(self.event("日常")),
            ("日常", [], self.handler),
        )
        self.assertIsNone(
            self.plugin.resolve_command(self.event("日常", awake=False))
        )

    def test_whitespace_empty_and_unrelated_messages(self):
        for text in ("", "  ", "jx3", "jx3 不存在", "普通聊天"):
            with self.subTest(text=text):
                self.assertIsNone(self.plugin.resolve_command(self.event(text)))
        self.assertEqual(
            self.plugin.resolve_command(self.event("  jx3  日常  ")),
            ("日常", [], self.handler),
        )

    async def test_rejected_message_does_not_stop_other_plugins_or_llm(self):
        event = self.event("jx3 日常", awake=False)
        self.plugin.session_control = Mock()
        self.plugin._call_with_auto_args = AsyncMock()
        self.assertEqual([result async for result in self.plugin.on_all_message(event)], [])
        event.stop_event.assert_not_called()
        event.should_call_llm.assert_not_called()
        self.plugin.session_control.is_allowed.assert_not_called()
        self.plugin._call_with_auto_args.assert_not_awaited()

    async def test_awakened_command_dispatches_once(self):
        event = self.event("jx3 日常 梦江南", original="/jx3 日常 梦江南")
        self.plugin.session_control = SimpleNamespace(is_allowed=Mock(return_value=True))
        self.plugin._prepare_server_args = AsyncMock(return_value=["梦江南"])
        self.plugin._prepare_kungfu_args = Mock(side_effect=lambda handler, args: args)
        self.plugin.cache = Mock()
        self.plugin._call_with_auto_args = AsyncMock(return_value="查询结果")

        self.assertEqual(
            [result async for result in self.plugin.on_all_message(event)], ["查询结果"]
        )
        event.stop_event.assert_called_once()
        self.plugin._call_with_auto_args.assert_awaited_once_with(
            self.handler, event, ["梦江南"]
        )
        self.plugin.cache.leave_command.assert_called_once_with(
            self.plugin.cache.enter_command.return_value
        )


if __name__ == "__main__":
    unittest.main()
