"""Links tests: name folding, markup extraction and the resolver's refusal to
guess. No network — every page here is a fixture and every lane is stubbed."""

import links

SOURCE = "https://visitwicklow.ie/listing/avondale-house-forest-park/"

PAGE = """<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Place","name":"Beyond the Trees Avondale",
 "url":"https://visitwicklow.ie/listing/avondale-house-forest-park/",
 "sameAs":["https://beyondthetrees.ie/","https://www.instagram.com/beyondthetreesavondale/"],
 "offers":{"url":"https://beyondthetrees.ie/tickets"}}
</script></head><body>
<a href="https://www.beyondthetrees.ie/visit">Official site</a>
<a href="https://www.tripadvisor.ie/Attraction_Review-g123">Reviews</a>
</body></html>"""


def test_a_handle_folds_onto_the_venue_name():
    assert links.name_folds("loughkey", "Lough Key Forest & Activity Park") is True


def test_an_unrelated_domain_does_not_fold_onto_the_venue_name():
    assert links.name_folds("visitwicklow", "Beyond the Trees Avondale") is False


def test_a_name_too_short_to_identify_anything_never_folds():
    assert links.name_folds("zoo", "Dublin Zoo") is False


def test_the_core_of_a_name_drops_the_articles_a_handle_would_not_carry():
    assert links.core("The Ark, A Cultural Centre") == "arkculturalcentre"


def test_a_domain_keeping_an_article_the_name_spells_out_still_folds():
    assert links.name_folds("beyondthetrees", "Beyond the Trees Avondale") is True


def test_jsonld_same_as_gives_the_official_site():
    assert links.links_from_jsonld(PAGE, SOURCE)["official_url"] == "https://beyondthetrees.ie/"


def test_jsonld_url_on_the_source_host_is_not_an_official_site():
    page = '<script type="application/ld+json">{"url":"%s"}</script>' % SOURCE
    assert links.links_from_jsonld(page, SOURCE)["official_url"] == ""


def test_jsonld_same_as_gives_the_instagram_account():
    assert links.links_from_jsonld(PAGE, SOURCE)["instagram_url"] == (
        "https://www.instagram.com/beyondthetreesavondale/")


def test_jsonld_offers_give_the_booking_url():
    assert links.links_from_jsonld(PAGE, SOURCE)["booking_url"] == "https://beyondthetrees.ie/tickets"


def test_an_outbound_link_folding_onto_the_venue_name_is_the_official_site():
    page = '<a href="https://www.beyondthetrees.ie/visit">Visit</a>'
    assert links.official_from_outbound(page, SOURCE, "Beyond the Trees Avondale") == (
        "https://www.beyondthetrees.ie/")


def test_an_outbound_link_to_an_aggregator_is_never_the_official_site():
    page = '<a href="https://www.tripadvisor.ie/beyondthetrees">Reviews</a>'
    assert links.official_from_outbound(page, SOURCE, "Beyond the Trees") == ""


def test_a_post_url_is_not_read_as_an_instagram_account():
    assert links.profile_url("https://www.instagram.com/p/CyAbC123/") == ""


def test_an_account_url_keeps_only_the_handle():
    assert links.profile_url("https://www.instagram.com/dublinzoo/?hl=en") == (
        "https://www.instagram.com/dublinzoo/")


def test_a_tiktok_video_url_yields_its_creator_account():
    assert links.profile_url("https://www.tiktok.com/@wee.adventurers_ni/video/7616462561") == (
        "https://www.tiktok.com/@wee.adventurers_ni")


def test_handles_in_html_finds_the_account_a_site_links_to():
    footer = '<a href="https://instagram.com/loughkeyforestpark">Follow us</a>'
    assert links.handles_in_html(footer)[0] == ["loughkeyforestpark"]


def test_a_sites_own_account_is_the_one_named_after_its_domain():
    assert links.site_handle(["wishwithaudrey", "jurassicnewpark"], "Jurassic Haunt",
                             "https://jurassicnewpark.com/") == "jurassicnewpark"


def test_an_embedded_creators_account_is_not_the_sites_own():
    assert links.site_handle(["fionnmaccumhail1", "someoneelse"], "Arklow Arts Centre",
                             "https://arklowartscentre.ie/") == ""


def test_the_only_account_a_site_links_to_is_its_own():
    assert links.site_handle(["bttavondale"], "Beyond the Trees Avondale",
                             "https://www.beyondthetreesavondale.com/") == "bttavondale"


def test_a_search_result_whose_handle_does_not_fold_is_refused(monkeypatch):
    monkeypatch.setattr(links.factory_worker, "_src_gateway",
                        lambda query, limit: [{"url": "https://www.instagram.com/randomparent/",
                                               "title": "A parent in Wicklow"}])
    assert links.social_from_search("instagram", "Beyond the Trees Avondale", "Wicklow") == ""


