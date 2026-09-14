"""Media tests: the licence filter, embed id extraction, and the gate's
fail-open contract. No network, no model — every response here is a fixture."""

import contract
import media


def _record(kind="place", source_url="https://loughkey.ie/activities/"):
    record = contract.EMPTY_RECORD(kind)
    record["status"] = "on-air"
    record["id"] = "place-lough-key-roscommon"
    record["title"] = "Lough Key Forest & Activity Park"
    record["location"].update({"name": "Lough Key Forest & Activity Park", "county": "Roscommon"})
    record["links"]["source_url"] = source_url
    return record


# --- licences -------------------------------------------------------------

def test_a_share_alike_licence_may_be_hosted():
    assert media.licence_ok("CC BY-SA 4.0") is True


def test_a_non_commercial_licence_may_not_be_hosted():
    assert media.licence_ok("CC BY-NC 4.0") is False


def test_a_no_derivatives_licence_may_not_be_hosted():
    assert media.licence_ok("CC BY-ND 4.0") is False


def test_an_all_rights_reserved_image_may_not_be_hosted():
    assert media.licence_ok("All rights reserved") is False


def test_an_unlicensed_image_may_not_be_hosted():
    assert media.licence_ok("") is False


COMMONS_PAGE = {
    "imageinfo": [{
        "mime": "image/jpeg",
        "thumburl": "https://upload.wikimedia.org/thumb/Lough_Key.jpg/1600px.jpg",
        "url": "https://upload.wikimedia.org/Lough_Key.jpg",
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Lough_Key.jpg",
        "extmetadata": {
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
            "Artist": {"value": '<a href="/wiki/User:Aoife">Aoife</a>'},
        },
    }]
}


def test_a_commons_file_keeps_the_photographer_name_the_credit_needs():
    assert media._commons_candidate(COMMONS_PAGE)["attribution"] == "Aoife"


def test_a_commons_file_under_a_licence_we_cannot_host_is_dropped():
    page = {"imageinfo": [{**COMMONS_PAGE["imageinfo"][0], "extmetadata": {
        "LicenseShortName": {"value": "All rights reserved"}}}]}
    assert media._commons_candidate(page) is None


def test_a_commons_file_that_is_not_an_image_is_dropped():
    page = {"imageinfo": [{**COMMONS_PAGE["imageinfo"][0], "mime": "application/pdf"}]}
    assert media._commons_candidate(page) is None


def test_an_openverse_result_spells_out_its_full_licence():
    result = {"url": "https://upload.wikimedia.org/Castle.jpg", "license": "by-sa",
              "license_version": "4.0", "creator": "Apiechorowska"}
    assert media._openverse_candidate(result)["licence"] == "CC BY-SA 4.0"


def test_an_openverse_result_under_a_licence_we_cannot_host_is_dropped():
    result = {"url": "https://example.org/x.jpg", "license": "by-nc", "license_version": "4.0"}
    assert media._openverse_candidate(result) is None


def test_a_licensed_photo_is_credited_to_its_photographer_and_licence():
    assert media.credit_line({"attribution": "Aoife", "licence": "CC BY-SA 4.0",
                              "source": "wikimedia-commons"}) == "Photo: Aoife · CC BY-SA 4.0"


def test_a_placeholder_photo_is_credited_to_the_site_it_came_from():
    assert media.credit_line({"attribution": "loughkey.ie", "placeholder": True}) == (
        "Photo: loughkey.ie")


# --- embeds ---------------------------------------------------------------

def test_an_instagram_post_becomes_its_embed_url():
    assert media.embed_for("https://www.instagram.com/p/CyAbC123/")["url"] == (
        "https://www.instagram.com/p/CyAbC123/embed/")


def test_an_instagram_reel_becomes_a_post_embed():
    assert media.embed_for("https://www.instagram.com/reel/DxYz_09/?igsh=1")["url"] == (
        "https://www.instagram.com/p/DxYz_09/embed/")


def test_a_tiktok_video_becomes_its_embed_url():
    assert media.embed_for("https://www.tiktok.com/@wee.adventurers_ni/video/7616462561650740503") == (
        {"platform": "tiktok", "url": "https://www.tiktok.com/embed/v2/7616462561650740503"})


def test_an_account_page_has_nothing_to_embed():
    assert media.embed_for("https://www.instagram.com/dublinzoo/") is None


def test_a_plain_web_page_has_nothing_to_embed():
    assert media.embed_for("https://loughkey.ie/activities/") is None


def test_a_record_found_on_tiktok_embeds_the_video_it_came_from():
    record = _record(source_url="https://www.tiktok.com/@inflatableworld/video/7616462561650740503")
    assert media.build_embeds(record)[0]["platform"] == "tiktok"


def test_the_same_post_is_never_embedded_twice():
    record = _record(source_url="https://www.instagram.com/p/CyAbC123/")
    record["links"]["instagram_url"] = "https://www.instagram.com/p/CyAbC123/"
    assert len(media.build_embeds(record)) == 1


# --- the gate -------------------------------------------------------------

JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00C\x00" + bytes(64) + b"\xff\xd9")


