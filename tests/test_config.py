import pytest

from app.config import ConfigError, parse_chats


def test_valid():
    chats = parse_chats({"chats": [{"peer_id": 2000000001, "title": " A "}]})
    assert chats[0].peer_id == 2000000001 and chats[0].title == "A"


@pytest.mark.parametrize("data", [
    None, {}, {"chats": []}, {"chats": "x"},
    {"chats": [{"peer_id": 2000000000, "title": "A"}]},
    {"chats": [{"peer_id": "2000000001", "title": "A"}]},
    {"chats": [{"peer_id": True, "title": "A"}]},
    {"chats": [{"peer_id": 2000000001, "title": ""}]},
    {"chats": [{"peer_id": 2000000001}]},
    {"chats": [{"peer_id": 2000000001, "title": "A"}, {"peer_id": 2000000001, "title": "B"}]},
])
def test_invalid(data):
    with pytest.raises(ConfigError):
        parse_chats(data)
