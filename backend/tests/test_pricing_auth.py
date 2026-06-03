from app.auth import create_token, hash_password, verify_password, verify_token
from app.pricing import estimate_credits
from app.tool_config import get_tool


def test_mask_video_pricing_uses_duration_and_minimum() -> None:
    tool = get_tool("remove-watermark")
    assert estimate_credits(tool, {"duration": 30, "resolution": "1080p", "priority": "standard"}) == 15
    assert estimate_credits(tool, {"duration": 1, "resolution": "720p", "priority": "standard"}) == 10


def test_enhance_video_pricing_uses_resolution() -> None:
    tool = get_tool("enhance")
    assert estimate_credits(tool, {"duration": 30, "resolution": "1080p", "priority": "standard"}) == 30
    assert estimate_credits(tool, {"duration": 30, "resolution": "4K", "priority": "standard"}) == 68


def test_image_pricing_uses_count() -> None:
    tool = get_tool("image-cleanup")
    assert estimate_credits(tool, {"imageCount": 3, "priority": "standard"}) == 9


def test_password_hash_and_token_round_trip() -> None:
    password_hash = hash_password("secret-password")
    assert verify_password("secret-password", password_hash)
    assert not verify_password("wrong-password", password_hash)
    token = create_token("user-123")
    assert verify_token(token) == "user-123"
