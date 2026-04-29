"""Tests for puzzleeval.vision_judge (Gap 6 + Gap 26)."""
from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest

from puzzleeval.vision_judge import (
    VisionVerdict,
    _build_vision_prompt,
    _decode_data_url,
    extract_image_url,
    evaluate_generated_image,
)


def _tiny_png_bytes() -> bytes:
    """A 1x1 transparent PNG — enough bytes to satisfy the download path."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )


class TestExtractImageUrl:
    def test_direct_url(self):
        assert extract_image_url("https://example.com/foo.png") == "https://example.com/foo.png"

    def test_non_image_url_rejected(self):
        assert extract_image_url("https://example.com/foo.html") is None

    def test_url_in_dict_under_url_key(self):
        payload = {"url": "https://cdn.example/result.jpg"}
        assert extract_image_url(payload) == "https://cdn.example/result.jpg"

    def test_url_in_nested_dict(self):
        payload = {"data": {"outputs": [{"image_url": "https://x.com/a.webp"}]}}
        assert extract_image_url(payload) == "https://x.com/a.webp"

    def test_data_url_recognized(self):
        data_url = "data:image/png;base64,iVBORw0KGgo="
        assert extract_image_url(data_url) == data_url

    def test_none_returns_none(self):
        assert extract_image_url(None) is None
        assert extract_image_url({}) is None
        assert extract_image_url("not a url") is None


class TestDecodeDataUrl:
    def test_valid_data_url(self):
        b = _tiny_png_bytes()
        data_url = "data:image/png;base64," + base64.b64encode(b).decode("ascii")
        decoded = _decode_data_url(data_url)
        assert decoded is not None
        body, mime = decoded
        assert body == b
        assert mime == "image/png"

    def test_invalid_data_url_returns_none(self):
        assert _decode_data_url("data:text/plain,foo") is None
        assert _decode_data_url("https://example.com/x.png") is None


class TestBuildVisionPrompt:
    def test_includes_description_and_criteria(self):
        criteria = [
            {"criterion": "Matches prompt", "weight": 0.6, "eval_type": "subjective_quality"},
            {"criterion": "Readable text", "weight": 0.4, "eval_type": "subjective_quality"},
        ]
        prompt = _build_vision_prompt("A red apple on a table", criteria)
        assert "A red apple on a table" in prompt
        assert "Matches prompt" in prompt
        assert "Readable text" in prompt
        # JSON schema stub
        assert '"criterion_scores"' in prompt
        assert '"score"' in prompt
        assert '"passed"' in prompt

    def test_falls_back_when_no_criteria(self):
        prompt = _build_vision_prompt("A sunset", [])
        assert "Overall fidelity" in prompt


class TestEvaluateGeneratedImage:
    def test_rejects_unsupported_source(self):
        v = evaluate_generated_image("ftp://example.com/x.png", "anything")
        assert v.fallback_reason == "unsupported_source"
        assert not v.passed

    def test_invalid_data_url_falls_back(self):
        v = evaluate_generated_image("data:image/png;base64,@@bogus@@", "anything")
        assert v.fallback_reason in {"invalid_data_url", "unsupported_source"}

    def test_data_url_success_path_with_mock_client(self):
        b = _tiny_png_bytes()
        data_url = "data:image/png;base64," + base64.b64encode(b).decode("ascii")

        mock_text_block = MagicMock()
        mock_text_block.text = (
            '{"criterion_scores": {"Matches prompt": 0.9}, '
            '"score": 0.9, "passed": true, "reasoning": "Looks good."}'
        )
        mock_response = MagicMock()
        mock_response.content = [mock_text_block]

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        v = evaluate_generated_image(
            data_url,
            expected_description="A blue sky",
            criteria=[{"criterion": "Matches prompt", "weight": 1.0, "eval_type": "subjective_quality"}],
            client=mock_client,
            model="claude-sonnet-4-6",
        )
        assert v.fallback_reason is None
        assert v.passed is True
        assert v.score == pytest.approx(0.9)
        assert v.criterion_scores.get("Matches prompt") == pytest.approx(0.9)

    def test_json_parse_failure_falls_back(self):
        b = _tiny_png_bytes()
        data_url = "data:image/png;base64," + base64.b64encode(b).decode("ascii")

        mock_text_block = MagicMock()
        mock_text_block.text = "not json at all, just prose"
        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        v = evaluate_generated_image(data_url, "expected", client=mock_client)
        assert v.fallback_reason == "parse_failure"

    def test_strips_markdown_code_fences(self):
        b = _tiny_png_bytes()
        data_url = "data:image/png;base64," + base64.b64encode(b).decode("ascii")

        mock_text_block = MagicMock()
        mock_text_block.text = (
            '```json\n'
            '{"criterion_scores": {}, "score": 0.5, "passed": false, '
            '"reasoning": "mid"}\n```'
        )
        mock_response = MagicMock()
        mock_response.content = [mock_text_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        v = evaluate_generated_image(data_url, "expected", client=mock_client)
        assert v.fallback_reason is None
        assert v.score == pytest.approx(0.5)
        assert v.passed is False
