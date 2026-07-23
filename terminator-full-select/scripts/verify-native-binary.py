#!/usr/bin/env python3
"""Verify that a native engine matches the contract embedded in the plugin."""

from __future__ import annotations

import argparse
import ast
import ctypes
import hashlib
import os
from pathlib import Path
import stat
import sys


REQUIRED_CONSTANTS = {
    "NATIVE_ENGINE_ABI_VERSION",
    "NATIVE_ENGINE_FRAME_ABI_VERSION",
    "NATIVE_ENGINE_EXPECTED_ABI_SIZES",
    "NATIVE_ENGINE_EXPECTED_BUILD_ID",
    "NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS",
    "NATIVE_ENGINE_EXPECTED_SHA256",
}


class VerificationError(RuntimeError):
    """Raised when a native binary fails the authenticated contract."""


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify a Terminator Full Select native engine against the "
            "contract embedded in full_select_visual_prototype.py."
        )
    )
    parser.add_argument(
        "--plugin",
        required=True,
        type=Path,
        help="Path to full_select_visual_prototype.py.",
    )
    parser.add_argument(
        "--binary",
        required=True,
        type=Path,
        help="Path to libterminator_full_select_engine.so.",
    )
    return parser.parse_args()


def read_contract(plugin_path: Path) -> dict[str, object]:
    try:
        source = plugin_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise VerificationError(
            f"Unable to read plugin source: {exc}"
        ) from exc

    try:
        tree = ast.parse(source, filename=str(plugin_path))
    except SyntaxError as exc:
        raise VerificationError(
            f"Plugin source is not valid Python: {exc}"
        ) from exc

    values: dict[str, object] = {}

    for node in tree.body:
        name: str | None = None
        value_node: ast.expr | None = None

        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            name = node.target.id
            value_node = node.value

        if name not in REQUIRED_CONSTANTS or value_node is None:
            continue

        try:
            values[name] = ast.literal_eval(value_node)
        except (ValueError, TypeError) as exc:
            raise VerificationError(
                f"{name} is not a literal value."
            ) from exc

    missing = sorted(REQUIRED_CONSTANTS.difference(values))
    if missing:
        raise VerificationError(
            "Plugin source is missing native contract constants: "
            + ", ".join(missing)
        )

    expected_sha256 = values["NATIVE_ENGINE_EXPECTED_SHA256"]
    expected_build_id = values["NATIVE_ENGINE_EXPECTED_BUILD_ID"]
    expected_abi = values["NATIVE_ENGINE_ABI_VERSION"]
    expected_frame_abi = values[
        "NATIVE_ENGINE_FRAME_ABI_VERSION"
    ]
    expected_features = values[
        "NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS"
    ]
    expected_sizes = values["NATIVE_ENGINE_EXPECTED_ABI_SIZES"]

    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in expected_sha256
        )
    ):
        raise VerificationError(
            "NATIVE_ENGINE_EXPECTED_SHA256 is not a lowercase SHA-256."
        )

    if not isinstance(expected_build_id, str) or not expected_build_id:
        raise VerificationError(
            "NATIVE_ENGINE_EXPECTED_BUILD_ID is invalid."
        )

    if not isinstance(expected_abi, int) or expected_abi < 1:
        raise VerificationError(
            "NATIVE_ENGINE_ABI_VERSION is invalid."
        )

    if (
        not isinstance(expected_frame_abi, int)
        or expected_frame_abi < 1
    ):
        raise VerificationError(
            "NATIVE_ENGINE_FRAME_ABI_VERSION is invalid."
        )

    if not isinstance(expected_features, int) or expected_features < 0:
        raise VerificationError(
            "NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS is invalid."
        )

    if not isinstance(expected_sizes, dict):
        raise VerificationError(
            "NATIVE_ENGINE_EXPECTED_ABI_SIZES is not a dictionary."
        )

    for required_size in ("abi_info", "frame_abi_info"):
        size_value = expected_sizes.get(required_size)
        if (
            not isinstance(size_value, int)
            or isinstance(size_value, bool)
            or size_value < 8
            or size_value > 1024 * 1024
        ):
            raise VerificationError(
                "NATIVE_ENGINE_EXPECTED_ABI_SIZES has an invalid "
                f"{required_size!r} value."
            )

    return values


