import json
import unittest

from chat_adapter import (
    ChatRequestError,
    completion_payload,
    stream_events,
    validate_chat_request,
)


class ChatAdapterProtocolTests(unittest.TestCase):
    def test_model_may_be_missing_empty_or_null_and_max_tokens_is_accepted(self):
        base = {"messages": [{"role": "user", "content": "你好"}], "max_tokens": 1}
        for model_value in ("__missing__", "", None):
            payload = dict(base)
            if model_value != "__missing__":
                payload["model"] = model_value
            messages, stream, model = validate_chat_request(payload)
            self.assertEqual(messages[0]["role"], "user")
            self.assertFalse(stream)
            self.assertTrue(model)

    def test_system_user_and_assistant_roles_are_accepted(self):
        messages, _, _ = validate_chat_request({"messages": [
            {"role": "system", "content": "规则"},
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "旧回答"},
        ]})
        self.assertEqual([item["role"] for item in messages], ["system", "user", "assistant"])

    def test_stream_must_be_a_json_boolean(self):
        with self.assertRaises(ChatRequestError):
            validate_chat_request({
                "messages": [{"role": "user", "content": "你好"}],
                "stream": "false",
            })

    def test_missing_messages_and_images_are_rejected(self):
        with self.assertRaises(ChatRequestError):
            validate_chat_request({})
        with self.assertRaises(ChatRequestError):
            validate_chat_request({"messages": [{
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://example.test/a.png"}}],
            }]})

    def test_non_streaming_shape_and_usage(self):
        messages = [{"role": "user", "content": "你好"}]
        payload = completion_payload(messages, "你好，我是实践小搭。", "practice-xiaoda")
        self.assertIsInstance(payload["choices"][0]["message"]["content"], str)
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            set(payload["usage"]),
            {"prompt_tokens", "completion_tokens", "total_tokens"},
        )

    def test_sse_frame_order_is_exact(self):
        messages = [{"role": "user", "content": "你好"}]
        events = list(stream_events(messages, "回答", "practice-xiaoda"))
        self.assertEqual(events[-1], "data: [DONE]\n\n")
        frames = [json.loads(item[len("data: "):].strip()) for item in events[:-1]]
        self.assertEqual(frames[0]["choices"][0]["delta"], {"role": "assistant"})
        role_frames = [frame for frame in frames if "role" in frame["choices"][0]["delta"]]
        stop_frames = [frame for frame in frames if frame["choices"][0]["finish_reason"] is not None]
        self.assertEqual(len(role_frames), 1)
        self.assertEqual(len(stop_frames), 1)
        stop = stop_frames[0]
        self.assertEqual(stop["choices"][0]["delta"], {})
        self.assertEqual(stop["choices"][0]["finish_reason"], "stop")
        self.assertEqual(set(stop["usage"]), {"prompt_tokens", "completion_tokens", "total_tokens"})


if __name__ == "__main__":
    unittest.main()
