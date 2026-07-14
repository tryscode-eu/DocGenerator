import os
import zipfile
from unittest.mock import patch

import pytest

import main


def test_replace_variables_does_not_log_values(capsys):
    rendered = main.replace_variables_in_text(
        "Hello {% firstname %} {% lastname %}",
        {"firstname": "PrivateFirst", "lastname": "PrivateLast"},
    )

    assert rendered == "Hello PrivateFirst PrivateLast"
    output = capsys.readouterr().out
    assert "PrivateFirst" not in output
    assert "PrivateLast" not in output


def test_replace_variables_escapes_xml_metacharacters():
    rendered = main.replace_variables_in_text(
        "<text>{% name %}</text>",
        {"name": "A&B <Admin> 'quoted'"},
    )

    assert rendered == ("<text>A&amp;B &lt;Admin&gt; &apos;quoted&apos;</text>")


@pytest.mark.parametrize(
    "raw",
    (
        b'{"name":"first","name":"second"}',
        b'{"value":NaN}',
        b"[]",
        b"",
    ),
)
def test_document_json_is_strict_and_object_only(raw):
    with pytest.raises(ValueError):
        main.parse_json_data(raw)


def test_private_data_file_requires_restrictive_permissions(tmp_path):
    data_file = tmp_path / "document.json"
    data_file.write_text('{"name":"Private Learner"}', encoding="utf-8")
    data_file.chmod(0o600)

    assert main.read_private_data_file(data_file) == {"name": "Private Learner"}

    data_file.chmod(0o644)
    with pytest.raises(ValueError, match="permissions"):
        main.read_private_data_file(data_file)


def test_cli_loads_document_data_outside_argv(tmp_path):
    data_file = tmp_path / "document.json"
    data_file.write_text('{"name":"Private Learner"}', encoding="utf-8")
    data_file.chmod(0o600)

    with patch.object(main, "main") as render:
        main.cli(
            [
                "template.odt",
                "output.pdf",
                "--data-file",
                str(data_file),
            ]
        )

    render.assert_called_once_with(
        main.Path("template.odt"),
        main.Path("output.pdf"),
        {"name": "Private Learner"},
    )


def test_process_loops_renders_each_item_and_removes_missing_arrays():
    content = (
        "{%! for periods until %}<p>{% label %}</p>{%! end %}"
        "{%! for missing until %}<p>hidden</p>{%! end %}"
    )

    rendered = main.process_loops_in_content(
        content,
        {"periods": [{"label": "one"}, {"label": "two"}]},
    )

    assert rendered == "<p>one</p><p>two</p>"


def test_repackage_odt_replaces_only_content_xml(tmp_path):
    source = tmp_path / "template.odt"
    destination = tmp_path / "rendered.odt"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("content.xml", "old")
        archive.writestr("styles.xml", "unchanged")

    main.repackage_odt(source, destination, "new")

    with zipfile.ZipFile(destination) as archive:
        assert archive.read("content.xml") == b"new"
        assert archive.read("styles.xml") == b"unchanged"


def test_resolve_libreoffice_binary_supports_soffice_fallback():
    def fake_which(candidate):
        return "/opt/libreoffice/soffice" if candidate == "soffice" else None

    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(main.shutil, "which", side_effect=fake_which),
    ):
        assert main.resolve_libreoffice_binary() == "/opt/libreoffice/soffice"


def test_resolve_libreoffice_binary_honours_explicit_override():
    with (
        patch.dict(os.environ, {"LIBREOFFICE_BIN": "/custom/office"}, clear=True),
        patch.object(main.shutil, "which", return_value="/custom/office") as which,
    ):
        assert main.resolve_libreoffice_binary() == "/custom/office"
        which.assert_called_once_with("/custom/office")


def test_resolve_libreoffice_binary_fails_clearly_when_missing():
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.object(main.shutil, "which", return_value=None),
    ):
        with pytest.raises(RuntimeError, match="LibreOffice is required"):
            main.resolve_libreoffice_binary()


def test_convert_to_pdf_uses_resolved_binary_and_moves_generated_file(tmp_path):
    source = tmp_path / "modified.odt"
    destination = tmp_path / "final.pdf"
    source.write_bytes(b"odt")

    def fake_run(command, check):
        assert command[0] == "/opt/soffice"
        assert command[1:4] == ["--headless", "--convert-to", "pdf"]
        assert check is True
        (tmp_path / "modified.pdf").write_bytes(b"pdf")

    with (
        patch.object(main, "resolve_libreoffice_binary", return_value="/opt/soffice"),
        patch.object(main.subprocess, "run", side_effect=fake_run),
    ):
        main.convert_to_pdf(source, destination)

    assert destination.read_bytes() == b"pdf"
