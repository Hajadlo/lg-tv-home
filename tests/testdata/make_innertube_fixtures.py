#!/usr/bin/env python3
"""Write the synthetic InnerTube answers in innertube/ (shapes as YouTube's WEB client sends them).

Everything here is made up: channel names, video ids, titles and ids. Run it
again after changing the shapes; the tests read the JSON files it writes.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "innertube"
USER = "100000000000000000001"
BRAND = "200000000000000000002"
CTX = {"responseContext": {"mainAppWebResponseContext": {"datasyncId": f"{BRAND}||{USER}", "loggedOut": False}}}


def text(s):
    return {"runs": [{"text": s}]}


def lockup(vid, title, channel, duration="12:34", progress=None):
    bottom = {"badges": [{"thumbnailBadgeViewModel": {"text": duration}}]}
    if progress is not None:
        bottom = {"progressBar": {"thumbnailOverlayProgressBarViewModel": {"startPercent": progress}}, **bottom}
    return {"lockupViewModel": {
        "contentId": vid, "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
        "contentImage": {"thumbnailViewModel": {"overlays": [
            {"thumbnailBottomOverlayViewModel": bottom},
            {"thumbnailHoverOverlayToggleActionsViewModel": {"buttons": []}}]}},
        "metadata": {"lockupMetadataViewModel": {
            "title": {"content": title},
            "metadata": {"contentMetadataViewModel": {"metadataRows": [
                {"metadataParts": [{"text": {"content": channel}}, {"text": {"content": "1.2K views"}}]}]}}}}}}


def short(vid):
    return {"shortsLockupViewModel": {"entityId": f"shorts-shelf-item-{vid}",
                                      "overlayMetadata": {"primaryText": {"content": "a Short"}}}}


def continuation(token):
    return {"continuationItemRenderer": {"trigger": "CONTINUATION_TRIGGER_ON_ITEM_SHOWN", "continuationEndpoint": {
        "continuationCommand": {"token": token, "request": "CONTINUATION_REQUEST_TYPE_BROWSE"}}}}


def rich(item):
    return {"richItemRenderer": {"content": item}}


def subscriptions():
    items = [
        rich(lockup("fakeLive001", "Live: building a birdhouse", "Woodshop Weekly", "LIVE")),
        rich(lockup("fakeVid0002", "Ten knots you should know", "Rope Corner")),
        rich(lockup("fakeVid0003", "Sourdough, day three", "Crumb Lab", "6:15:17")),
        {"richSectionRenderer": {"content": {"richShelfRenderer": {"title": text("Shorts"), "contents": [
            rich(short("fakeShort01")), rich(short("fakeShort02"))]}}}},
        rich(lockup("fakeVid0004", "Fixing a squeaky door", "Woodshop Weekly", "7:11")),
        rich(lockup("fakeVidPart", "How tides work", "Ocean Notes", "31:48", 27)),
        rich(lockup("fakeVidDone", "Night sky in October", "Star Guide", "18:02", 100)),
        rich(lockup("fakeVidTenP", "Bread scoring patterns", "Crumb Lab", "9:40", 10)),
        rich(lockup("fakeVidSeen", "The quiet history of maps", "Map Room", "44:10", 100)),
        continuation("FAKE_SUBS_CONTINUATION"),
    ]
    return {**CTX, "header": {"feedTabbedHeaderRenderer": {"title": text("Subscriptions")}},
            "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
                "selected": True, "content": {"richGridRenderer": {"contents": items}}}}]}}}


def history():
    def day(title, contents, simple=False):
        head = {"simpleText": title} if simple else text(title)
        return {"itemSectionRenderer": {"header": {"itemSectionHeaderRenderer": {"title": head}},
                                        "contents": contents}}
    sections = [
        day("Today", [lockup("fakeHist001", "Knitting a scarf", "Yarn Club", progress=25),
                      lockup("fakeVidTenP", "Bread scoring patterns", "Crumb Lab", progress=10),
                      lockup("fakeVidDone", "Night sky in October", "Star Guide", progress=100),
                      lockup("fakeHist002", "Repairing a bike chain", "Pedal Shop", progress=100)]),
        day("Yesterday", [lockup("fakeHist003", "A walk through the old town", "Slow Travel", progress=100),
                          lockup("fakeHist004", "Chess openings for beginners", "Board Room", progress=100),
                          lockup("fakeHist005", "Growing tomatoes indoors", "Green Shelf", progress=44),
                          lockup("fakeHist006", "Lofi beats to fold laundry to", "Calm Tunes - Topic", progress=10),
                          lockup("fakeVidPart", "How tides work", "Ocean Notes", progress=27)]),
        day("Sunday", [lockup("fakeHist007", "Paper planes that fly far", "Fold It", progress=100),
                       lockup("fakeHist008", "Making ramen at home", "Crumb Lab", progress=100),
                       lockup("fakeVidSeen", "The quiet history of maps", "Map Room", progress=100),
                       lockup("fakeHist009", "Why cats knock things over", "Pet Science", progress=10),
                       lockup("fakeHist010", "Learning to juggle", "Circus School", progress=10)], simple=True),
        day("Friday", [{"reelShelfRenderer": {"title": text("Shorts"), "items": [short("fakeShort03"),
                                                                               short("fakeShort04")]}}],
            simple=True),
        continuation("FAKE_HISTORY_CONTINUATION"),
    ]
    return {**CTX, "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {
        "selected": True, "content": {"sectionListRenderer": {"contents": sections}}}}]}}}


def channels():
    def channel(i):
        return {"channelRenderer": {"channelId": f"UCfixturechannel{i:02d}xxxxxx", "title": {"simpleText": f"Channel {i}"},
                                    "subscribeButton": {"subscribeButtonRenderer": {"subscribed": True}}}}
    return {**CTX, "contents": {"twoColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {
        "sectionListRenderer": {
            "header": {"chipBarViewModel": {"chips": [{"chipViewModel": {"tapCommand": {"innertubeCommand": {
                "continuationCommand": {"token": "FAKE_CHIP_TOKEN_NOT_A_PAGE"}}}}}]}},
            "contents": [{"itemSectionRenderer": {"contents": [{"shelfRenderer": {"content": {
                "expandedShelfContentsRenderer": {"items": [channel(i) for i in (1, 2, 3)]}}}}]}}]}}}}]}}}


def accounts():
    def account(name, handle, selected, tokens):
        return {"accountItemRenderer": {"accountName": {"simpleText": name}, "isSelected": selected,
                                        "isDisabled": False, "hasChannel": True,
                                        "serviceEndpoint": {"selectActiveIdentityEndpoint": {"supportedTokens": tokens}},
                                        "channelHandle": {"simpleText": handle}}}
    main = account("Main Account", "@mainaccount", True, [
        {"accountStateToken": {"hasChannel": True, "isMerged": True, "obfuscatedGaiaId": USER}},
        {"datasyncIdToken": {"datasyncIdToken": f"{USER}||"}}])
    brand = account("Couch Channel", "@couchchannel", False, [
        {"pageIdToken": {"pageId": BRAND}},
        {"accountStateToken": {"hasChannel": True, "isMerged": True, "obfuscatedGaiaId": BRAND}},
        {"datasyncIdToken": {"datasyncIdToken": f"{BRAND}||{USER}"}}])
    return {"responseContext": {"mainAppWebResponseContext": {"datasyncId": f"{USER}||", "loggedOut": False}},
            "selectText": {"simpleText": "OK"},
            "actions": [{"updateChannelSwitcherPageAction": {"page": {"channelSwitcherPageRenderer": {
                "header": {"channelSwitcherHeaderRenderer": {"title": text("All channels")}},
                "contents": [{"buttonRenderer": {"text": text("Create a channel")}}, main, brand]}}}}]}


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, data in [("fe-subscriptions.json", subscriptions()), ("fe-history.json", history()),
                       ("fe-channels.json", channels()), ("accounts-list.json", accounts())]:
        (OUT / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
        print("wrote", OUT / name)
