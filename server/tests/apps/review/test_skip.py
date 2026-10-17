# SPDX-License-Identifier: AGPL-3.0-or-later
"""The auto-skip classifier: which changed files a walkthrough may leave out, and why."""

from __future__ import annotations

import pytest

from jesinia.apps.review.domain.skip import auto_skip_reason


@pytest.mark.parametrize(
    "path",
    [
        "pnpm-lock.yaml",
        "web/package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "bun.lockb",
        "server/uv.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "poetry.lock",
        "Pipfile.lock",
        "go.sum",
        "composer.lock",
        "mix.lock",
        "pubspec.lock",
        "ios/Podfile.lock",
        "flake.lock",
        "deno.lock",
        "./CARGO.LOCK",
    ],
)
def test_should_classify_lockfiles(path: str) -> None:
    assert auto_skip_reason(path, False) == "lockfile"


@pytest.mark.parametrize(
    "path",
    [
        "vendor/github.com/x/y.go",
        "web/node_modules/react/index.js",
        "third_party/zlib/inflate.c",
        "src/third-party/lib.js",
        "bower_components/a.js",
    ],
)
def test_should_classify_vendored_paths(path: str) -> None:
    assert auto_skip_reason(path, False) == "vendored"


@pytest.mark.parametrize(
    "path",
    [
        "static/app.min.js",
        "static/app.min.css",
        "static/app.js.map",
        "api/types.generated.ts",
        "proto/service_pb2.py",
        "proto/service_pb2_grpc.py",
        "proto/service_pb2.pyi",
        "api/service.pb.go",
        "lib/model.g.dart",
        "dist/index.js",
        "packages/ui/build/out.css",
        "src/__generated__/schema.ts",
    ],
)
def test_should_classify_generated_files(path: str) -> None:
    assert auto_skip_reason(path, False) == "generated"


def test_should_classify_binary_files_by_content() -> None:
    assert auto_skip_reason("assets/logo.png", True) == "binary"
    assert auto_skip_reason("assets/logo.png", False) is None


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "build",  # a file named build is not a build directory
        "scripts/dist.sh",
        "docs/lockfiles.md",
        "src/minify.js",
        "vendoring/notes.md",
        "",
    ],
)
def test_should_not_skip_ordinary_source(path: str) -> None:
    assert auto_skip_reason(path, False) is None


def test_lockfile_and_vendored_reasons_win_over_binary() -> None:
    assert auto_skip_reason("vendor/blob.bin", True) == "vendored"
    assert auto_skip_reason("bun.lockb", True) == "lockfile"
    # A vendored lockfile is still reported as a lockfile: the name is checked first.
    assert auto_skip_reason("vendor/x/yarn.lock", False) == "lockfile"
