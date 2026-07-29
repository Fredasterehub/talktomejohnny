"""Tests for the hardware advisor: GPU detection parsing and recommendations."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from talktomeclaude import advisor
from talktomeclaude.advisor import GPU, Machine


def _machine(gpus=()):
    return Machine(
        os="Linux 6.1",
        arch="x86_64",
        python="3.12.9",
        cpu_count=20,
        ram_gb=64.0,
        gpus=tuple(gpus),
    )


class NvidiaSmiParseTests(unittest.TestCase):
    def test_absent_nvidia_smi_yields_no_gpus(self) -> None:
        with mock.patch.object(advisor.shutil, "which", return_value=None):
            self.assertEqual(advisor._nvidia_smi_gpus(), ())

    def test_parses_csv_rows(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout="NVIDIA GeForce RTX 5060 Ti, 16311, 12.0\nTesla T4, 15360, 7.5\n",
            stderr="",
        )
        with mock.patch.object(advisor.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch.object(advisor.subprocess, "run", return_value=completed):
            gpus = advisor._nvidia_smi_gpus()
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0], GPU("NVIDIA GeForce RTX 5060 Ti", 16311, "12.0"))
        self.assertEqual(gpus[1].compute_cap, "7.5")

    def test_smi_failure_is_swallowed(self) -> None:
        with mock.patch.object(advisor.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch.object(advisor.subprocess, "run", side_effect=OSError):
            self.assertEqual(advisor._nvidia_smi_gpus(), ())


class ComputeCapTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(advisor.compute_cap_tuple("12.0"), (12, 0))
        self.assertEqual(advisor.compute_cap_tuple(" 8.6 "), (8, 6))
        self.assertIsNone(advisor.compute_cap_tuple(""))
        self.assertIsNone(advisor.compute_cap_tuple("weird"))


class RecommendTests(unittest.TestCase):
    def test_no_gpu_not_feasible_but_suggests_piper(self) -> None:
        rec = advisor.recommend(_machine())
        self.assertFalse(rec.clone_feasible)
        self.assertEqual(rec.clone_recipe, ())
        self.assertTrue(rec.stt_tier.startswith("CPU"))
        self.assertTrue(any("Piper" in note for note in rec.notes))

    def test_blackwell_feasible_with_cu128_recipe(self) -> None:
        rec = advisor.recommend(_machine([GPU("RTX 5060 Ti", 16311, "12.0")]))
        self.assertTrue(rec.clone_feasible)
        recipe = "\n".join(rec.clone_recipe)
        self.assertIn("download.pytorch.org/whl/cu128", recipe)
        self.assertIn("--no-deps chatterbox-tts==0.1.7", recipe)
        self.assertIn("transformers==5.5.0", recipe)
        self.assertIn("diffusers==0.38.0", recipe)
        self.assertIn("setuptools>=83", recipe)
        self.assertNotIn("transformers==5.2.0", recipe)
        self.assertNotIn("diffusers==0.29.0", recipe)
        self.assertTrue(rec.stt_tier.startswith("GPU"))
        self.assertTrue(any("cu128" in note for note in rec.notes))

    def test_recipe_uses_uv_when_uv_present(self) -> None:
        with mock.patch.object(advisor.shutil, "which", side_effect=lambda name: "/usr/bin/uv" if name == "uv" else None):
            recipe = advisor.clone_install_recipe()
        self.assertTrue(all(cmd.startswith("uv pip install") for cmd in recipe))

    def test_recipe_uses_python_pip_without_uv(self) -> None:
        with mock.patch.object(advisor.shutil, "which", return_value=None):
            recipe = advisor.clone_install_recipe()
        self.assertTrue(all(cmd.startswith("python -m pip install") for cmd in recipe))

    def test_clone_security_warnings_flag_only_versions_below_fixed_floors(
        self,
    ) -> None:
        versions = {
            "diffusers": "0.29.0",
            "setuptools": "83.0.0",
            "transformers": "5.5.0",
        }
        with mock.patch.object(
            advisor, "package_version", side_effect=lambda name: versions[name]
        ):
            warnings = advisor.clone_security_warnings()

        self.assertEqual(len(warnings), 1)
        self.assertIn("diffusers 0.29.0", warnings[0])
        self.assertIn("0.38.0", warnings[0])

    def test_clone_security_warnings_ignore_absent_optional_stack(self) -> None:
        with mock.patch.object(
            advisor, "package_version", side_effect=advisor.PackageNotFoundError
        ):
            self.assertEqual(advisor.clone_security_warnings(), ())

    def test_recommend_picks_the_most_capable_gpu(self) -> None:
        rec = advisor.recommend(
            _machine([GPU("iGPU", 512, "6.1"), GPU("RTX 5060 Ti", 16311, "12.0")])
        )
        self.assertTrue(rec.clone_feasible)
        self.assertIn("5060 Ti", rec.clone_reason)

    def test_falls_back_when_compute_cap_query_rejected(self) -> None:
        rows = [["Tesla K80", "12288"]]
        with mock.patch.object(advisor.shutil, "which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch.object(advisor, "_run_smi", side_effect=[None, rows]):
            gpus = advisor._nvidia_smi_gpus()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].compute_cap, "")
        self.assertEqual(gpus[0].vram_mb, 12288)

    def test_old_gpu_not_feasible(self) -> None:
        rec = advisor.recommend(_machine([GPU("GTX 1080", 8192, "6.1")]))
        self.assertFalse(rec.clone_feasible)
        self.assertEqual(rec.clone_recipe, ())

    def test_low_vram_feasible_but_warned(self) -> None:
        rec = advisor.recommend(_machine([GPU("RTX 3050 Laptop", 4096, "8.6")]))
        self.assertTrue(rec.clone_feasible)
        self.assertTrue(any("VRAM" in note for note in rec.notes))

    def test_unknown_compute_cap_not_feasible(self) -> None:
        rec = advisor.recommend(_machine([GPU("Mystery GPU", 8192, "")]))
        self.assertFalse(rec.clone_feasible)

    def test_report_has_sections(self) -> None:
        report = advisor.format_report(_machine([GPU("RTX 5060 Ti", 16311, "12.0")]))
        self.assertIn("Hardware", report)
        self.assertIn("Recommendation", report)
        self.assertIn("Install the cloning engine", report)
        self.assertIn("cu128", report)


if __name__ == "__main__":
    unittest.main()
