# Whisper Studio: instructions for installation and maintenance agents

This is a Windows x64 local transcription studio. Read `INSTALL.md` before
installing and `RESTORE.md` for recovery details. The public repository contains
source only; models, recordings, credentials and runtime environments are ignored.

## Installing on another machine

- Run commands from the repository root. Start with
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 -Plan`.
- Use `-Device cpu` when CPU is requested, `-Device cuda` when NVIDIA is requested,
  or `-Device auto` to detect hardware. No Docker or WSL is required.
- Invoke `scripts/install.ps1` directly rather than the BAT launcher, which pauses
  for keyboard input. The installer sets up dependencies and downloads the pinned
  Whisper and Nemotron checkpoints. Do not substitute different models silently.
- Stop this installation's running service before upgrading its libraries.
  Preserve existing projects, credentials and working GPU environments. Do not
  delete `.venv_diarization_gpu` or replace its CUDA build with a CPU build.
- On a fresh installation, Nemotron is selected for speaker diarization and needs
  no Hugging Face token. Pyannote is optional and requires the user's own token.
- Check installer exit codes. Check actual CPU/GPU computation, then start with
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/start.ps1 -NoBrowser`.
  The interface is at `http://127.0.0.1:3000`. Use a user-provided short recording
  to validate both transcription and Nemotron; do not claim inference works from
  dependency checks alone. Report missing prerequisites and the actual device.
- Keep downloaded packages, logs and temporary work inside the checkout where
  possible. `winget` installs required system programs and Hugging Face uses the
  user's standard model cache; these are the documented exceptions.

## Maintaining source

For project summaries, minutes, diagrams and batch artifact export, read
`MATERIALS_API.md`. Agents should use the same revision-checked API as the UI,
not edit artifact manifests directly or automate clicks for bulk operations.

Respect the user's requested scope. Keep `data`, `models`, `vendor`, `bundle`,
Python environments, recordings, cookies and tokens out of commits. Never force-add
ignored runtime files. Public-repository changes must not contain personal data.

Relevant checks after installer/runtime changes:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_installation tests.test_doctor tests.test_components tests.test_model_cache tests.test_speaker_engines
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/install.ps1 -Device cpu -Plan
npm.cmd test
```

Treat CPU and GPU as equally supported installation modes. Preserve automatic
GPU use for existing installations that have no `data/runtime.json` preference.
Do not run a fresh installation over the user's working runtime merely to test
an installer change; use temporary, isolated environments and test doubles.

Existing-entity editors must save on leaving edit mode (outside click, blur,
navigation and Escape), as well as Enter or the checkmark. Never clear drafts
from a global dismissal handler. Project/folder names, folder assignment and
visibility, speaker aliases and dictation history share the inline save queue.
Preserve failed drafts and test exit paths in the browser.
