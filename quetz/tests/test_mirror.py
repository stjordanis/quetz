import os
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.background import BackgroundTasks

from quetz import rest_models
from quetz.authorization import Rules
from quetz.db_models import Channel, Package
from quetz.mirror import KNOWN_SUBDIRS, RemoteRepository, initial_sync_mirror


@pytest.fixture
def proxy_channel(db):

    channel = Channel(name="test_proxy_channel", mirror_channel_url="http://host")
    db.add(channel)
    db.commit()

    yield channel

    db.delete(channel)
    db.commit()


@pytest.fixture
def mirror_channel(dao, user, db):

    channel_data = rest_models.Channel(
        name="test_mirror_channel",
        private=False,
        mirror_channel_url="http://host",
        mirror_mode="mirror",
    )

    channel = dao.create_channel(channel_data, user.id, "owner")

    yield channel

    db.delete(channel)
    db.commit()


@pytest.fixture
def local_channel(db):

    channel = Channel(name="test_local_channel")
    db.add(channel)
    db.commit()

    yield channel

    db.delete(channel)
    db.commit()


def test_set_mirror_url(db, client, user):
    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.post(
        "/api/channels",
        json={
            "name": "test_create_channel",
            "private": False,
            "mirror_channel_url": "http://my_remote_host",
        },
    )
    assert response.status_code == 201

    channel = db.query(Channel).get("test_create_channel")
    assert channel.mirror_channel_url == "http://my_remote_host"


def test_get_mirror_url(proxy_channel, local_channel, client):
    """test configuring mirror url"""

    response = client.get("/api/channels/{}".format(proxy_channel.name))

    assert response.status_code == 200
    assert response.json()["mirror_channel_url"] == "http://host"

    response = client.get("/api/channels/{}".format(local_channel.name))
    assert response.status_code == 200
    assert not response.json()["mirror_channel_url"]


DUMMY_PACKAGE = Path("./test-package-0.1-0.tar.bz2")


@pytest.mark.parametrize(
    "repo_content,timestamp_mirror_sync,expected_timestamp,new_package",
    [
        (
            [b'{"packages": {"my-package": {"time_modified": 100}}}', DUMMY_PACKAGE],
            0,
            100,
            True,
        ),
        ([b'{"packages": {"my-package": {}}}', DUMMY_PACKAGE], 0, 0, True),
        ([b'{"packages": {"my-package": {}}}', DUMMY_PACKAGE], 100, 100, True),
        (
            [b'{"packages": {"my-package": {"time_modified": 1000}}}', DUMMY_PACKAGE],
            100,
            1000,
            True,
        ),
        (
            [b'{"packages": {"my-package": {"time_modified": 100}}}', DUMMY_PACKAGE],
            1000,
            1000,
            False,
        ),
    ],
)
def test_synchronisation_timestamp(
    mirror_channel,
    dao,
    config,
    dummy_response,
    db,
    user,
    expected_timestamp,
    timestamp_mirror_sync,
    new_package,
):

    mirror_channel.timestamp_mirror_sync = timestamp_mirror_sync
    pkgstore = config.get_package_store()
    background_tasks = BackgroundTasks()
    rules = Rules("", {"user_id": str(uuid.UUID(bytes=user.id))}, db)

    class DummySession:
        def get(self, path, stream=False):
            return dummy_response()

    dummy_repo = RemoteRepository("", DummySession())

    initial_sync_mirror(
        mirror_channel.name,
        dummy_repo,
        "linux-64",
        dao,
        pkgstore,
        rules,
        background_tasks,
        skip_errors=False,
    )

    channel = db.query(Channel).get(mirror_channel.name)
    assert channel.timestamp_mirror_sync == expected_timestamp

    if new_package:
        assert channel.packages[0].name == 'test-package'
        db.delete(channel.packages[0])
        db.commit()
    else:
        assert not channel.packages


@pytest.fixture
def repo_content():
    return b"Hello world!"


@pytest.fixture
def status_code():
    return 200


@pytest.fixture
def dummy_response(repo_content, status_code):
    class DummyResponse:
        def __init__(self):
            if isinstance(repo_content, list):
                content = repo_content.pop(0)
            else:
                content = repo_content
            if isinstance(content, Path):
                with open(content.absolute(), 'rb') as fid:
                    content = fid.read()
            self.raw = BytesIO(content)
            self.headers = {"content-type": "application/json"}
            if isinstance(status_code, list):
                self.status_code = status_code.pop(0)
            else:
                self.status_code = status_code

    return DummyResponse


