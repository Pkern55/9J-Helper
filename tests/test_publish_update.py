"""Tests for tools/publish_update.py with fake Drive/sheet objects (nothing touches Google)."""
import hashlib
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "publish_update", os.path.join(os.path.dirname(HERE), "tools", "publish_update.py"))
pu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pu)


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Records calls. `reachable` = file ids that PATCH accepts; others answer 404."""

    def __init__(self, md5, reachable=()):
        self.md5, self.reachable, self.calls = md5, set(reachable), []

    def patch(self, url, params=None, data=None, headers=None):
        fid = url.rsplit("/", 1)[1]
        self.calls.append(("PATCH", fid, data.read() if data else None))
        if fid not in self.reachable:
            return Resp(404)
        return Resp(200, {"id": fid, "md5Checksum": self.md5})

    def post(self, url, params=None, json=None):
        self.calls.append(("POST", url.split("/v3/")[1], json))
        if url.endswith("/files"):
            self.reachable.add("NEWID")
            return Resp(200, {"id": "NEWID"})
        return Resp(200, {})


class FakeSheet:
    title = "Sheet1"

    def __init__(self, rows):
        self.rows = rows

    def get_values(self, rng):
        assert rng == "A1:B20"
        return self.rows


class FakeBook:
    def __init__(self, rows):
        self.sheet1 = FakeSheet(rows)
        self.writes = []

    def values_batch_update(self, body):
        self.writes.append(body)


@pytest.fixture
def exe(tmp_path):
    p = tmp_path / "9J-Helper.exe"
    p.write_bytes(b"pretend this is an exe")
    return str(p), hashlib.md5(b"pretend this is an exe").hexdigest()


ROWS = [["version", "0.4.1"], ["download", "https://drive.google.com/file/d/OLDID/view?usp=sharing"],
        ["Notes", ""]]


def test_helpers():
    assert pu.file_id_from_link("https://drive.google.com/file/d/1jxYgr-_Y/view?usp=drive_link") == "1jxYgr-_Y"
    assert pu.file_id_from_link("") is None and pu.file_id_from_link("https://example.com") is None
    assert pu.view_link("abc") == "https://drive.google.com/file/d/abc/view?usp=sharing"
    assert pu.find_rows(ROWS) == {"version": 1, "download": 2, "notes": 3}
    assert pu.local_version().count(".") >= 1


def test_updates_the_existing_drive_file_then_the_sheet(exe):
    path, md5 = exe
    sess, book = FakeSession(md5, reachable={"OLDID"}), FakeBook([list(r) for r in ROWS])
    out = pu.publish(pu.Drive(sess), book, "0.4.2", path, notes="Adds X", log=lambda *_: None)
    assert out == {"file_id": "OLDID", "link": pu.view_link("OLDID"), "created": False,
                   "previous_version": "0.4.1"}
    assert sess.calls == [("PATCH", "OLDID", b"pretend this is an exe")]  # no new file created
    assert book.writes == [{"valueInputOption": "RAW", "data": [
        {"range": "'Sheet1'!B1", "values": [["0.4.2"]]},
        {"range": "'Sheet1'!B2", "values": [[pu.view_link("OLDID")]]},
        {"range": "'Sheet1'!B3", "values": [["Adds X"]]}]}]


def test_creates_a_new_file_when_the_old_one_is_not_reachable_and_shares_it(exe):
    path, md5 = exe
    sess = FakeSession(md5, reachable=set())  # the hand-uploaded file isn't visible to drive.file
    book = FakeBook([list(r) for r in ROWS])
    out = pu.publish(pu.Drive(sess), book, "0.4.2", path, share=["dad@example.com"], log=lambda *_: None)
    assert out["created"] and out["file_id"] == "NEWID"
    kinds = [(c[0], c[1]) for c in sess.calls]
    assert kinds == [("PATCH", "OLDID"), ("POST", "files"), ("PATCH", "NEWID"),
                     ("POST", "files/NEWID/permissions")]
    assert sess.calls[-1][2] == {"role": "reader", "type": "user", "emailAddress": "dad@example.com"}
    written = {d["range"]: d["values"][0][0] for d in book.writes[0]["data"]}
    assert written == {"'Sheet1'!B1": "0.4.2", "'Sheet1'!B2": pu.view_link("NEWID")}  # notes untouched


def test_blank_download_cell_creates_the_file(exe):
    path, md5 = exe
    sess = FakeSession(md5)
    book = FakeBook([["version", ""], ["download", ""]])
    out = pu.publish(pu.Drive(sess), book, "0.4.1", path, notes="n", log=lambda *_: None)
    assert out["created"] and [c[0:2] for c in sess.calls][:1] == [("POST", "files")]
    assert len(book.writes[0]["data"]) == 2  # no 'notes' row, so notes aren't written


def test_corrupt_upload_leaves_the_sheet_alone(exe):
    path, _ = exe
    sess, book = FakeSession("deadbeef", reachable={"OLDID"}), FakeBook([list(r) for r in ROWS])
    with pytest.raises(pu.PublishError, match="doesn't match"):
        pu.publish(pu.Drive(sess), book, "0.4.2", path, log=lambda *_: None)
    assert book.writes == []


def test_refuses_to_publish_an_older_version_unless_forced(exe):
    path, md5 = exe
    rows = [["version", "0.4.2"], ["download", "https://drive.google.com/file/d/OLDID/view"]]
    sess, book = FakeSession(md5, reachable={"OLDID"}), FakeBook([list(r) for r in rows])
    with pytest.raises(pu.PublishError, match="already says version 0.4.2"):
        pu.publish(pu.Drive(sess), book, "0.4.1", path, log=lambda *_: None)
    assert sess.calls == [] and book.writes == []  # refused before touching Drive or the sheet
    pu.publish(pu.Drive(sess), book, "0.4.1", path, force=True, log=lambda *_: None)
    assert book.writes[0]["data"][0]["values"] == [["0.4.1"]]
    # the same version again is fine (e.g. re-uploading)
    pu.publish(pu.Drive(sess), FakeBook([["version", "0.4.1"], ["download", "https://x/file/d/OLDID/v"]]),
               "0.4.1", path, log=lambda *_: None)


def test_sheet_without_the_expected_rows_is_rejected(exe):
    path, md5 = exe
    with pytest.raises(pu.PublishError, match="needs 'version' and 'download'"):
        pu.publish(pu.Drive(FakeSession(md5)), FakeBook([["hello", "x"]]), "1.0", path)
