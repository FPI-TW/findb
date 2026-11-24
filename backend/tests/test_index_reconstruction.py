"""Membership history from snapshots and published changes, without a database."""

from __future__ import annotations

from datetime import date

from app.services.index_reconstruction import (
    ChangeEvent,
    Interval,
    build_history,
    canonical_snapshots,
    is_temporary_counter,
    partition_findings,
)

D = date


def add(day: str, symbol: str, after: int) -> ChangeEvent:
    return ChangeEvent(D.fromisoformat(day), "add", symbol, after, ("add", day, symbol))


def delete(day: str, symbol: str, after: int) -> ChangeEvent:
    return ChangeEvent(D.fromisoformat(day), "delete", symbol, after, ("delete", day, symbol))


def snapshots(**days: set[str]) -> dict[date, frozenset[str]]:
    return {
        D.fromisoformat(day.removeprefix("d").replace("_", "-")): frozenset(v)
        for day, v in days.items()
    }


def test_changes_before_the_first_snapshot_are_rolled_back():
    events = [
        add("2010-03-08", "00005", 3),
        delete("2010-03-08", "00017", 3),
        add("2020-06-08", "00700", 4),
    ]

    history, findings = build_history(
        "00001.00", snapshots(d2026_11_23={"00001", "00002", "00005", "00700"}), events
    )

    assert findings == []
    assert history.observed_from == D(2026, 11, 23)
    assert history.members_on(D(2009, 1, 2)) == {"00001", "00002", "00017"}
    assert history.members_on(D(2015, 1, 2)) == {"00001", "00002", "00005"}
    assert history.intervals() == [
        Interval("00001", None, None),
        Interval("00002", None, None),
        Interval("00005", D(2010, 3, 8), None, ("add", "2010-03-08", "00005")),
        Interval("00017", None, D(2010, 3, 8), None, ("delete", "2010-03-08", "00017")),
        Interval("00700", D(2020, 6, 8), None, ("add", "2020-06-08", "00700")),
    ]


def test_snapshots_decide_their_own_days_and_report_unlisted_changes():
    days = snapshots(
        d2026_10_02={"00001", "00002"},
        d2026_10_05={"00001", "00002", "00003"},
        d2026_10_06={"00001", "00003"},
    )

    history, findings = build_history("00011.00", days, [add("2026-10-05", "00003", 3)])

    assert history.members_on(D(2026, 10, 6)) == {"00001", "00003"}
    assert [(item.rule, dict(item.detail)) for item in findings] == [
        (
            "snapshot_change_not_listed",
            {"day": "2026-10-06", "added": [], "deleted": ["00002"]},
        )
    ]
    assert Interval("00002", None, D(2026, 10, 6)) in history.intervals()


def test_a_listed_change_snapshots_do_not_show_is_reported():
    days = snapshots(d2026_10_02={"00001"}, d2026_10_06={"00001"})

    _, findings = build_history("00011.00", days, [add("2026-10-05", "00009", 2)])

    assert [item.rule for item in findings] == ["listed_change_not_in_snapshots"]


def test_changes_after_the_latest_snapshot_are_applied_as_listed():
    history, _ = build_history(
        "00011.00", snapshots(d2026_11_23={"00001", "00002"}), [add("2026-12-07", "02475", 3)]
    )

    assert history.members_on(D(2026, 12, 7)) == {"00001", "00002", "02475"}
    assert history.members_on(D(2026, 12, 4)) == {"00001", "00002"}


def test_a_code_deleted_and_added_on_one_date_starts_a_new_membership():
    events = [delete("2015-03-18", "00001", 2), add("2015-03-18", "00001", 2)]

    history, findings = build_history("00001.03", snapshots(d2026_11_23={"00001", "00016"}), events)

    assert findings == []
    assert [item for item in history.intervals() if item.symbol == "00001"] == [
        Interval("00001", None, D(2015, 3, 18), None, ("delete", "2015-03-18", "00001")),
        Interval("00001", D(2015, 3, 18), None, ("add", "2015-03-18", "00001")),
    ]


def test_disagreements_while_rolling_back_are_findings_and_keep_the_snapshot():
    events = [add("2026-03-09", "02910", 3), add("2020-01-02", "00005", 2)]

    history, findings = build_history(
        "00011.00", snapshots(d2026_11_23={"00001", "00005", "00009"}), events
    )

    assert [(item.rule, dict(item.detail)) for item in findings] == [
        (
            "change_contradicts_members",
            {"day": "2026-03-09", "added_not_member": ["02910"], "deleted_still_member": []},
        ),
        ("count_after_change", {"day": "2020-01-02", "stated": 2, "derived": 3}),
    ]
    assert history.members_on(D(2026, 11, 23)) == {"00001", "00005", "00009"}
    assert history.members_on(D(2019, 1, 2)) == {"00001", "00009"}