@pytest.fixture
def dummy_repo(app, dummy_response):

    from quetz.main import get_remote_session

    files = []

    class DummySession:
        def get(self, path, stream=False):
            files.append(path)
            return dummy_response()

    app.dependency_overrides[get_remote_session] = DummySession

    yield files

    app.dependency_overrides.pop(get_remote_session)


def test_download_remote_file(client, user, dummy_repo):
    """Test downloading from cache."""
    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.post(
        "/api/channels",
        json={
            "name": "proxy_channel",
            "private": False,
            "mirror_channel_url": "http://host",
        },
    )
    assert response.status_code == 201

    # download from remote server
    response = client.get("/channels/proxy_channel/test_file.txt")

    assert response.status_code == 200
    assert response.content == b"Hello world!"
    assert dummy_repo == [("http://host/test_file.txt")]

    dummy_repo.pop()

    assert dummy_repo == []

    # serve from cache
    response = client.get("/channels/proxy_channel/test_file.txt")

    assert response.status_code == 200
    assert response.content == b"Hello world!"

    assert dummy_repo == []

    # new file - download from remote
    response = client.get("/channels/proxy_channel/test_file_2.txt")

    assert response.status_code == 200
    assert response.content == b"Hello world!"
    assert dummy_repo == [("http://host/test_file_2.txt")]


def test_always_download_repodata(client, user, dummy_repo):
    """Test downloading from cache."""
    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.post(
        "/api/channels",
        json={
            "name": "proxy_channel_2",
            "private": False,
            "mirror_channel_url": "http://host",
        },
    )
    assert response.status_code == 201

    response = client.get("/channels/proxy_channel_2/repodata.json")
    assert response.status_code == 200
    assert response.content == b"Hello world!"

    response = client.get("/channels/proxy_channel_2/repodata.json")
    assert response.status_code == 200
    assert response.content == b"Hello world!"

    assert dummy_repo == [
        ("http://host/repodata.json"),
        ("http://host/repodata.json"),
    ]


def test_method_not_implemented_for_proxies(client, proxy_channel):

    response = client.post("/api/channels/{}/packages".format(proxy_channel.name))
    assert response.status_code == 405
    assert "not implemented" in response.json()["detail"]


def test_api_methods_for_proxy_channels(client, mirror_channel):
    """mirror-mode channels should have all standard API calls"""

    response = client.get("/api/channels/{}/packages".format(mirror_channel.name))
    assert response.status_code == 200
    assert not response.json()


@pytest.mark.parametrize(
    "repo_content,expected_paths",
    [
        # linux-64 subdir without packages
        (
            [b'{"subdirs": ["linux-64"]}', b'{"packages": {}}'],
            ["channeldata.json", "linux-64/repodata.json"],
        ),
        # empty repodata
        (
            [b'{"subdirs": ["linux-64"]}', b"{}"],
            ["channeldata.json", "linux-64/repodata.json"],
        ),
        # no subodirs
        ([b'{"subdirs": []}'], ["channeldata.json"]),
        # two arbitrary subdirs
        (
            [b'{"subdirs": ["some-arch-1", "some-arch-2"]}', b"{}", b"{}"],
            [
                "channeldata.json",
                "some-arch-1/repodata.json",
                "some-arch-2/repodata.json",
            ],
        ),
    ],
)
def test_mirror_initial_sync(client, dummy_repo, user, expected_paths):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    host = "http://mirror3_host"
    response = client.post(
        "/api/channels",
        json={
            "name": "mirror_channel_" + str(uuid.uuid4())[:10],
            "private": False,
            "mirror_channel_url": host,
            "mirror_mode": "mirror",
        },
    )
    assert response.status_code == 201

    assert dummy_repo == [os.path.join(host, p) for p in expected_paths]


empty_archive = b""


@pytest.mark.parametrize(
    "repo_content",
    [
        [
            b'{"subdirs": ["linux-64"]}',
            b'{"packages": {"my_package-0.1.tar.bz": {"subdir":"linux-64"}}}',
            empty_archive,
        ]
    ],
)
def test_wrong_package_format(client, dummy_repo, user):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    channel_name = "mirror_channel_" + str(uuid.uuid4())[:10]

    response = client.post(
        "/api/channels",
        json={
            "name": channel_name,
            "private": False,
            "mirror_channel_url": "http://mirror3_host",
            "mirror_mode": "mirror",
        },
    )

    assert response.status_code == 201

    assert dummy_repo == [
        "http://mirror3_host/channeldata.json",
        "http://mirror3_host/linux-64/repodata.json",
        "http://mirror3_host/linux-64/my_package-0.1.tar.bz",
    ]

    # check if package was not added
    response = client.get(f"/api/channels/{channel_name}/packages")

    assert response.status_code == 200

    assert not response.json()


