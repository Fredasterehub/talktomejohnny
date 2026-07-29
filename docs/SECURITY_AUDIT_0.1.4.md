# TalkToMeJohnny 0.1.4 security and migration audit

Date: 2026-07-29

## Scope

This release gate covered the current source tree, reachable Git history,
packaged release contents, assistant hook ownership, optional voice-cloning
dependencies, and the non-destructive TalkToMeClaude-to-TalkToMeJohnny state
migration.

The audit deliberately did not collect or publish token values, configuration
contents, transcripts, or voice-reference hashes.

## Results

- Current-tree and reachable-history scans found no Hugging Face, OpenAI,
  GitHub, AWS, or private-key credential patterns outside test fixtures.
- Published history retains legacy product names and non-secret development
  topology references. It was not force-rewritten after publication.
- Hook upgrades accept only exact prior generated entries. Marker-bearing or
  user-authored conflicts continue to fail closed.
- Windows hook commands encode only an absolute TalkToMeJohnny invocation and
  fixed arguments for noninteractive PowerShell. This preserves stdin/stdout
  JSON when Claude Code hosts the command through Bash.
- Codex installation and trust remain separate. Operators must review current
  definitions through `/hooks`; TalkToMeJohnny does not bypass that decision.
- The optional Chatterbox recipe now uses `diffusers==0.38.0`,
  `transformers==5.5.0`, and `setuptools>=83`, replacing versions affected by
  remote-code-execution or package-build advisories. `doctor` warns about older
  installed versions.
- The real Perth watermark remains enabled with setuptools 83. A narrow
  standard-library compatibility bridge supplies the bundled model directory
  that Perth 1.0.1 previously located through removed `pkg_resources` code.
- A full installed-environment `pip-audit` reported no known vulnerabilities.
  The local TalkToMeJohnny packages and custom CUDA Torch/Torchaudio wheels were
  skipped because they are not resolvable as matching PyPI distributions.

Primary advisory records:

- [Diffusers GHSA-7wx4-6vff-v64p](https://github.com/huggingface/diffusers/security/advisories/GHSA-7wx4-6vff-v64p)
- [Diffusers GHSA-98h9-4798-4q5v](https://github.com/huggingface/diffusers/security/advisories/GHSA-98h9-4798-4q5v)
- [Diffusers GHSA-j7w6-vpvq-j3gm](https://github.com/huggingface/diffusers/security/advisories/GHSA-j7w6-vpvq-j3gm)
- [Transformers GHSA-29pf-2h5f-8g72](https://github.com/advisories/GHSA-29pf-2h5f-8g72)
- [Transformers GHSA-fgcw-684q-jj6r](https://github.com/advisories/GHSA-fgcw-684q-jj6r)
- [setuptools GHSA-5rjg-fvgr-3xxf](https://github.com/pypa/setuptools/security/advisories/GHSA-5rjg-fvgr-3xxf)
- [setuptools GHSA-h35f-9h28-mq5c](https://github.com/pypa/setuptools/security/advisories/GHSA-h35f-9h28-mq5c)

## Migration evidence

- Legacy settings are copied forward only when the new namespace does not
  already contain a value; legacy files remain available as recovery sources.
- Existing Rick and Gimli reference files were present in both namespaces and
  matched byte-for-byte before and after dependency and hook upgrades.
- The preferred Hugging Face cache continues to resolve to the populated legacy
  cache when the new cache is absent. No model download or recloning is required.
- Exact pre-release generated skills are migrated to the current
  `talktomejohnny` namespace. User-authored legacy skill content is preserved.

## Runtime evidence

- The final Windows gate passed 825 tests with one platform-specific skip and
  445 state-machine subtests; Ruff, compileall, and diff checks also passed.
- Claude Code executed the native Windows hook through Bash and returned the
  local `not attached` status block without a model call.
- Codex CLI executed the same control path with its one-run reviewed-hook bypass
  and completed with zero input/output tokens. Normal use still requires `/hooks`
  trust.
- The fixed optional dependency set loaded on an RTX 4090 and rendered Rick to a
  valid 24 kHz WAV while retaining the real Perth watermark.
- The stuck-waiting recovery remains covered by state-machine regression tests:
  starting a new recording from the waiting state advances generation and
  invalidates the missing-reply turn.

## Residual risks

- Existing installations are not silently mutated. Operators with old optional
  cloning packages must rerun the current `doctor` recipe to receive fixed
  versions.
- Custom CUDA Torch/Torchaudio wheels require vendor-specific review because the
  PyPI audit could not match them.
- Physical microphone timing, focus, and hotkey behavior remain machine-level
  checks in addition to automated state-machine coverage.
