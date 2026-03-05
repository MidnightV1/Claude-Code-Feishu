#!/usr/bin/env python3
"""Compress image to WebP in an isolated subprocess.

Keeps PIL/Pillow native libraries (libwebp, libjpeg, etc.) out of the
main hub process, avoiding ld.so dlopen race conditions on QNAP kernels
when forking Claude CLI subprocesses.

Usage: python compress_image.py <input_path> <output_path> [max_dim] [quality]
Exit 0 on success, non-zero on failure. Prints JSON result to stdout.
"""

import json
import os
import sys


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: compress_image.py <input> <output> [max_dim] [quality]"}))
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]
    max_dim = int(sys.argv[3]) if len(sys.argv) > 3 else 1024
    quality = int(sys.argv[4]) if len(sys.argv) > 4 else 80

    from PIL import Image

    img = Image.open(input_path)
    if max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    img.save(output_path, "WEBP", quality=quality)

    orig_kb = os.path.getsize(input_path) / 1024
    final_kb = os.path.getsize(output_path) / 1024
    print(json.dumps({"orig_kb": round(orig_kb), "final_kb": round(final_kb)}))


if __name__ == "__main__":
    main()
