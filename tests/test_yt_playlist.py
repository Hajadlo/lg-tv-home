"""yt-playlist against a fake InnerTube serving tv-home/server/testdata/innertube (sanitized live shapes)."""
import copy
import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parent.parent / "server"
FIXTURES = Path(__file__).resolve().parent / "testdata" / "innertube"
sys.path.insert(0, str(SERVER))

import net  # noqa: E402
import yt_cookies  # noqa: E402
import yt_watched  # noqa: E402

_loader = importlib.machinery.SourceFileLoader("yt_playlist", str(SERVER / "yt-playlist"))
ytp = importlib.util.module_from_spec(importlib.util.spec_from_loader("yt_playlist", _loader))
_loader.exec_module(ytp)

PL = "PLFAKE0000000000000001"
CHANNEL_DS = "200000000000000000002||100000000000000000001"  # the accounts fixture's brand channel
LOGIN = [{"host": ".youtube.com", "name": n, "value": f"fake-{n}", "path": "/", "expiry": None}
         for n in ("LOGIN_INFO", "SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID", "SID")]


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeYouTube:
    """InnerTube stand-in: one playlist whose tracks and privacy change with edit_playlist."""

    def __init__(self):
        self.calls = []
        page = fixture("playlist-page.json")
        self.renderers = {}
        for item in ytp.the_list(page) + ytp.continuation_items(fixture("playlist-page-2.json")):
            if "playlistVideoRenderer" in item:
                self.renderers[item["playlistVideoRenderer"]["videoId"]] = item
        self.order = list(self.renderers)
        self.privacy = "Unlisted"

    def track(self, vid):
        if vid not in self.renderers:
            item = copy.deepcopy(self.renderers[self.order[0]])
            r = item["playlistVideoRenderer"]
            r.update(videoId=vid, setVideoId=f"SET{vid.upper()}", title={"runs": [{"text": f"Added {vid}"}]})
            self.renderers[vid] = item
        return self.renderers[vid]

    def page(self):
        page = fixture("playlist-page.json")
        items = ytp.the_list(page)
        token = items[-1]
        items[:] = [self.track(v) for v in self.order[:2]] + ([token] if len(self.order) > 2 else [])
        rows = page["header"]["pageHeaderRenderer"]["content"]["pageHeaderViewModel"]["metadata"][
            "contentMetadataViewModel"]["metadataRows"]
        rows[1]["metadataParts"][1]["text"]["content"] = self.privacy
        return page

    def __call__(self, url, method="GET", headers=None, data=None, timeout=15, follow=True):
        body = json.loads(data)
        endpoint = url.split("/youtubei/v1/")[1].split("?")[0]
        self.calls.append((endpoint, body.get("browseId") or body.get("continuation") or body.get("playlistId")))
        ctx = {"responseContext": {"mainAppWebResponseContext": {"datasyncId": CHANNEL_DS, "loggedOut": False}}}
        if endpoint == "account/accounts_list":
            if headers["X-Goog-AuthUser"] == "1":  # the channel lives at session index 1
                answer = fixture("accounts-list.json")
            elif headers["X-Goog-AuthUser"] == "0":
                answer = {"responseContext": {"mainAppWebResponseContext": {"datasyncId": "9||"}}, "actions": [
                    {"accountItemRenderer": {"accountName": {"simpleText": "Work"}, "serviceEndpoint": {
                        "selectActiveIdentityEndpoint": {"supportedTokens": [{"datasyncIdToken": {
                            "datasyncIdToken": "9||"}}]}}}}]}
            else:
                answer = {"responseContext": {"mainAppWebResponseContext": {"loggedOut": True}}}
        elif endpoint == "browse/edit_playlist":
            for action in body["actions"]:
                if action["action"] == "ACTION_ADD_VIDEO":
                    self.order.append(action["addedVideoId"])
                elif action["action"] == "ACTION_REMOVE_VIDEO":
                    assert self.track(action["removedVideoId"])["playlistVideoRenderer"]["setVideoId"] == \
                        action["setVideoId"]
                    self.order.remove(action["removedVideoId"])
                elif action["action"] == "ACTION_SET_PLAYLIST_PRIVACY":
                    self.privacy = action["playlistPrivacy"].title()
            answer = fixture("edit-playlist.json")
        elif body.get("browseId") == "FEplaylist_aggregation":
            answer = {**fixture("playlists.json"), **ctx}
        elif body.get("browseId") == "VL" + PL:
            answer = {**self.page(), **ctx}
        elif body.get("continuation") == "FAKE_LIST_PAGE_2":
            page2 = fixture("playlist-page-2.json")
            page2["onResponseReceivedActions"][0]["appendContinuationItemsAction"]["continuationItems"] = [
                self.track(v) for v in self.order[2:]]
            answer = {**page2, **ctx}
        elif body.get("continuation") == "FAKE_SUGGESTIONS":
            answer = {**fixture("playlist-suggestions.json"), **ctx}
        else:
            return net.Response(404, {}, b"{}")
        return net.Response(200, {}, json.dumps(answer).encode())

    def browsed(self):
        return [target for endpoint, target in self.calls if endpoint == "browse"]


