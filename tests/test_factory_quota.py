import unittest

from wqb_agent.factory_quota import (
    carry_forward_quota,
    prepare_quota,
    quota_release,
    quota_remaining,
    quota_reserve,
)
from wqb_agent.weekly_quota import WeeklySimulationQuota


class FactoryQuotaTests(unittest.TestCase):
    def test_legacy_reservation_is_projected_conservatively(self):
        quota = WeeklySimulationQuota(weekly_cap=10, daily_cap=6, clock=lambda: 0)
        session = {"simulations_reserved": 4}
        prepared = prepare_quota(session, quota)
        self.assertIs(prepared, quota)
        self.assertEqual(session["quota"]["weekly_reserved"], 4)
        self.assertEqual(session["quota"]["daily_reserved"], 4)

    def test_lifecycle_delegates_to_single_quota_owner(self):
        quota = WeeklySimulationQuota(weekly_cap=10, daily_cap=10, clock=lambda: 0)
        session = {"quota": quota.initial_state()}
        quota_reserve(session, quota, 3)
        self.assertEqual(quota_remaining(session, quota), 7)
        quota_release(session, quota, 1)
        self.assertEqual(session["simulations_reserved"], 2)

    def test_carry_forward_reuses_normalized_state(self):
        quota = WeeklySimulationQuota(weekly_cap=10, daily_cap=10, clock=lambda: 0)
        state = quota.initial_state()
        state["weekly_reserved"] = 5
        carried = carry_forward_quota({"quota": state}, quota)
        self.assertEqual(carried["weekly_reserved"], 5)


if __name__ == "__main__":
    unittest.main()
