# Experimental V8 LA57 backport

This branch investigates [Codex issue #43820](https://github.com/openai/codex/issues/43820).
It is an independent experiment, not an official Codex release. It does not change
installed Codex binaries, running app-servers, or user configuration.

## Change

The V8 source pin remains 15.2.124.1. The source build applies these upstream
changes, adapted to the older source contexts:

- [2f22e9d: initial LA57 support for pointer-table tags](https://github.com/v8/v8/commit/2f22e9ddc83a9e50f4514a2e613b4f905bb1624f)
- [25852db: 32-byte JSDispatchTable entries on x64](https://github.com/v8/v8/commit/25852dbecbcbfe149f0e188577401a0b7927aa39)

The first change moves relevant pointer-table tags to accommodate 57-bit addresses.
The second stores the full Code pointer in its own word, rather than packing it
with the 16-bit parameter count. Code generation and table reservation sizes change
together.

Codex replaces upstream V8's Bazel BUILD with its own engine policy. Consequently,
the patch also enables `V8_SUPPORT_LA57` in that policy. The archive, snapshot
generators, C++ binding, and generated Rust bindings must use the same definitions.

## Build scope

This patch affects **Bazel builds from source**. A normal Cargo build that downloads
an upstream prebuilt rusty_v8 archive does **not** pick up this patch. Existing
published archives and their checksums are unchanged. Do not mix a patched archive
with bindings or snapshots generated for the old layout.

From the repository root, using the Bazel version in `.bazelversion`:

```sh
bazel build --compilation_mode=opt --jobs=4 \
  //codex-rs/code-mode-host:codex-code-mode-host
```

Use separate output and cache directories when isolating the experiment from an
existing build. The resulting host is a diagnostic binary; this document does not
instruct replacing an installed host or restarting a service.

## Offline regression probe

`scripts/check_code_mode_la57.py` starts only the explicitly supplied host binary.
It uses the stdio protocol, a temporary HOME, and no enabled tools or model API.
Core dumps are disabled in the child. The existing address-space limit is inherited.

```sh
python3 scripts/check_code_mode_la57.py \
  /absolute/path/to/codex-code-mode-host --runs 5 --timeout 120
```

The probe checks arithmetic, an array callback, an ordinary function call, and an
async callback in separate cells. It asserts outputs, checks clean shutdown, and
reports high anonymous mappings observed in the host. A whole-probe deadline
bounds hangs; all synchronization uses protocol responses.

Run it on a Linux x86_64 LA57 host with the affected resource limits to exercise
the reported condition. Passing on a 48-bit host alone is insufficient evidence.
The mappings are supporting observations, not a guarantee that every V8 table or
every possible execution path was exercised.

## Validation record

On 2026-11-02, the patched host built successfully from base commit
`18c6dbe52b3b7cef89c13d52efa535ce39bbe27b` on Linux x86_64.

Environment: AMD EPYC 9534 with LA57, Linux
5.14.0-687.31.1.el9_8.x86_64, 200 GiB soft/hard RLIMIT_AS.

| Binary | Arithmetic | Array callback | Ordinary function | Async callback |
| --- | --- | --- | --- | --- |
| Official 0.160.0 (musl) | 2 | SIGSEGV | Not reached | Not reached |
| Official 0.164.0 (musl) | SIGSEGV | Not reached | Not reached | Not reached |
| Patched source build (GNU/Linux) | 2 | [2,3,4] | 42 | [2,4,6] |

The patched host passed all four checks in five fresh processes, each exiting
normally. Every process still had anonymous mappings above 2^48. Example starts
were `0x60886d00000000` and `0x7ec32400000000`. No debugger, address-hint
rewriting, or resource-limit increase was used for these runs.

Patched host SHA-256:
`321d5a64b3b2e044ca479903652f8101169d9f1542b6a7f34318c88804c7a487`.

The existing stdio and settled integration suites both passed with the patched
host (`code-mode-host-stdio-test` and `code-mode-host-settled-test`).
Changed Bazel files passed the pinned Buildifier check. The probe passed pinned
Ruff formatting/lint checks.

Limitations: the official baseline binaries and patched source build differ in
Codex revision and build ABI; this is not a same-revision binary A/B comparison.
The earlier debugger experiment and matching source/instruction analysis provide
independent support for the pointer-truncation mechanism. Other platforms and
full Codex application workflows have not been validated.

This host is not a drop-in replacement for older Codex installations. Use matching
app-server/host versions and validate packaging before deployment. No production
deployment is part of this experiment.
