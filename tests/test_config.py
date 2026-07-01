import pytest

import config


@pytest.mark.parametrize("raw,expected", [
    ("@RemoteGeekJob", "remotegeekjob"),
    ("https://t.me/remotegeekjob", "remotegeekjob"),
    ("https://t.me/s/remotegeekjob", "remotegeekjob"),
    ("t.me/remotegeekjob?foo=bar", "remotegeekjob"),
    ("  RemoteGeekJob/  ", "remotegeekjob"),
    ("https://t.me/remotegeekjob/123", "remotegeekjob"),
    ("remotegeekjob", "remotegeekjob"),
])
def test_normalize_channel(raw, expected):
    assert config.normalize_channel(raw) == expected


def test_normalize_channel_blank():
    assert config.normalize_channel("   ") == ""
