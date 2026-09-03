from app.storage import GROUP_IDS, all_groups, data_coverage


def test_all_eight_groups_are_readable():
    groups = all_groups()
    assert [group["id"] for group in groups] == list(GROUP_IDS)
    assert all("returnPct" in group for group in groups)


def test_migrated_data_is_visible():
    coverage = {item["key"]: item for item in data_coverage()}
    assert coverage["instruments"]["available"] is True
    assert coverage["financials"]["available"] is True
    assert coverage["kline_daily"]["files"] > 0

