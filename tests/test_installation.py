"""Fresh CPU setup, preservation of existing GPU setup, and installer safety."""
import contextlib
import gc
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend import diarization, runtime, storage, transcriber
from scripts import configure_runtime, install_cuda

ROOT = Path(__file__).resolve().parents[1]


class DeviceSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.config = self.root / "runtime.json"
        self.patch = patch.object(runtime, "CONFIG_PATH", self.config)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("WHISPER_DEVICE", None)

    def test_existing_installation_without_config_keeps_automatic_gpu(self):
        self.assertEqual(runtime.preferred_device(), "auto")

    def test_cpu_preference_and_environment_override(self):
        self.config.write_text('{"device":"cpu"}')
        self.assertEqual(runtime.preferred_device(), "cpu")
        os.environ["WHISPER_DEVICE"] = "cuda"
        self.assertEqual(runtime.preferred_device(), "cuda")

    def test_invalid_config_does_not_break_existing_installation(self):
        self.config.write_text("{broken")
        self.assertEqual(runtime.preferred_device(), "auto")

    def test_whisper_cpu_mode_never_attempts_gpu(self):
        self.config.write_text('{"device":"cpu"}')
        for name in runtime.TURBO_MODEL_FILES:
            (self.root / name).touch()
        transcriber.release_models()
        self.addCleanup(transcriber.release_models)
        with patch.object(transcriber, "RUSSIAN_MODEL_DIR", self.root):
            with patch.object(transcriber, "WhisperModel") as factory:
                _, device = transcriber._load_model("ru")
        self.assertEqual(device, "cpu")
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(factory.call_args.kwargs["device"], "cpu")

    def test_explicit_gpu_failure_is_not_reported_as_cpu_success(self):
        self.config.write_text('{"device":"cuda"}')
        for name in runtime.TURBO_MODEL_FILES:
            (self.root / name).touch()
        transcriber.release_models()
        self.addCleanup(transcriber.release_models)
        with patch.object(transcriber, "RUSSIAN_MODEL_DIR", self.root):
            with patch.object(transcriber, "WhisperModel", side_effect=RuntimeError("driver")) as factory:
                with self.assertRaisesRegex(RuntimeError, "driver"):
                    transcriber._load_model("ru")
        self.assertEqual(factory.call_count, 1)

    def test_cpu_installation_prefers_cpu_diarization_environment(self):
        self.config.write_text('{"device":"cpu"}')
        with patch.object(diarization, "_venv_runtime", side_effect=lambda path: (path, path)) as probe:
            diarization._external_runtime()
        self.assertEqual(probe.call_args.args[0], diarization.CPU_VENV)


class FreshConfigurationTests(unittest.TestCase):
    def test_nemotron_ready_requires_pinned_snapshot(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(diarization, "NEMOTRON_MODEL_CACHE", Path(folder)):
            snapshots = Path(folder) / "snapshots"
            other = snapshots / ("a" * 40)
            other.mkdir(parents=True)
            for name in diarization.NEMOTRON_FILES:
                (other / name).touch()
            self.assertFalse(diarization._nemotron_model_ready())
            pinned = snapshots / runtime.NEMOTRON_REVISION
            pinned.mkdir()
            for name in diarization.NEMOTRON_FILES:
                (pinned / name).touch()
            self.assertTrue(diarization._nemotron_model_ready())

    def test_interrupted_whisper_snapshot_only_fetches_missing_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            english, russian = root / "turbo", root / "turbo-russian"
            for directory, missing in ((english, "tokenizer.json"), (russian, "config.json")):
                directory.mkdir()
                for name in runtime.TURBO_MODEL_FILES:
                    if name != missing:
                        (directory / name).write_bytes(b"existing")

            def download(*, allow_patterns, local_dir, **kwargs):
                for name in allow_patterns:
                    path = Path(local_dir) / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"repaired")

            with patch.multiple(transcriber, MODELS_DIR=root, ENGLISH_MODEL_DIR=english, RUSSIAN_MODEL_DIR=russian), \
                 patch.object(transcriber, "snapshot_download", side_effect=download) as snapshot:
                transcriber.prepare_model_files()
                transcriber.prepare_model_files()
            self.assertEqual([call.kwargs["allow_patterns"] for call in snapshot.call_args_list],
                             [["tokenizer.json"], ["ct2_int8_float16/config.json"]])
            for directory, name in ((english, "tokenizer.json"), (russian, "config.json")):
                self.assertEqual((directory / name).read_bytes(), b"repaired")
                self.assertEqual((directory / "model.bin").read_bytes(), b"existing")

    def test_fresh_nemotron_defaults_and_existing_choices_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with contextlib.ExitStack() as stack:
                stack.callback(gc.collect)
                for name, path in {
                    "DATA_DIR": root, "DB_PATH": root / "whisper.db",
                    "TEMP_DIR": root / "temp", "DICTATIONS_DIR": root / "dictations",
                    "COOKIES_DIR": root / "cookies",
                }.items():
                    stack.enter_context(patch.object(storage, name, path))
                stack.enter_context(patch.object(runtime, "CONFIG_PATH", root / "runtime.json"))
                configure_runtime.configure("cpu")
                settings = storage.get_settings()
                self.assertEqual(settings["diarization_engine"], "nemotron")
                self.assertEqual(settings["diarization_device"], "cpu")
                storage.update_settings({"diarization_engine": "pyannote", "diarization_device": "cuda"})
                configure_runtime.configure("auto")
                settings = storage.get_settings()
                self.assertEqual(settings["diarization_engine"], "pyannote")
                self.assertEqual(settings["diarization_device"], "cuda")


