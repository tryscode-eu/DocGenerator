#!/usr/bin/env python3

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


MAX_JSON_BYTES = 1024 * 1024


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_non_finite(_value):
    raise ValueError("non-finite JSON number")


def parse_json_data(raw: bytes):
    if not raw or len(raw) > MAX_JSON_BYTES:
        raise ValueError("document JSON is empty or too large")
    data = json.loads(
        raw,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_non_finite,
    )
    if not isinstance(data, dict):
        raise ValueError("document JSON must be an object")
    return data


def read_private_data_file(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("document data file must be regular")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("document data file permissions must be 0600 or stricter")
        with os.fdopen(fd, "rb") as data_file:
            fd = -1
            return parse_json_data(data_file.read(MAX_JSON_BYTES + 1))
    finally:
        if fd >= 0:
            os.close(fd)


def read_stdin_data():
    return parse_json_data(sys.stdin.buffer.read(MAX_JSON_BYTES + 1))


def resolve_libreoffice_binary():
    """Return a usable LibreOffice CLI without assuming its executable name."""
    configured = os.environ.get("LIBREOFFICE_BIN", "").strip()
    candidates = [configured] if configured else ["libreoffice", "soffice"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    if configured:
        raise RuntimeError(
            "LIBREOFFICE_BIN does not resolve to an executable LibreOffice CLI"
        )
    raise RuntimeError(
        "LibreOffice is required: install 'libreoffice'/'soffice' or set "
        "LIBREOFFICE_BIN"
    )


def extract_content_xml(odt_path):
    with zipfile.ZipFile(odt_path, "r") as odt:
        content = odt.read("content.xml").decode("utf-8")
    print("Extracted content.xml from the ODT file.")
    return content


def replace_variables_in_text(text, data):
    # Replace simple variables
    pattern = re.compile(r"{%\s*(\w+)\s*%}")

    def replace_match(match):
        key = match.group(1)
        value = str(data.get(key, ""))
        print(f"Replacing placeholder '{key}'.")
        return escape(value, {'"': "&quot;", "'": "&apos;"})

    return pattern.sub(replace_match, text)


def process_loops_in_content(content, data):
    loop_pattern = re.compile(
        r"{%!\s*for\s+(\w+)\s+until\s*%}(.*?){%!\s*end\s*%}", re.DOTALL
    )
    iteration = 1
    while True:
        match = loop_pattern.search(content)
        if not match:
            break
        array_name = match.group(1)
        loop_block = match.group(2)
        print(f"\nProcessing loop #{iteration}: array '{array_name}'.")
        if array_name in data and isinstance(data[array_name], list):
            replacement = ""
            for index, item in enumerate(data[array_name], start=1):
                print(f"  Processing iteration {index}.")
                temp_block = loop_block
                # Replace variables in temp_block with item data
                temp_block = replace_variables_in_text(temp_block, item)
                replacement += temp_block
            content = content[: match.start()] + replacement + content[match.end() :]
        else:
            # Remove the loop block if data is not available
            print(
                f"Array '{array_name}' not found or is not a list. Removing loop block."
            )
            content = content[: match.start()] + content[match.end() :]
        iteration += 1
    return content


def replace_variables(content, data):
    print("Starting loop processing...")
    # First process loops
    content = process_loops_in_content(content, data)
    print("Loop processing completed.\n")
    print("Starting variable replacement...")
    # Then replace simple variables
    content = replace_variables_in_text(content, data)
    print("Variable replacement completed.")
    return content


def repackage_odt(original_odt_path, new_odt_path, content_xml):
    # Copy the original ODT file to a new ODT file
    with zipfile.ZipFile(original_odt_path, "r") as zin:
        with zipfile.ZipFile(new_odt_path, "w") as zout:
            for item in zin.infolist():
                if item.filename != "content.xml":
                    buffer = zin.read(item.filename)
                    zout.writestr(item, buffer)
            # Write the modified content.xml
            zout.writestr("content.xml", content_xml)
    print(f"Repackaged ODT file saved as '{new_odt_path}'.")


def convert_to_pdf(odt_path, pdf_path):
    # Use LibreOffice in headless mode to convert ODT to PDF
    print(f"Converting '{odt_path}' to PDF...")
    output_dir = os.path.dirname(pdf_path) or "."
    office_binary = resolve_libreoffice_binary()
    subprocess.run(
        [
            office_binary,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            odt_path,
        ],
        check=True,
    )
    # The output PDF will have the same base name as the input ODT file
    odt_base_name = os.path.splitext(os.path.basename(odt_path))[0]
    generated_pdf = os.path.join(output_dir, odt_base_name + ".pdf")
    if os.path.abspath(generated_pdf) != os.path.abspath(pdf_path):
        shutil.move(generated_pdf, pdf_path)
    print(f"PDF saved as '{pdf_path}'.")


def main(template_path, output_pdf_path, data):
    print("Loading data...")
    print("Data loaded without logging document fields.")
    print("\nExtracting content.xml...")
    content = extract_content_xml(template_path)
    print("Replacing variables and processing loops...")
    content = replace_variables(content, data)
    # Create a temporary directory to store the modified ODT
    with tempfile.TemporaryDirectory() as tmpdirname:
        modified_odt_path = os.path.join(tmpdirname, "modified.odt")
        print("\nRepackaging the ODT file with modified content...")
        repackage_odt(template_path, modified_odt_path, content)
        print("\nConverting the modified ODT file to PDF...")
        convert_to_pdf(modified_odt_path, output_pdf_path)
    print("\nProcessing completed successfully.")


def cli(argv=None):
    parser = argparse.ArgumentParser(
        description="Render an ODT template without placing document data in argv."
    )
    parser.add_argument("template", type=Path)
    parser.add_argument("output_pdf", type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--data-file", type=Path)
    source.add_argument("--data-stdin", action="store_true")
    args = parser.parse_args(argv)
    try:
        data = (
            read_private_data_file(args.data_file)
            if args.data_file is not None
            else read_stdin_data()
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid document data: {type(exc).__name__}")
    main(args.template, args.output_pdf, data)


if __name__ == "__main__":
    cli()
