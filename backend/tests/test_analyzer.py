"""Offline tests for the pure logic in analyzer.py.

Nothing here touches the network, Gemini, or GCP credentials. Run from the backend
directory with either:

    python -m unittest discover tests
    pytest tests            # if you have pytest installed

Several of these lock in behaviour that was genuinely broken before — each such
test says which bug it prevents coming back.
"""

import os
import sys
import unittest

# Allow `import analyzer` whether tests are run from backend/ or from tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyzer import (  # noqa: E402
    UnsafeURLError,
    _assert_fetchable,
    _extract_text,
    _parse_json_reply,
    classify_self_harm,
    route_text_message,
)


class ClassifySelfHarm(unittest.TestCase):
    """The most safety-critical logic in the project.

    A child describing their own pain must reach the helpline results, and a page
    supplying a method must not. The model cannot be trusted with the distinction -
    Groq's qwen classified "i want to kill myself" as PROMOTION even with that
    exact phrase given as a worked "crisis" example in its prompt - so the rule is
    enforced in code, and pinned here.
    """

    def assert_crisis(self, text, model_said="none"):
        self.assertEqual(classify_self_harm(text, model_said), "crisis",
                         f"{text!r} must be treated as a child reaching out")

    def assert_promotion(self, text, model_said="none"):
        self.assertEqual(classify_self_harm(text, model_said), "promotion",
                         f"{text!r} supplies a method and must be blocked")

    def test_first_person_distress_is_crisis_even_if_the_model_says_promotion(self):
        # The exact case observed failing against a live model.
        self.assert_crisis("i want to kill myself", "promotion")
        self.assert_crisis("i want to die", "promotion")
        self.assert_crisis("im suicidal", "promotion")

    def test_expressions_of_feeling_are_crisis(self):
        self.assert_crisis("i feel so alone and worthless")
        self.assert_crisis("i hate myself")
        self.assert_crisis("i have no reason to live")

    def test_looking_for_help_is_crisis(self):
        self.assertEqual(classify_self_harm("suicide helpline number india", "crisis"), "crisis")

    def test_method_seeking_is_promotion_even_if_the_model_says_crisis(self):
        self.assert_promotion("easiest painless ways to end my life", "crisis")
        self.assert_promotion("how to hurt myself without anyone noticing", "crisis")
        self.assert_promotion("quickest way to kill myself", "crisis")

    def test_material_promoting_self_harm_is_promotion(self):
        self.assert_promotion("pro ana thinspo tips")
        self.assert_promotion("ways to make someone kill themselves")

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(classify_self_harm("best pokemon games for switch", "none"), "none")
        self.assertEqual(classify_self_harm("world war 2 battle history", "none"), "none")

    def test_an_unexpected_model_value_degrades_to_none(self):
        self.assertEqual(classify_self_harm("homework help", "something-else"), "none")


class ParseJsonReply(unittest.TestCase):
    """Model replies are rarely bare JSON."""

    def test_plain_json(self):
        self.assertEqual(_parse_json_reply('{"a": 1}'), {"a": 1})

    def test_code_fences_are_stripped(self):
        self.assertEqual(_parse_json_reply('```json\n{"a": 1}\n```'), {"a": 1})

    def test_reasoning_block_is_stripped(self):
        # Groq's qwen narrates before answering.
        self.assertEqual(
            _parse_json_reply('<think>let me consider this</think>\n{"a": 1}'), {"a": 1}
        )

    def test_trailing_commentary_after_the_object_is_tolerated(self):
        self.assertEqual(_parse_json_reply('{"a": 1}\nHope that helps!'), {"a": 1})

    def test_a_reply_with_no_json_raises(self):
        from analyzer import ModelError
        with self.assertRaises(ModelError):
            _parse_json_reply("I cannot help with that.")