class NvidiaWheelTests(unittest.TestCase):
    def test_extracts_only_nvidia_dlls_without_archive_path_traversal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wheel = root / "fixture.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                for name in install_cuda.REQUIRED:
                    directory = "cublas" if name.startswith("cublas") else "cudnn"
                    archive.writestr(f"nvidia/{directory}/bin/{name}", b"fixture")
                archive.writestr("../../escaped.dll", b"unsafe")
                archive.writestr("nvidia/cudnn/bin/../../escaped.dll", b"unsafe")
            destination = root / "dlls"
            destination.mkdir()
            self.assertEqual(install_cuda.extract_dlls([wheel], destination), install_cuda.REQUIRED)
            self.assertFalse((root / "escaped.dll").exists())
            self.assertEqual({p.name for p in destination.iterdir()}, install_cuda.REQUIRED)

    def test_incomplete_wheels_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wheel = root / "empty.whl"
            with zipfile.ZipFile(wheel, "w"):
                pass
            with self.assertRaisesRegex(RuntimeError, "missing DLLs"):
                install_cuda.extract_dlls([wheel], root)


@unittest.skipUnless(os.name == "nt", "Windows PowerShell installer")
class PowerShellInstallerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node.exe"), "Node required for frontend launch")
    def test_start_from_spaced_folder_and_without_frontend(self):
        with tempfile.TemporaryDirectory(prefix="whisper studio ") as folder:
            root = Path(folder)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/install_helpers.ps1", scripts)
            launcher = (ROOT / "scripts/start.ps1").read_text(encoding="utf-8-sig")
            # Isolate backend discovery and readiness. The frontend argument,
            # executable launch, working directory and output are all real.
            mocks = """
function Get-ServiceProcess($file, $service) {
    if ($service -eq 'backend') { return @{ ProcessId = $PID } }
}
function Wait-ForUrl($url, $seconds) { return $true }
"""
            launcher = launcher.replace("$backendStartMutex =", mocks + "\n$backendStartMutex =", 1)
            launcher = launcher.replace("Local\\WebinarAnalyzerWhisperBackend8765Start", f"Local\\Test-{root.name}")
            (scripts / "start.ps1").write_text(launcher, encoding="utf-8-sig")
            python = root / ".venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(scripts / "start.ps1"), "-NoBrowser"]
            subprocess.run(command + ["-BackendOnly"], check=True, capture_output=True)
            (root / "dist/client/assets").mkdir(parents=True)
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            (root / "dist/server").mkdir()
            (root / "dist/server/index.js").touch()
            (scripts / "start_frontend.mjs").write_text('console.log("frontend-ok")', encoding="utf-8")
            subprocess.run(command, capture_output=True, check=True)
            # The launcher normally waits for /health; our fixture exits instead.
            import time
            deadline = time.monotonic() + 5
            output = root / "logs/frontend.out.log"
            while time.monotonic() < deadline and "frontend-ok" not in output.read_text():
                time.sleep(.05)
            self.assertIn("frontend-ok", output.read_text())

    def run_isolated_install(self, device, *, fail_compute=False, partial_frontend=False):
        """Exercise the real orchestration while replacing downloads/system installs."""
        with tempfile.TemporaryDirectory(prefix="whisper setup ") as folder:
            root = Path(folder)
            scripts = root / "scripts"
            scripts.mkdir()
            if partial_frontend:
                (root / "node_modules/vinext").mkdir(parents=True)
                (root / "dist/client/assets").mkdir(parents=True)
            shutil.copy2(ROOT / "scripts/install_helpers.ps1", scripts)
            installer = (ROOT / "scripts/install.ps1").read_text(encoding="utf-8-sig")
            installer = installer.replace(
                "$installDevice = Get-InstallDevice $Device $hasNvidia",
                "$hasNvidia = ($Device -eq 'cuda')\n$installDevice = Get-InstallDevice $Device $hasNvidia", 1,
            )
            mock_commands = """
function Record-TestCommand([string]$Kind, [string[]]$Values) {
    @{ kind=$Kind; arguments=$Values } | ConvertTo-Json -Compress | Add-Content (Join-Path $projectRoot 'calls.jsonl') -Encoding UTF8
}
function Install-Package { return $true }
function Install-Python311 { return @{ Path='fixture-python'; Arguments=@() } }
function New-PythonVenv([hashtable]$basePython, [string]$target) {
    $directory = Join-Path $target 'Scripts'
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    New-Item -ItemType File -Path (Join-Path $directory 'python.exe') | Out-Null
    return $true
}
function Install-Requirements {
    param([string]$Python, [string]$Wheels, [string[]]$Targets, [string]$Title)
    Record-TestCommand 'packages' $Targets
    return 0
}
function Invoke-External {
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory)
    Record-TestCommand 'process' $Arguments
    if ($Arguments -contains 'scripts.check_runtime' -and FAIL_COMPUTE) { return 1 }
    return 0
}
""".replace("FAIL_COMPUTE", "$true" if fail_compute else "$false")
            position = installer.index('Write-Host "Установка локального Whisper"')
            installer = installer[:position] + mock_commands + installer[position:]
            fixture = scripts / "install.ps1"
            fixture.write_text(installer, encoding="utf-8-sig")
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(fixture),
                "-Device", device, "-CudaVariant", "cu126", *([] if partial_frontend else ["-SkipFrontend"]),
            ], capture_output=True, encoding="utf-8")
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text(encoding="utf-8-sig").splitlines()]
            return result, calls

    def test_full_cpu_flow_uses_cpu_torch_and_skips_cuda_download(self):
        result, calls = self.run_isolated_install("cpu")
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = [c["arguments"] for c in calls if c["kind"] == "packages"]
        self.assertTrue(any("torch==2.11.0+cpu" in arguments for arguments in packages))
        self.assertFalse(any("scripts.install_cuda" in c["arguments"] for c in calls))
        self.assertTrue(any("scripts.check_runtime" in c["arguments"] for c in calls))

    def test_full_gpu_flow_uses_cuda_torch_and_downloads_whisper_dlls(self):
        result, calls = self.run_isolated_install("cuda")
        self.assertEqual(result.returncode, 0, result.stderr)
        packages = [c["arguments"] for c in calls if c["kind"] == "packages"]
        self.assertTrue(any("torch==2.11.0+cu126" in arguments for arguments in packages))
        self.assertFalse(any("torch==2.11.0+cpu" in arguments for arguments in packages))
        self.assertTrue(any("scripts.install_cuda" in c["arguments"] for c in calls))

    def test_failed_device_check_fails_the_installation(self):
        result, _ = self.run_isolated_install("cuda", fail_compute=True)
        self.assertEqual(result.returncode, 1)

    @unittest.skipUnless(shutil.which("npm.cmd"), "npm required for frontend installation")
    def test_install_resumes_incomplete_node_modules_and_build(self):
        result, calls = self.run_isolated_install("cpu", partial_frontend=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = [call["arguments"] for call in calls if call["kind"] == "process"]
        self.assertIn(["ci", "--no-audit", "--no-fund"], commands)
        self.assertIn(["run", "build"], commands)

    def test_cpu_plan_needs_no_cuda_and_makes_no_installation_changes(self):
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
            str(ROOT / "scripts/install.ps1"), "-Device", "cpu", "-Plan",
        ], capture_output=True, encoding="utf-8", check=True)
        plan = json.loads(result.stdout)
        self.assertEqual(plan["selected_device"], "cpu")
        self.assertEqual(plan["torch_variant"], "cpu")
        self.assertFalse(plan["install_cuda_libraries"])

    def test_auto_without_nvidia_and_explicit_cuda_without_driver(self):
        helper = str(ROOT / "scripts/install_helpers.ps1").replace("'", "''")
        code = f"""
$ErrorActionPreference = 'Stop'
. '{helper}'
if ((Get-InstallDevice auto $false) -ne 'cpu') {{ throw 'Wrong CPU choice' }}
if ((Get-InstallDevice auto $true) -ne 'cuda') {{ throw 'Wrong GPU choice' }}
$rejected = $false
try {{ Get-InstallDevice cuda $false }} catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'CUDA must require NVIDIA' }}
"""
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", code], check=True)

    def test_native_arguments_survive_spaces_quotes_and_trailing_slashes(self):
        with tempfile.TemporaryDirectory(prefix="whisper args ") as folder:
            root = Path(folder)
            (root / "echo args.py").write_text("import json,sys; print(json.dumps(sys.argv[1:]))", encoding="utf-8")
            expected = ["", "hello world", 'a"b', "C:\\folder with spaces\\"]
            helper = str(ROOT / "scripts/install_helpers.ps1").replace("'", "''")
            values = [str(root / "echo args.py"), *expected]
            literals = ",".join("'" + item.replace("'", "''") + "'" for item in values)
            script = root / "test.ps1"
            script.write_text(f"""
param([string]$Python)
$ErrorActionPreference = 'Stop'
. '{helper}'
$arguments = @({literals})
$process = Start-Process -FilePath $Python -ArgumentList @($arguments | ForEach-Object {{ ConvertTo-NativeArgument $_ }}) -NoNewWindow -Wait -PassThru
exit $process.ExitCode
""", encoding="utf-8-sig")
            result = subprocess.run([
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                "-Python", sys.executable,
            ], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), expected)


if __name__ == "__main__":
    unittest.main()
