"""Multi-user mode: account header, private directories, per-user file serving."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import main


@pytest.fixture
def multi_user(monkeypatch):
    monkeypatch.setattr(main, "MULTI_USER", True)
    monkeypatch.setattr(main, "_AUTH_HEADER", "Remote-User")
    monkeypatch.setattr(main, "_AUTH_ENVIRON_KEY", "HTTP_REMOTE_USER")
    monkeypatch.setattr(main, "ADMIN_USER", "admin")
    monkeypatch.setattr(main, "_user_queues", {})


@pytest.mark.parametrize("raw,expected", [
    ("nnitiema", "nnitiema"),
    ("  NSNitiema ", "nsnitiema"),
    ("jean.dupont", "jean.dupont"),
    ("", None),
    (None, None),
    ("../etc", None),
    ("a..b", None),
    (".metube", None),
    ("a/b", None),
    ("a b", None),
    ("x" * 65, None),
])
def test_normalize_user(raw, expected):
    assert main._normalize_user(raw) == expected


def test_user_config_gives_private_directories_and_shared_settings():
    cfg = main.UserConfig(main.config, "alice")
    assert cfg.DOWNLOAD_DIR == os.path.join(main.config.DOWNLOAD_DIR, "alice")
    assert cfg.TEMP_DIR == os.path.join(main.config.TEMP_DIR, "alice")
    assert cfg.STATE_DIR == os.path.join(main.config.STATE_DIR, "users", "alice")
    assert os.path.isdir(cfg.DOWNLOAD_DIR) and os.path.isdir(cfg.STATE_DIR)
    assert cfg.MAX_CONCURRENT_DOWNLOADS == main.config.MAX_CONCURRENT_DOWNLOADS
    assert cfg.YTDL_OPTIONS is main.config.YTDL_OPTIONS


async def _ok(request):
    return web.Response(text=request.get("metube_user", ""))


@pytest.mark.asyncio
async def test_guard_is_inactive_in_single_user_mode():
    request = make_mocked_request("GET", "/")
    response = await main.auth_user_guard(request, _ok)
    assert response.text == ""


@pytest.mark.asyncio
async def test_guard_refuses_requests_without_the_account_header(multi_user):
    with pytest.raises(web.HTTPUnauthorized):
        await main.auth_user_guard(make_mocked_request("GET", "/"), _ok)
    with pytest.raises(web.HTTPUnauthorized):
        await main.auth_user_guard(
            make_mocked_request("GET", "/", headers={"Remote-User": "../x"}), _ok)


@pytest.mark.asyncio
async def test_guard_records_the_normalized_account(multi_user):
    request = make_mocked_request("GET", "/", headers={"Remote-User": "NNitiema"})
    response = await main.auth_user_guard(request, _ok)
    assert response.text == "nnitiema"


def test_admin_features_are_reserved_to_the_admin_account(multi_user):
    user = make_mocked_request("POST", "/subscribe")
    user["metube_user"] = "alice"
    with pytest.raises(web.HTTPForbidden):
        main._require_admin(user)
    admin = make_mocked_request("POST", "/subscribe")
    admin["metube_user"] = "admin"
    main._require_admin(admin)


def test_admin_features_are_closed_without_an_admin_account(multi_user, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_USER", "")
    request = make_mocked_request("POST", "/subscribe")
    request["metube_user"] = "alice"
    with pytest.raises(web.HTTPForbidden):
        main._require_admin(request)


def _file_request(user, path):
    request = make_mocked_request("GET", "/download/x", match_info={"path": path})
    request["metube_user"] = user
    return request


@pytest.mark.asyncio
async def test_a_user_gets_only_files_from_their_own_directory():
    base = main.config.DOWNLOAD_DIR
    for user in ("alice", "bob"):
        os.makedirs(os.path.join(base, user), exist_ok=True)
    with open(os.path.join(base, "alice", "vidéo 1.mp4"), "wb") as f:
        f.write(b"data")
    handler = main._user_file_handler("DOWNLOAD_DIR")

    response = await handler(_file_request("alice", "vidéo 1.mp4"))
    assert isinstance(response, web.FileResponse)
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert "vid%C3%A9o%201.mp4" in disposition

    # Same URL, other account: the file does not exist for them.
    with pytest.raises(web.HTTPNotFound):
        await handler(_file_request("bob", "vidéo 1.mp4"))
    # Path tricks do not leave the caller's directory.
    for path in ("../alice/vidéo 1.mp4", "..", "", "/etc/passwd"):
        with pytest.raises(web.HTTPNotFound):
            await handler(_file_request("bob", path))


@pytest.mark.asyncio
async def test_each_user_gets_their_own_queue(multi_user, monkeypatch):
    created = []

    def fake_queue(cfg, notifier):
        q = MagicMock()
        q.initialize = AsyncMock()
        q.cfg, q.notifier = cfg, notifier
        created.append(q)
        return q

    monkeypatch.setattr(main, "DownloadQueue", fake_queue)
    alice = await main._get_user_queue("alice")
    bob = await main._get_user_queue("bob")
    assert alice is not bob
    assert await main._get_user_queue("alice") is alice
    assert len(created) == 2
    assert alice.cfg.DOWNLOAD_DIR != bob.cfg.DOWNLOAD_DIR
    assert alice.notifier.room == "user:alice" and bob.notifier.room == "user:bob"
    alice.initialize.assert_awaited_once()
    # The administrator uses the default queue, which subscriptions feed.
    assert await main._get_user_queue("admin") is main.dqueue


@pytest.mark.asyncio
async def test_notifier_emits_only_to_its_room(monkeypatch):
    emit = AsyncMock()
    monkeypatch.setattr(main.sio, "emit", emit)
    await main.Notifier("user:alice").canceled("some-id")
    assert emit.await_args.kwargs["to"] == "user:alice"


@pytest.mark.asyncio
async def test_socket_connection_without_account_is_refused(multi_user):
    import socketio
    with pytest.raises(socketio.exceptions.ConnectionRefusedError):
        await main.connect("sid-x", {})