@pytest.fixture
def yt(tmp_path):
    net.write_json(tmp_path / "yt-cookies.json", {"synced": "x", "cookies": LOGIN})
    web = FakeYouTube()
    store = yt_cookies.CookieStore(tmp_path / "yt-cookies.json", lambda: False)
    session = yt_watched.CookieYouTube(store, tmp_path / "yt-watched.json", channel_name="Couch Channel", fetch=web, notify=lambda t, m: False)
    return web, lambda *argv: ytp.main(list(argv), session=session)


def test_tracks_are_the_list_only_never_the_suggestions(yt, capsys):
    web, run = yt
    assert run("list", PL) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# Fake list: 3 tracks (YouTube counts 4: it hides unavailable videos), Unlisted"
    assert [line.split("\t")[0] for line in out[1:]] == ["aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3"]
    assert out[2] == "bbbbbbbbbb2\tArtist B - Topic\tTrack two"
    assert web.browsed() == ["VL" + PL, "FAKE_LIST_PAGE_2"]  # the suggestions' continuation is never fetched


def test_suggestion_shapes_parse_to_no_tracks():
    suggestions = ytp.continuation_items(fixture("playlist-suggestions.json"))
    assert ytp.tracks(suggestions) == [] and ytp.list_token(suggestions) == "FAKE_MORE_SUGGESTIONS"
    assert ytp.the_list(fixture("playlist-suggestions.json")) is None


def test_library_lists_playlists(yt, capsys):
    _, run = yt
    assert run("list") == 0
    assert capsys.readouterr().out.splitlines() == [
        f"{PL}\tUnlisted · Playlist · Updated yesterday · View full playlist\tFake list",
        "LL\tPrivate · Playlist · View full playlist\tLiked videos",
        "PLOTHER000000000000002\tSomeone · Playlist · View full playlist\tSomeone's list"]


def test_add_and_remove_read_the_playlist_back(yt, capsys):
    web, run = yt
    assert run("add", PL, "dddddddddd4") == 0
    assert "dddddddddd4\t" in capsys.readouterr().out
    assert run("remove", PL, "bbbbbbbbbb2", "dddddddddd4") == 0
    assert web.order == ["aaaaaaaaaa1", "cccccccccc3"]
    edits = [target for endpoint, target in web.calls if endpoint == "browse/edit_playlist"]
    assert edits == [PL, PL]


def test_remove_refuses_a_video_that_is_not_a_track(yt, capsys):
    web, run = yt
    assert run("remove", PL, "aaaaaaaaaa1", "ssssssssss1") == 1
    assert "a suggestion is not a track" in capsys.readouterr().err and "ssssssssss1" not in web.order
    assert web.order == ["aaaaaaaaaa1", "bbbbbbbbbb2", "cccccccccc3"]
    assert not [c for c in web.calls if c[0] == "browse/edit_playlist"]


def test_privacy_prints_and_sets(yt, capsys):
    web, run = yt
    assert run("privacy", PL) == 0
    assert capsys.readouterr().out == f"{PL}\tUnlisted\tFake list\n"
    assert run("privacy", PL, "--set", "private") == 0
    assert capsys.readouterr().out == f"{PL}\tPrivate\tFake list\n" and web.privacy == "Private"


def test_unknown_playlist_and_signed_out_exit_1(yt, capsys, tmp_path):
    _, run = yt
    assert run("list", "PLNOPE") == 1
    net.write_json(tmp_path / "yt-cookies.json", {"synced": "x", "cookies": []})
    assert run("list", PL) == 1
    assert "yt-playlist:" in capsys.readouterr().err