def test_the_gate_reads_the_verdict_and_the_alt_text_from_one_answer():
    text = '{"verdict": "hero", "why": "park fills the frame", "alt": "Lakeside forest park"}'
    assert media._read_verdict(text) == ("hero", "park fills the frame", "Lakeside forest park")


def test_the_gate_still_finds_a_verdict_in_a_model_that_answered_in_prose():
    assert media._read_verdict("This is a logo, so REJECT.")[0] == "reject"


def test_a_gate_outage_is_a_skip_not_a_rejection(monkeypatch):
    monkeypatch.setattr(media, "local_free", lambda: False)
    monkeypatch.setattr(media, "_ask_vision", _raise)
    assert media.gate_photo(JPEG, "Lough Key", "Roscommon")[0] == "skip"


def _raise(*args, **kwargs):
    raise RuntimeError("HTTP 429: rate limited")


def test_a_photo_a_skipped_gate_could_not_judge_is_kept(tmp_path, monkeypatch):
    hero = _one_hero(tmp_path, monkeypatch, verdict="skip")
    assert media.public_path_to_file(hero["url"]).exists() is True


def test_a_rejected_photo_is_deleted_from_disk(tmp_path, monkeypatch):
    assert _one_hero(tmp_path, monkeypatch, verdict="reject") is None


def test_a_hero_carries_the_alt_text_the_gate_wrote(tmp_path, monkeypatch):
    assert _one_hero(tmp_path, monkeypatch)["alt"] == "A lake and a castle"


def test_a_hero_carries_the_licence_it_was_downloaded_under(tmp_path, monkeypatch):
    assert _one_hero(tmp_path, monkeypatch)["licence"] == "CC BY-SA 4.0"


def test_a_hero_is_stored_under_its_kind_and_id(tmp_path, monkeypatch):
    # Relative, no leading slash: the site is served under a path prefix in
    # production, and a leading "/media/..." resolves against the wrong app.
    assert _one_hero(tmp_path, monkeypatch)["url"].startswith("media/place/place-lough-key-roscommon-")


def _one_hero(tmp_path, monkeypatch, verdict="hero"):
    """Run choose_hero against one licensed Commons candidate, no network."""
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(media, "MEDIA_INDEX", tmp_path / "media_index.json")
    monkeypatch.setattr(media, "commons_candidates",
                        lambda name, county="", limit=4: [media._commons_candidate(COMMONS_PAGE)])
    monkeypatch.setattr(media, "openverse_candidates", lambda name, county="", limit=4: [])
    monkeypatch.setattr(media, "fetch_image", lambda url, timeout=30: JPEG)
    monkeypatch.setattr(media, "to_jpeg", lambda data, max_width=media.MAX_WIDTH: data)
    monkeypatch.setattr(media, "gate_photo",
                        lambda data, name, county="": (verdict, "why", "A lake and a castle"))
    return media.choose_hero(_record())


# --- the rule nothing may break ------------------------------------------

def test_an_instagram_image_is_never_downloaded():
    try:
        media.fetch_image("https://scontent.cdninstagram.com/v/t51/photo.jpg")
    except ValueError as error:
        assert "refusing to download" in str(error)
        return
    raise AssertionError("media must never fetch an image from Instagram")


def test_a_tiktok_image_is_never_downloaded():
    try:
        media.fetch_image("https://www.tiktok.com/photo.jpg")
    except ValueError as error:
        assert "refusing to download" in str(error)
        return
    raise AssertionError("media must never fetch an image from TikTok")


def test_a_record_that_already_has_a_licensed_hero_is_left_alone_by_the_refresh():
    record = _record()
    record["media"]["hero"] = {"url": "/media/place/x.jpg", "placeholder": False}
    assert media.needs_media(record) is False


def test_a_record_with_only_a_placeholder_is_tried_again_by_the_refresh():
    record = _record()
    record["media"]["hero"] = {"url": "/media/place/x.jpg", "placeholder": True}
    assert media.needs_media(record) is True


def test_a_photo_no_lane_ever_graded_is_tried_again_by_the_refresh():
    record = _record()
    record["media"]["hero"] = {"url": "/media/place/x.jpg", "gate": "skip"}
    assert media.needs_media(record) is True


def test_a_licensed_photo_replaces_the_placeholder_that_stood_in_for_it():
    placeholder = {"url": "/media/place/og.jpg", "placeholder": True, "gate": "pass"}
    licensed = {"url": "/media/place/commons.jpg", "placeholder": False, "gate": "pass"}
    assert media._rank(licensed) > media._rank(placeholder)


def test_a_hero_whose_photo_a_retry_deleted_is_dropped_from_the_record(tmp_path, monkeypatch):
    """A re-judged photo the gate finally rejects is deleted from disk; the
    record must not keep pointing at it."""
    monkeypatch.setattr(media, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(media, "choose_hero", lambda record: None)
    record = _record()
    record["media"]["hero"] = {"url": "/media/place/gone.jpg", "gate": "skip"}
    assert media.attach(record)["media"]["hero"] is None


def test_a_graded_photo_beats_one_no_lane_could_judge():
    graded = {"url": "/media/place/a.jpg", "gate": "pass"}
    skipped = {"url": "/media/place/b.jpg", "gate": "skip"}
    assert media._rank(graded) > media._rank(skipped)