def test_a_search_result_whose_handle_folds_is_accepted(monkeypatch):
    monkeypatch.setattr(links.factory_worker, "_src_gateway",
                        lambda query, limit: [{"url": "https://www.instagram.com/beyondthetrees/",
                                               "title": "Beyond the Trees (@beyondthetrees)"}])
    assert links.social_from_search("instagram", "Beyond the Trees Avondale", "Wicklow") == (
        "https://www.instagram.com/beyondthetrees/")


def _record(source_url=SOURCE, name="Beyond the Trees Avondale"):
    import contract

    record = contract.EMPTY_RECORD("place")
    record["title"] = name
    record["location"].update({"name": name, "county": "Wicklow"})
    record["links"]["source_url"] = source_url
    return record


def test_resolve_stores_an_official_site_that_answers(monkeypatch):
    monkeypatch.setattr(links, "verify_url", lambda url, timeout=12: True)
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: PAGE)
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    assert links.resolve(_record())["links"]["official_url"] == "https://beyondthetrees.ie/"


def test_resolve_refuses_an_official_site_that_does_not_answer(monkeypatch):
    monkeypatch.setattr(links, "verify_url", lambda url, timeout=12: False)
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: PAGE)
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    assert links.resolve(_record())["links"]["official_url"] == ""


def test_resolve_keeps_an_instagram_account_named_after_the_venue(monkeypatch):
    monkeypatch.setattr(links, "verify_url", lambda url, timeout=12: True)
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: PAGE)
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    assert links.resolve(_record())["links"]["instagram_url"] == (
        "https://www.instagram.com/beyondthetreesavondale/")


def test_resolve_refuses_the_publishers_own_account_off_a_listing_page(monkeypatch):
    """purecork.ie's JSON-LD names @pure_cork on every event it lists."""
    page = ('<script type="application/ld+json">{"@type":"Event",'
            '"sameAs":["https://www.instagram.com/pure_cork/"]}</script>')
    monkeypatch.setattr(links, "verify_url", lambda url, timeout=12: True)
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: page)
    monkeypatch.setattr(links, "wikidata_links", lambda name, county="", timeout=30: {})
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    record = _record("https://www.purecork.ie/whats-on/1120", "Trad Duo and Irish Dancers")
    assert links.resolve(record)["links"]["instagram_url"] == ""


def test_a_social_network_on_another_tld_is_never_an_official_site():
    page = '<script type="application/ld+json">{"url":"https://www.pinterest.co.uk/pure_cork"}</script>'
    assert links.links_from_jsonld(page, SOURCE)["official_url"] == ""


def test_resolve_stamps_when_the_links_were_last_checked(monkeypatch):
    monkeypatch.setattr(links, "verify_url", lambda url, timeout=12: True)
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: PAGE)
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    assert links.resolve(_record())["links"]["links_checked"].startswith("20")


def test_a_social_candidate_on_the_venues_own_account_becomes_its_tiktok_link(monkeypatch):
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: "")
    monkeypatch.setattr(links, "wikidata_links", lambda name, county="", timeout=30: {})
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    record = _record("https://www.tiktok.com/@inflatableworld/video/761646", "Inflatable World")
    assert links.resolve(record, verdict={"is_venue_account": True})["links"]["tiktok_url"] == (
        "https://www.tiktok.com/@inflatableworld")


def test_the_post_url_parked_in_the_social_field_is_replaced_by_the_account(monkeypatch):
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: "")
    monkeypatch.setattr(links, "wikidata_links", lambda name, county="", timeout=30: {})
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    post = "https://www.tiktok.com/@inflatableworld/video/761646"
    record = _record(post, "Inflatable World")
    record["links"]["tiktok_url"] = post
    assert links.resolve(record, verdict={"is_venue_account": True})["links"]["tiktok_url"] == (
        "https://www.tiktok.com/@inflatableworld")


def test_a_social_candidate_from_a_visitors_account_is_not_stored_as_the_venues(monkeypatch):
    monkeypatch.setattr(links, "fetch_html", lambda url, timeout=20: "")
    monkeypatch.setattr(links, "wikidata_links", lambda name, county="", timeout=30: {})
    monkeypatch.setattr(links, "social_from_search", lambda *a: "")
    record = _record("https://www.tiktok.com/@wee.adventurers_ni/video/761646", "Inflatable World")
    assert links.resolve(record, verdict={"is_venue_account": False})["links"]["tiktok_url"] == ""


def test_a_record_with_every_link_is_left_alone_by_the_refresh():
    record = _record()
    record["links"].update({"official_url": "https://x.ie", "instagram_url": "https://i",
                            "tiktok_url": "https://t"})
    assert links.needs_links(record, before="2099-01-01") is False


def test_a_record_checked_yesterday_is_not_rechecked_tonight():
    record = _record()
    record["links"]["links_checked"] = "2026-09-13T00:00:00+00:00"
    assert links.needs_links(record, before="2026-09-01T00:00:00+00:00") is False
