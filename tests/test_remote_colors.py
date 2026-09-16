import unittest

from wqb_agent.remote_colors import preview_remote_colors, sync_remote_colors


def _row(alpha_id="a", color=None):
    return {"alpha_id": alpha_id, "alpha": {
        "status": "DONE", "color": color,
        "is": {"sharpe": 1.5, "fitness": 1.2, "turnover": 0.4,
                "checks": [{"name": "syntax", "pass": True}]},
    }}


class TestRemoteColors(unittest.TestCase):
    def test_preview_consumes_remote_evidence_only(self):
        result = preview_remote_colors([_row()])
        self.assertEqual(result[0]["desired_color"], "GREEN")
        self.assertEqual(result[0]["action"], "DRY_RUN_PATCH")

    def test_sync_dry_run_does_not_patch_and_overwrite_is_explicit(self):
        rows = [_row(color="BLUE")]
        patched = []
        result = sync_remote_colors(
            rows, get_alpha=lambda _alpha_id: {"color": "BLUE"},
            set_alpha_color=lambda *args, **kwargs: patched.append((args, kwargs)),
            dry_run=True,
        )
        self.assertEqual(result[0]["action"], "PRESERVED_EXISTING")
        self.assertEqual(patched, [])

        result = sync_remote_colors(
            rows, get_alpha=lambda _alpha_id: {"color": "BLUE"},
            set_alpha_color=lambda *args, **kwargs: patched.append((args, kwargs)) or {"color": "GREEN"},
            overwrite=True,
        )
        self.assertEqual(result[0]["action"], "PATCHED")
        self.assertEqual(len(patched), 1)


if __name__ == "__main__":
    unittest.main()