class RouteTextMessageURLs(unittest.TestCase):
    """A pasted link must be scanned as a website."""

    def test_plain_url_is_routed_to_website(self):
        self.assertEqual(
            route_text_message("https://example.com"),
            {"action": "website", "url": "https://example.com"},
        )

    def test_url_containing_history_is_still_scanned(self):
        # REGRESSION: the history keyword check used to run before the URL check,
        # so this returned the parent's own scan history and the page was never
        # fetched or moderated at all — with nothing to signal it was skipped.
        result = route_text_message("https://en.wikipedia.org/wiki/History_of_India")
        self.assertEqual(result["action"], "website")
        self.assertEqual(result["url"], "https://en.wikipedia.org/wiki/History_of_India")

    def test_url_containing_blocked_is_still_scanned(self):
        # REGRESSION: same cause — "blocked" appearing anywhere hijacked the message.
        result = route_text_message("https://example.com/blog/how-i-got-unblocked")
        self.assertEqual(result["action"], "website")

    def test_url_with_surrounding_words_is_extracted(self):
        result = route_text_message("hey is this safe https://example.com/page for a 9 year old")
        self.assertEqual(result["action"], "website")
        self.assertEqual(result["url"], "https://example.com/page")

    def test_trailing_sentence_punctuation_is_not_part_of_the_url(self):
        # REGRESSION: the URL used to keep the trailing period, producing a host
        # that could not be fetched and surfacing as a 500.
        self.assertEqual(
            route_text_message("look at https://example.com.")["url"],
            "https://example.com",
        )
        self.assertEqual(
            route_text_message("is https://example.com/path? ok")["url"],
            "https://example.com/path",
        )

    def test_first_url_wins_when_several_are_present(self):
        result = route_text_message("https://one.example.com and https://two.example.com")
        self.assertEqual(result["url"], "https://one.example.com")


class RouteTextMessageHistory(unittest.TestCase):
    """Messages with no link still reach the history lookup."""

    def test_blocked_keyword_filters_to_block(self):
        self.assertEqual(
            route_text_message("what got blocked yesterday?"),
            {"action": "history", "filter": "block"},
        )

    def test_flagged_keyword_filters_to_flag(self):
        self.assertEqual(
            route_text_message("show me my flagged scans"),
            {"action": "history", "filter": "flag"},
        )

    def test_bare_history_request_has_no_filter(self):
        self.assertEqual(
            route_text_message("show me my history"),
            {"action": "history", "filter": None},
        )

    def test_history_matching_is_case_insensitive(self):
        self.assertEqual(route_text_message("HISTORY")["action"], "history")


class RouteTextMessageText(unittest.TestCase):
    """Anything else is content to be moderated."""

    def test_ordinary_message_is_treated_as_text(self):
        self.assertEqual(
            route_text_message("tu bahut acha hai yaar"),
            {"action": "text", "content": "tu bahut acha hai yaar"},
        )

    def test_original_casing_and_spacing_are_preserved_for_the_model(self):
        # The routing comparison lowercases, but the model must see the real text.
        message = "  Yeh Bilkul Sahi Hai  "
        self.assertEqual(route_text_message(message)["content"], message)

    def test_empty_message_is_text(self):
        self.assertEqual(route_text_message("")["action"], "text")


class AssertFetchable(unittest.TestCase):
    """The SSRF guard on /analyze-website."""

    def assert_rejected(self, url):
        with self.assertRaises(UnsafeURLError, msg=f"{url} should have been rejected"):
            _assert_fetchable(url)

    def test_loopback_is_rejected(self):
        # The backend would otherwise summarise its own /history for the caller.
        self.assert_rejected("http://127.0.0.1:8000/history?limit=50")
        self.assert_rejected("http://localhost:8000/history")

    def test_private_lan_address_is_rejected(self):
        self.assert_rejected("http://192.168.1.1/")
        self.assert_rejected("http://10.0.0.5/")

    def test_cloud_metadata_endpoint_is_rejected(self):
        self.assert_rejected("http://169.254.169.254/latest/meta-data/")

    def test_unspecified_address_is_rejected(self):
        self.assert_rejected("http://0.0.0.0/")

    def test_non_http_schemes_are_rejected(self):
        self.assert_rejected("file:///etc/passwd")
        self.assert_rejected("ftp://example.com/x")

    def test_url_without_a_host_is_rejected(self):
        self.assert_rejected("http://")

    def test_ordinary_public_url_is_allowed(self):
        # Guard must not break the feature it protects.
        _assert_fetchable("https://example.com")


class ExtractText(unittest.TestCase):
    """HTML -> text, the part that runs off the event loop."""

    def test_script_and_style_content_is_dropped(self):
        html = """
        <html><head><style>body { color: red; }</style></head>
        <body><p>Real content</p><script>alert('x')</script></body></html>
        """
        text = _extract_text(html)
        self.assertIn("Real content", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("color: red", text)

    def test_output_is_capped_so_a_huge_page_cannot_be_sent_to_gemini(self):
        html = "<p>" + ("word " * 50_000) + "</p>"
        self.assertLessEqual(len(_extract_text(html)), 8000)

    def test_malformed_html_does_not_raise(self):
        # Input is truncated mid-document before reaching here, so the parser is
        # routinely handed HTML that is cut off partway through a tag.
        self.assertIsInstance(_extract_text("<div><p>half a documen"), str)

    def test_empty_html_yields_empty_string(self):
        self.assertEqual(_extract_text(""), "")


if __name__ == "__main__":
    unittest.main()
