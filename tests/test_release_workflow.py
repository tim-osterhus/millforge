from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "publish-to-pypi.yml"

WHEEL = "millforge-0.1.0-py3-none-any.whl"
SDIST = "millforge-0.1.0.tar.gz"
WHEEL_SHA256 = "d9c6f73b4616f120f66aff2bd06138ac5cd0b5c44aac878961dabea404c540d5"
SDIST_SHA256 = "e029a2f19fb2396754833841927cf98c4e45d34290c4050c0569e960f0025f2f"
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "astral-sh/setup-uv": "d0cc045d04ccac9d8b7881df0226f9e82c39688e",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "pypa/gh-action-pypi-publish": ("ba38be9e461d3875417946c167d0b5f3d385a247"),
}


def _workflow() -> str:
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def test_release_workflow_verifies_reviewed_artifact_manifest_before_upload_and_publish() -> (
    None
):
    workflow = _workflow()
    build_job = workflow[workflow.index("  build:") : workflow.index("  publish:")]
    publish_job = workflow[workflow.index("  publish:") :]
    upload = workflow.index(
        f"uses: actions/upload-artifact@{ACTION_PINS['actions/upload-artifact']}"
    )
    download = workflow.index(
        f"uses: actions/download-artifact@{ACTION_PINS['actions/download-artifact']}"
    )
    publish = workflow.index(
        f"uses: pypa/gh-action-pypi-publish@{ACTION_PINS['pypa/gh-action-pypi-publish']}"
    )

    checkout = build_job.index(
        f"uses: actions/checkout@{ACTION_PINS['actions/checkout']}"
    )
    sync = build_job.index("uv sync --frozen --extra dev")
    tests = build_job.index('uv run python -m pytest -m "not live_model_backend"')
    build = build_job.index("run: uv build")
    package_smoke = build_job.index("uv run python scripts/ci_package_smoke.py dist")
    verify = build_job.index("- name: Verify reviewed distributions")
    upload_in_build = build_job.index("uses: actions/upload-artifact@")
    assert checkout < sync < tests < build < package_smoke < verify < upload_in_build

    build_verify = build_job[verify:upload_in_build]
    assert '"millforge-0.1.0-py3-none-any.whl"' in build_verify
    assert '"millforge-0.1.0.tar.gz"' in build_verify
    assert (
        "find dist -maxdepth 1 -type f \\( -name '*.whl' -o -name '*.tar.gz' \\)"
        in build_verify
    )
    assert 'if [[ "${actual[*]}" != "${expected_sorted[*]}" ]]; then' in build_verify
    assert "unexpected distribution set" in build_verify
    assert "exit 1" in build_verify
    assert "cat > dist/SHA256SUMS <<'EOF'" in build_verify
    assert WHEEL_SHA256 in build_verify
    assert SDIST_SHA256 in build_verify
    assert "sha256sum --check --strict SHA256SUMS" in build_verify

    publish_verify = publish_job[
        publish_job.index("- name: Recheck reviewed distributions") : publish_job.index(
            "- name: Publish distributions"
        )
    ]
    assert '"SHA256SUMS"' in publish_verify
    assert '"millforge-0.1.0-py3-none-any.whl"' in publish_verify
    assert '"millforge-0.1.0.tar.gz"' in publish_verify
    assert "find dist -maxdepth 1 -type f -printf '%f\\n' | sort" in publish_verify
    assert 'if [[ "${actual[*]}" != "${expected_sorted[*]}" ]]; then' in (
        publish_verify
    )
    assert "unexpected downloaded file set" in publish_verify
    assert "exit 1" in publish_verify
    assert "cat > expected-SHA256SUMS <<'EOF'" in publish_verify
    assert "cmp --silent expected-SHA256SUMS dist/SHA256SUMS" in publish_verify
    assert WHEEL_SHA256 in publish_verify
    assert SDIST_SHA256 in publish_verify
    assert "sha256sum --check --strict SHA256SUMS" in publish_verify

    assert workflow.index(WHEEL_SHA256) < upload
    assert workflow.index(SDIST_SHA256) < upload
    assert workflow.index("sha256sum --check --strict SHA256SUMS") < upload
    assert workflow.count("sha256sum --check --strict SHA256SUMS") == 2
    assert workflow.count("name: pypi-distributions") == 2
    assert f"dist/{WHEEL}" in workflow[upload:download]
    assert f"dist/{SDIST}" in workflow[upload:download]
    assert "dist/SHA256SUMS" in workflow[upload:download]
    assert download < workflow.rindex(WHEEL_SHA256) < publish
    assert download < workflow.rindex(SDIST_SHA256) < publish
    assert download < workflow.rindex("sha256sum --check --strict SHA256SUMS") < publish
    assert workflow.count("uses: pypa/gh-action-pypi-publish@") == 1
    assert "packages-dir: dist/" in workflow[publish:]
    assert "rm dist/SHA256SUMS" in workflow[download:publish]


def test_release_workflow_refuses_unsafe_publication_surfaces() -> None:
    workflow = _workflow()
    lowered = workflow.lower()
    workflow_header = workflow[: workflow.index("jobs:")]
    build_job = workflow[workflow.index("  build:") : workflow.index("  publish:")]
    publish_job = workflow[workflow.index("  publish:") :]
    build_permissions = re.search(
        r"^    permissions:\n((?:^      [^\n]+\n)+)",
        build_job,
        re.MULTILINE,
    )
    publish_permissions = re.search(
        r"^    permissions:\n((?:^      [^\n]+\n)+)",
        publish_job,
        re.MULTILINE,
    )

    assert "refs/tags/v0.1.0" in workflow
    assert re.search(r"tags:\s*\n\s*- v0\.1\.0\s*$", workflow, re.MULTILINE)
    assert build_job.count("if: github.ref == 'refs/tags/v0.1.0'") == 1
    assert publish_job.count("if: github.ref == 'refs/tags/v0.1.0'") == 1
    assert "environment: pypi" in publish_job
    assert "permissions:" not in workflow_header
    assert build_permissions is not None
    assert build_permissions.group(1).splitlines() == ["      contents: read"]
    assert publish_permissions is not None
    assert publish_permissions.group(1).splitlines() == ["      id-token: write"]
    assert workflow.count("id-token: write") == 1
    for forbidden_trigger in (
        "branches:",
        "pull_request:",
        "release:",
        "repository_dispatch:",
        "schedule:",
        "workflow_call:",
        "workflow_dispatch:",
    ):
        assert forbidden_trigger not in lowered
    assert "skip-existing" not in lowered
    assert "secrets." not in lowered
    assert "api-token:" not in lowered
    assert "password:" not in lowered
    assert "repository-url:" not in lowered
    without_oidc_permission = lowered.replace("id-token: write", "")
    assert not re.search(
        r"(?:^|[^a-z])(?:api[-_]?token|pypi[-_]?token|token)(?:[^a-z]|$)",
        without_oidc_permission,
    )

    uses = re.findall(r"^\s*uses:\s*([^\s]+)$", workflow, re.MULTILINE)
    assert len(uses) == len(ACTION_PINS)
    assert set(uses) == {f"{action}@{commit}" for action, commit in ACTION_PINS.items()}
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", use) for use in uses)