def test_mirror_unavailable_url(client, user, db):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    channel_name = "mirror_channel_" + str(uuid.uuid4())[:10]
    host = "http://fantasy_host"

    response = client.post(
        "/api/channels",
        json={
            "name": channel_name,
            "private": False,
            "mirror_channel_url": host,
            "mirror_mode": "mirror",
        },
    )

    assert response.status_code == 503
    assert "unavailable" in response.json()['detail']
    assert host in response.json()['detail']

    channel = db.query(Channel).filter_by(name=channel_name).first()

    assert channel is None


def test_validate_mirror_url(client, user):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    channel_name = "mirror_channel_" + str(uuid.uuid4())[:10]
    host = "no-schema-host"

    response = client.post(
        "/api/channels",
        json={
            "name": channel_name,
            "private": False,
            "mirror_channel_url": host,
            "mirror_mode": "mirror",
        },
    )

    assert response.status_code == 422
    assert "schema (http/https) missing" in response.json()['detail'][0]['msg']


@pytest.fixture
def mirror_package(mirror_channel, db):
    pkg = Package(
        name="mirror_package", channel_name=mirror_channel.name, channel=mirror_channel
    )
    db.add(pkg)
    db.commit()

    yield pkg

    db.delete(pkg)
    db.commit()


def test_write_methods_for_local_channels(client, local_channel, user, db):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.post(
        "/api/channels/{}/packages".format(local_channel.name),
        json={"name": "my_package"},
    )
    assert response.status_code == 201

    pkg = db.query(Package).filter_by(name="my_package").first()

    db.delete(pkg)
    db.commit()


def test_disabled_methods_for_mirror_channels(
    client, mirror_channel, mirror_package, user
):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.post("/api/channels/{}/packages".format(mirror_channel.name))
    assert response.status_code == 405
    assert "not implemented" in response.json()["detail"]

    files = {'files': ('my_package-0.1.tar.bz', 'dfdf')}
    response = client.post(
        "/api/channels/{}/files/".format(mirror_channel.name), files=files
    )
    assert response.status_code == 405
    assert "not implemented" in response.json()["detail"]

    response = client.post(
        "/api/channels/{}/packages/mirror_package/files/".format(mirror_channel.name),
        files=files,
    )
    assert response.status_code == 405
    assert "not implemented" in response.json()["detail"]


@pytest.mark.parametrize(
    "repo_content,status_code,expected_archs",
    [
        # no channeldata
        (b"", 404, KNOWN_SUBDIRS),
        # badly formatted channel data
        (b"<html></html>", 200, KNOWN_SUBDIRS),
        # no archs in channeldata
        (b"{}", 200, []),
        # custom architecture
        (b'{"subdirs":["wonder-arch"]}', 200, ["wonder-arch"]),
    ],
)
def test_repo_without_channeldata(user, client, dummy_repo, expected_archs):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.post(
        "/api/channels",
        json={
            "name": "mirror_channel_" + str(uuid.uuid4())[:10],
            "private": False,
            "mirror_channel_url": "http://mirror3_host",
            "mirror_mode": "mirror",
        },
    )

    assert dummy_repo[0] == "http://mirror3_host/channeldata.json"
    for arch in expected_archs:
        assert "http://mirror3_host/{}/repodata.json".format(arch) in dummy_repo
    assert len(dummy_repo) == len(expected_archs) + 1

    assert response.status_code == 201


def test_sync_mirror_channel(mirror_channel, user, client, dummy_repo):

    response = client.put(f"/api/channels/{mirror_channel.name}")

    assert response.status_code == 401

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.put(f"/api/channels/{mirror_channel.name}")
    assert response.status_code == 200


def test_can_not_sync_mirror_and_local_channels(
    proxy_channel, local_channel, user, client
):

    response = client.get("/api/dummylogin/bartosz")
    assert response.status_code == 200

    response = client.put(f"/api/channels/{proxy_channel.name}")
    assert response.status_code == 405

    response = client.put(f"/api/channels/{local_channel.name}")
    assert response.status_code == 405