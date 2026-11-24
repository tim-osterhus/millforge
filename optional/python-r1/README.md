# Optional Python R1 sidecar

Use this sidecar with the Millforge 0.2.1 npm package and a profile-v4 run.
The installed proof used Linux x86-64, CPython 3.12.14 and a scripted loopback
peer. Select a custodian and restricted run actor before preparing the environment.

`python_exec` runs one cell at a time in one kernel per invocation. The kernel retains variables between calls. A later invocation starts with an empty namespace. Python uses the run actor's operating-system permissions for files, processes and network access.

## Prepare the environment after approval

1. Use Linux x86-64 with glibc 2.26 or later and CPython 3.12.14 with the `cp312` ABI. Record the interpreter build and executable digest.
2. Check that `python-dependencies.json` has SHA-256 `97164d2432cf902dfa7a6b3fb6394d5729ca2cb1d9ddf204b59e12f5b9fc3750`. The manifest pins all 29 wheels, including their public artifact URLs and SHA-256 digests.
3. Download those wheels into an owned staging directory. Match each wheel's name, size and digest to the manifest before installing it.
4. The custodian creates an isolated virtual environment with copied executables, for example `/approved/cpython/bin/python3.12 -m venv --copies --without-pip /protected/python-r1/venv`.
5. Install only the verified local wheel files using the selected installer and the recorded interpreter. For example, use `uv pip install --python /protected/python-r1/venv/bin/python3.12 --offline --no-index --no-deps` followed by the exact wheel paths.
6. Bind the interpreter build, import roots and installed wheel files and versions in the reviewed environment inventory. Check `sys.prefix`, the ABI and imports using the recorded interpreter with `-I -B`.
7. Place `bridge.py` and `nano_output.py` in the protected sidecar directory. Keep their paths, the interpreter and the admission manifest under custodian control.

The run actor must be non-root and distinct from UID 1000 and the Codemode operator. Verify that the actor cannot write or replace the admitted files or their ancestors. Verify that the actor cannot change their permissions. Use a disposable workspace, zero credentials and egress limited to the controlled loopback peer.

## Admit the invocation

The custodian creates a private `millforge-python-admission/v1` JSON manifest. Its keys are `schema`, `interpreter`, `bridge`, `collector`, `environment_sha256` and `lock_sha256`. Each file field is an object with `path` and `sha256`. Paths must be canonical absolute paths to regular files. `environment_sha256` identifies the reviewed inventory. `lock_sha256` is the digest of `python-dependencies.json` above. Record the manifest digest in the operator's admission receipt.

Use `profile.example.json` as the input to `millforge profile create PROFILE_DIR --from profile.example.json`. Set the example endpoint port and operator model metadata to match the controlled peer before creating the profile. Validate with `millforge profile validate PROFILE_DIR`.

Set `MILLFORGE_PYTHON_R1_MANIFEST` to the manifest's absolute path for the selected run actor. Supply the profile's digest and disposable workspace in the `millforge-request/v1` file. Run `millforge run --profile PROFILE_DIR --request REQUEST_JSON --output-dir NEW_RUN_DIR` after the operator admits the environment. Inspect saved records with `millforge inspect --run NEW_RUN_DIR`.

Keep the Apache-2.0 license, Nano RLM MIT license and notice with the sidecar.
Store wheels, Python binaries, the environment inventory and the private
admission manifest in the operator's installation area.
