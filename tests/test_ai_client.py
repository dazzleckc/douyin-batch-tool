import asyncio
import unittest

from src.ai_client import DoubaoClient


class FakeElement:
    def __init__(self, *, visible=True, editable=True):
        self.visible = visible
        self.editable = editable

    async def is_visible(self):
        return self.visible

    async def is_editable(self):
        return self.editable


class FakePage:
    def __init__(self, elements):
        self.elements = elements

    async def query_selector_all(self, selector):
        return self.elements.get(selector, [])


class DoubaoInputSelectorTests(unittest.TestCase):
    def setUp(self):
        self.original_timeout = DoubaoClient.INPUT_TIMEOUT
        DoubaoClient.INPUT_TIMEOUT = 0

    def tearDown(self):
        DoubaoClient.INPUT_TIMEOUT = self.original_timeout

    def test_prefers_current_testid_textarea(self):
        current = FakeElement()
        fallback = FakeElement()
        page = FakePage(
            {
                'textarea[data-testid="chat_input_input"]': [current],
                'textarea[placeholder*="发消息"]': [fallback],
            }
        )

        element, selector = asyncio.run(DoubaoClient._find_chat_input(page))

        self.assertIs(element, current)
        self.assertEqual(selector, 'textarea[data-testid="chat_input_input"]')

    def test_uses_visible_editable_contenteditable_fallback(self):
        hidden = FakeElement(visible=False)
        readonly = FakeElement(editable=False)
        contenteditable = FakeElement()
        page = FakePage(
            {
                'textarea[data-testid="chat_input_input"]': [hidden],
                '[data-testid="chat_input_input"]': [readonly],
                '[contenteditable="true"][data-slate-editor="true"]': [
                    contenteditable
                ],
            }
        )

        element, selector = asyncio.run(DoubaoClient._find_chat_input(page))

        self.assertIs(element, contenteditable)
        self.assertEqual(
            selector, '[contenteditable="true"][data-slate-editor="true"]'
        )

    def test_returns_none_when_no_editable_input_exists(self):
        page = FakePage(
            {'textarea[data-testid="chat_input_input"]': [FakeElement(editable=False)]}
        )

        element, selector = asyncio.run(DoubaoClient._find_chat_input(page))

        self.assertIsNone(element)
        self.assertEqual(selector, "")


if __name__ == "__main__":
    unittest.main()