def test_without_a_snapshot_nothing_is_known():
    history, findings = build_history("00011.00", {}, [add("2026-03-09", "00005", 1)])

    assert (history.before, history.after, findings) == (frozenset(), [], [])


def test_temporary_counters_stand_for_the_codes_they_replace():
    assert is_temporary_counter("02915") and not is_temporary_counter("02800")
    raw = {
        "00011.00": {
            D(2026, 10, 2): {"00001", "02915", "99999"},
            D(2026, 10, 8): {"00001", "02670", "99999"},
            D(2026, 11, 20): {"00001", "02670"},
            D(2026, 11, 23): {"00001", "02963"},
        },
        "00011.10": {
            D(2026, 10, 2): {"02915"},
            D(2026, 10, 8): {"02670"},
            D(2026, 11, 20): {"02670"},
            D(2026, 11, 23): set(),
        },
        "00011.12": {
            D(2026, 10, 2): {"00001"},
            D(2026, 10, 8): {"00001"},
            D(2026, 11, 20): {"00001"},
            D(2026, 11, 23): {"00001"},
        },
    }
    raw["00011.12"][D(2026, 11, 20)] = {"00001", "01440"}
    raw["00011.00"][D(2026, 11, 20)] = {"00001", "02670", "01440"}
    raw["00011.00"][D(2026, 11, 23)] = {"00001", "02670", "02963"}
    raw["00011.10"][D(2026, 11, 23)] = {"02670"}
    raw["00011.12"][D(2026, 11, 23)] = {"00001", "02963"}

    canonical, findings = canonical_snapshots(raw, "00011.00")

    assert findings == []
    # Paired by the swap out of the counter, and by the swap into it.
    assert canonical["00011.00"][D(2026, 10, 2)] == {"00001", "02670"}
    assert canonical["00011.10"][D(2026, 10, 2)] == {"02670"}
    assert canonical["00011.00"][D(2026, 11, 23)] == {"00001", "02670", "01440"}
    assert canonical["00011.12"][D(2026, 11, 23)] == {"00001", "01440"}


def test_a_counter_without_a_partner_is_left_out_and_reported():
    canonical, findings = canonical_snapshots(
        {"00011.00": {D(2026, 10, 2): {"00001", "02950"}}}, "00011.00"
    )

    assert canonical["00011.00"][D(2026, 10, 2)] == {"00001"}
    assert [(item.rule, dict(item.detail)) for item in findings] == [
        ("temporary_counter_unpaired", {"day": "2026-10-02", "symbol": "02950"})
    ]


def test_children_must_split_their_parent_exactly_once():
    parent, _ = build_history("00001.00", snapshots(d2026_11_23={"00001", "00005", "00011"}), [])
    finance, _ = build_history("00001.01", snapshots(d2026_11_23={"00005", "00011"}), [])
    utilities, _ = build_history("00001.02", snapshots(d2026_11_23={"00011", "00002"}), [])

    (finding,) = partition_findings(
        "00001.00", parent, {"00001.01": finance, "00001.02": utilities}, [D(2026, 11, 23)]
    )

    assert dict(finding.detail) == {
        "day": "2026-11-23",
        "outside_parent": ["00002"],
        "in_no_child": ["00001"],
        "in_several_children": ["00011"],
    }


def test_a_reused_counter_is_paired_again_for_each_run():
    days = [D(2026, 10, 1), D(2026, 10, 2), D(2026, 10, 5), D(2026, 10, 6), D(2026, 10, 7)]
    root = [
        {"00001", "00002", "00003"},
        {"00001", "02910", "00003"},
        {"00001", "00002", "00003"},
        {"00001", "00002", "02910"},
        {"00001", "00002", "00003"},
    ]

    canonical, findings = canonical_snapshots(
        {"00011.00": dict(zip(days, root, strict=True))}, "00011.00"
    )

    assert findings == []
    assert canonical["00011.00"][D(2026, 10, 2)] == {"00001", "00002", "00003"}
    assert canonical["00011.00"][D(2026, 10, 6)] == {"00001", "00002", "00003"}
