"""`plexus dashboards` against a fake app that keeps the server's rules:
canonical text, ETag = content version, If-Match → 412, dry runs don't write.
"""

import json

import pytest

from plexus import dashboards_cli
from plexus.cli import main


def canonical(obj):
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"


class FakeApp:
    def __init__(self):
        self.files = {}  # uid -> canonical text
        self.version = {}  # uid -> int
        self.writes = 0

    def etag(self, uid):
        return f'"v{self.version[uid]}"'

    def add(self, uid, name="Rover"):
        self.files[uid] = canonical({"uid": uid, "name": name, "panels": []})
        self.version[uid] = 1

    def __call__(self, method, path, body=None, headers=None):
        headers = headers or {}
        if path == "/api/dashboards":
            rows = [{"uid": u, "name": json.loads(t)["name"], "panel_count": 0} for u, t in self.files.items()]
            return 200, {}, json.dumps({"dashboards": rows}).encode()
        uid = path.split("/")[3]
        if method == "GET":
            if uid not in self.files:
                return 404, {}, b'{"message":"Dashboard not found"}'
            return 200, {"etag": self.etag(uid)}, self.files[uid].encode()
        # PUT
        dry = "dryRun=true" in path
        text = canonical(json.loads(body.decode()))
        exists = uid in self.files
        if exists and "If-Match" in headers and headers["If-Match"] != self.etag(uid):
            return 412, {}, json.dumps({"etag": self.etag(uid)}).encode()
        unchanged = exists and self.files[uid] == text
        if not dry and not unchanged:
            self.files[uid] = text
            self.version[uid] = self.version.get(uid, 0) + 1
            self.writes += 1
        etag = f'"v{self.version.get(uid, 1)}"'
        return (201 if not exists and not dry else 200), {}, json.dumps(
            {"uid": uid, "unchanged": unchanged, "created": not exists, "etag": etag, "file": text, "warnings": []}
        ).encode()


@pytest.fixture
def app(monkeypatch, tmp_path):
    fake = FakeApp()
    monkeypatch.setattr(dashboards_cli, "_request", fake)
    monkeypatch.chdir(tmp_path)
    return fake


def test_pull_writes_the_server_bytes_and_a_lock(app, tmp_path):
    app.add("rover-01")
    assert main(["dashboards", "pull", "rover-01"]) == 0
    folder = tmp_path / "plexus" / "dashboards"
    assert (folder / "rover-01.json").read_text() == app.files["rover-01"]
    assert json.loads((folder / ".plexus-lock.json").read_text()) == {"rover-01": '"v1"'}
    assert (folder / ".gitignore").read_text() == ".plexus-lock.json\n"


def test_push_unchanged_is_a_no_op(app, capsys):
    app.add("rover-01")
    main(["dashboards", "pull", "rover-01"])
    assert main(["dashboards", "push"]) == 0
    assert main(["dashboards", "push"]) == 0
    assert app.writes == 0
    assert "unchanged" in capsys.readouterr().out


def test_push_sends_the_edit_and_rewrites_the_file(app, tmp_path):
    app.add("rover-01")
    main(["dashboards", "pull", "rover-01"])
    f = tmp_path / "plexus" / "dashboards" / "rover-01.json"
    data = json.loads(f.read_text())
    data["name"] = "Rover one"
    f.write_text(json.dumps(data))  # not canonical on purpose
    assert main(["dashboards", "push"]) == 0
    assert app.writes == 1
    assert f.read_text() == app.files["rover-01"]


def test_push_refuses_to_overwrite_an_edit_made_in_the_app(app, tmp_path, capsys):
    app.add("rover-01")
    main(["dashboards", "pull", "rover-01"])
    app.files["rover-01"] = canonical({"uid": "rover-01", "name": "Edited in the app", "panels": []})
    app.version["rover-01"] = 2
    f = tmp_path / "plexus" / "dashboards" / "rover-01.json"
    data = json.loads(f.read_text())
    data["name"] = "Mine"
    f.write_text(json.dumps(data))
    assert main(["dashboards", "push"]) == 2
    assert "changed in Plexus" in capsys.readouterr().err
    assert json.loads(app.files["rover-01"])["name"] == "Edited in the app"
    assert main(["dashboards", "push", "--force"]) == 0
    assert json.loads(app.files["rover-01"])["name"] == "Mine"


def test_diff_exit_codes(app, tmp_path):
    app.add("rover-01")
    main(["dashboards", "pull", "rover-01"])
    assert main(["dashboards", "diff"]) == 0
    f = tmp_path / "plexus" / "dashboards" / "rover-01.json"
    data = json.loads(f.read_text())
    data["name"] = "Changed"
    f.write_text(json.dumps(data))
    assert main(["dashboards", "diff"]) == 1
    assert app.writes == 0


def test_push_creates_a_new_dashboard(app, tmp_path, capsys):
    folder = tmp_path / "plexus" / "dashboards"
    folder.mkdir(parents=True)
    (folder / "new-one.json").write_text(json.dumps({"uid": "new-one", "name": "New", "panels": []}))
    assert main(["dashboards", "push"]) == 0
    assert "created" in capsys.readouterr().out
    assert "new-one" in app.files


def test_missing_scope_says_what_to_run(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        dashboards_cli,
        "_request",
        lambda *a, **k: (403, {}, b'{"error":"API key does not have required scope: dashboards"}'),
    )
    assert main(["dashboards", "list"]) == 2
    assert "plexus init --force" in capsys.readouterr().err


def test_no_key_is_a_clear_error(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PLEXUS_API_KEY", raising=False)
    monkeypatch.setattr(dashboards_cli.config, "get_api_key", lambda: None)
    assert main(["dashboards", "list"]) == 2
    assert "plexus init" in capsys.readouterr().err