def hash_open_file(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(file_descriptor, 0, os.SEEK_SET)

    while True:
        chunk = os.read(file_descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)

    os.lseek(file_descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def open_native_binary(binary_path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        file_descriptor = os.open(binary_path, flags)
    except OSError as exc:
        raise VerificationError(
            f"Unable to open native binary: {exc}"
        ) from exc

    file_status = os.fstat(file_descriptor)

    if not stat.S_ISREG(file_status.st_mode):
        os.close(file_descriptor)
        raise VerificationError(
            "Native binary is not a regular file."
        )

    if file_status.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        os.close(file_descriptor)
        raise VerificationError(
            "Native binary must not be group- or world-writable."
        )

    return file_descriptor


def query_abi(
    library: ctypes.CDLL,
    function_name: str,
    structure_size: int,
    abi_version: int,
) -> int:
    try:
        query_function = getattr(library, function_name)
    except AttributeError as exc:
        raise VerificationError(
            f"Native engine is missing a required export: {function_name}"
        ) from exc

    query_function.argtypes = [ctypes.c_void_p]
    query_function.restype = ctypes.c_int

    probe = ctypes.create_string_buffer(structure_size)
    ctypes.c_uint32.from_buffer(probe, 0).value = structure_size
    ctypes.c_uint32.from_buffer(probe, 4).value = abi_version
    query_status = int(query_function(ctypes.byref(probe)))

    if query_status != 0:
        raise VerificationError(
            f"{function_name} rejected the authenticated contract "
            f"with status {query_status}."
        )

    return query_status


def verify_binary(
    binary_path: Path,
    contract: dict[str, object],
) -> dict[str, object]:
    file_descriptor = open_native_binary(binary_path)

    try:
        actual_sha256 = hash_open_file(file_descriptor)
        expected_sha256 = str(
            contract["NATIVE_ENGINE_EXPECTED_SHA256"]
        )

        if actual_sha256 != expected_sha256:
            raise VerificationError(
                "Native engine SHA-256 mismatch: "
                f"expected {expected_sha256}, found {actual_sha256}."
            )

        loader_path = f"/proc/self/fd/{file_descriptor}"
        loader_mode = (
            getattr(os, "RTLD_LOCAL", 0)
            | getattr(os, "RTLD_NOW", 2)
        )

        try:
            library = ctypes.CDLL(loader_path, mode=loader_mode)
        except OSError as exc:
            raise VerificationError(
                f"Native engine could not be loaded: {exc}"
            ) from exc

        try:
            library.tfse_engine_abi_version.argtypes = []
            library.tfse_engine_abi_version.restype = ctypes.c_uint32
            library.tfse_engine_feature_flags.argtypes = []
            library.tfse_engine_feature_flags.restype = ctypes.c_uint32
            library.tfse_engine_build_id.argtypes = []
            library.tfse_engine_build_id.restype = ctypes.c_char_p
        except AttributeError as exc:
            raise VerificationError(
                f"Native engine is missing a required export: {exc}"
            ) from exc

        actual_abi = int(library.tfse_engine_abi_version())
        actual_features = int(
            library.tfse_engine_feature_flags()
        )
        raw_build_id = library.tfse_engine_build_id()

        if raw_build_id is None:
            raise VerificationError(
                "Native engine returned a null build ID."
            )

        try:
            actual_build_id = raw_build_id.decode(
                "ascii",
                errors="strict",
            )
        except UnicodeDecodeError as exc:
            raise VerificationError(
                "Native engine build ID is not ASCII."
            ) from exc

        expected_abi = int(
            contract["NATIVE_ENGINE_ABI_VERSION"]
        )
        expected_features = int(
            contract["NATIVE_ENGINE_EXPECTED_FEATURE_FLAGS"]
        )
        expected_build_id = str(
            contract["NATIVE_ENGINE_EXPECTED_BUILD_ID"]
        )
        expected_frame_abi = int(
            contract["NATIVE_ENGINE_FRAME_ABI_VERSION"]
        )
        expected_sizes = dict(
            contract["NATIVE_ENGINE_EXPECTED_ABI_SIZES"]
        )

        if actual_abi != expected_abi:
            raise VerificationError(
                "Native engine ABI mismatch: "
                f"expected {expected_abi}, found {actual_abi}."
            )

        if actual_features != expected_features:
            raise VerificationError(
                "Native engine feature flags mismatch: "
                f"expected {expected_features}, "
                f"found {actual_features}."
            )

        if actual_build_id != expected_build_id:
            raise VerificationError(
                "Native engine build ID mismatch: "
                f"expected {expected_build_id!r}, "
                f"found {actual_build_id!r}."
            )

        abi_query_status = query_abi(
            library,
            "tfse_query_abi_v1",
            int(expected_sizes["abi_info"]),
            expected_abi,
        )
        frame_abi_query_status = query_abi(
            library,
            "tfse_query_frame_abi_v2",
            int(expected_sizes["frame_abi_info"]),
            expected_frame_abi,
        )

        return {
            "sha256": actual_sha256,
            "abi_version": actual_abi,
            "frame_abi_version": expected_frame_abi,
            "feature_flags": actual_features,
            "build_id": actual_build_id,
            "abi_query_status": abi_query_status,
            "frame_abi_query_status": frame_abi_query_status,
        }
    finally:
        os.close(file_descriptor)


def main() -> int:
    arguments = parse_arguments()

    try:
        contract = read_contract(arguments.plugin)
        result = verify_binary(arguments.binary, contract)
    except VerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"native_binary_sha256={result['sha256']}")
    print(f"native_engine_abi_version={result['abi_version']}")
    print(
        "native_engine_frame_abi_version="
        f"{result['frame_abi_version']}"
    )
    print(f"native_engine_feature_flags={result['feature_flags']}")
    print(f"native_engine_build_id={result['build_id']}")
    print(f"native_abi_query_status={result['abi_query_status']}")
    print(
        "native_frame_abi_query_status="
        f"{result['frame_abi_query_status']}"
    )
    print("native_binary_contract=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
