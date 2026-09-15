import re
import tomllib
import unittest
from pathlib import Path


class DependencyConstraintTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def test_direct_runtime_and_tool_dependencies_are_pinned_for_ci(self):
        with (self.root / "pyproject.toml").open("rb") as stream:
            project = tomllib.load(stream)["project"]
        direct = list(project.get("dependencies", []))
        for values in project.get("optional-dependencies", {}).values():
            direct.extend(values)
        constraints = {
            match.group(1).lower(): match.group(2)
            for line in (self.root / "constraints" / "ci-py311.txt").read_text(encoding="utf-8").splitlines()
            if (match := re.match(r"^([A-Za-z0-9_.-]+)==([^#\s]+)", line.strip()))
        }
        missing = []
        for requirement in direct:
            name = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0].strip().lower()
            if name not in constraints:
                missing.append(name)
        self.assertEqual(missing, [])

    def test_ci_installs_with_constraints_and_avoids_coverage_gate(self):
        workflow = (self.root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("pip install -c constraints/ci-py311.txt", workflow)
        self.assertNotIn("coverage run", workflow)
        self.assertNotIn("fail_under", workflow)


if __name__ == "__main__":
    unittest.main()
