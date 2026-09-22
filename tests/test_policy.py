import unittest

from src.policy import downgrade_options, rank_bookings, release_gaps


def booking(booking_id, remaining, grade=1, smallholder=False, deferrals=0):
    return {
        "booking_id": booking_id,
        "remaining_freshness_hours": remaining,
        "order_grade": grade,
        "smallholder": smallholder,
        "deferrals": deferrals,
    }


class RankBookingsTest(unittest.TestCase):
    def test_most_urgent_lot_ranks_first(self) -> None:
        ranked = rank_bookings([
            booking("b-宽松", remaining=60),
            booking("b-告急", remaining=12),
            booking("b-一般", remaining=36),
        ])
        self.assertEqual([b["booking_id"] for b in ranked], ["b-告急", "b-一般", "b-宽松"])

    def test_higher_order_grade_wins_when_freshness_close(self) -> None:
        ranked = rank_bookings([
            booking("b-低等级", remaining=24, grade=1),
            booking("b-高等级", remaining=24, grade=3),
        ])
        self.assertEqual([b["booking_id"] for b in ranked], ["b-高等级", "b-低等级"])

    def test_deferred_smallholder_is_not_squeezed_out_forever(self) -> None:
        big_order = booking("b-大单", remaining=20, grade=5)
        smallholder = booking("b-小农", remaining=30, grade=1, smallholder=True, deferrals=3)
        ranked = rank_bookings([big_order, smallholder])
        # 小农批次被挤出 3 次后等效剩余 30-18=12 小时，排到大单前面。
        self.assertEqual([b["booking_id"] for b in ranked], ["b-小农", "b-大单"])

    def test_deferral_relief_is_capped(self) -> None:
        urgent = booking("b-告急", remaining=10, grade=1)
        # 宽减上限 24 小时：等效剩余 40-24=16，仍排在 10 小时之后。
        deferred = booking("b-老批次", remaining=40, grade=1, deferrals=99)
        ranked = rank_bookings([deferred, urgent])
        self.assertEqual([b["booking_id"] for b in ranked], ["b-告急", "b-老批次"])

    def test_input_is_not_mutated(self) -> None:
        bookings = [booking("b-2", remaining=60), booking("b-1", remaining=12)]
        rank_bookings(bookings)
        self.assertEqual([b["booking_id"] for b in bookings], ["b-2", "b-1"])


class ReleaseGapsTest(unittest.TestCase):
    def test_all_steps_done_means_no_gap(self) -> None:
        done = ["SPECIES_CONFIRMED", "SAMPLE_TESTED", "RULE_MATCHED", "CERTIFICATE_ISSUED", "CUSTOMS_CHECKED"]
        self.assertEqual(release_gaps(done), [])

    def test_missing_steps_are_listed(self) -> None:
        gaps = release_gaps(["SPECIES_CONFIRMED"])
        self.assertIn("检验样本合格", gaps)
        self.assertIn("证书签发", gaps)
        self.assertIn("口岸查验核对", gaps)

    def test_corrected_certificate_satisfies_certificate_step(self) -> None:
        done = ["SPECIES_CONFIRMED", "SAMPLE_TESTED", "RULE_MATCHED", "CERTIFICATE_CORRECTED", "CUSTOMS_CHECKED"]
        self.assertEqual(release_gaps(done), [])

    def test_open_holds_block_release(self) -> None:
        done = ["SPECIES_CONFIRMED", "SAMPLE_TESTED", "RULE_MATCHED", "CERTIFICATE_ISSUED", "CUSTOMS_CHECKED"]
        gaps = release_gaps(done, open_holds=1)
        self.assertEqual(gaps, ["存在 1 笔未解除的数量冻结"])


class DowngradeOptionsTest(unittest.TestCase):
    def test_fresh_ok_when_time_allows(self) -> None:
        self.assertEqual(downgrade_options(48), [])

    def test_frozen_or_dried_when_fresh_window_missed(self) -> None:
        self.assertEqual(downgrade_options(20), ["frozen", "dried"])

    def test_dried_only_when_time_is_short(self) -> None:
        self.assertEqual(downgrade_options(4), ["dried"])


if __name__ == "__main__":
    unittest.main()
