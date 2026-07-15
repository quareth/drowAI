import json
from agent.tools.web_applications import parsing_utils as utils


def test_clean_output_strips_ansi_and_whitespace():
    raw = " \x1b[31mtest\x1b[0m \n"
    assert utils.clean_output(raw) == "test"


def test_clean_output_truncates_length():
    assert utils.clean_output("abcdef", max_length=3) == "abc"


def test_detect_format_variants():
    assert utils.detect_format('{"a":1}') == "json"
    assert utils.detect_format("<root></root>") == "xml"
    assert utils.detect_format("col1,col2\n1,2") == "csv"
    assert utils.detect_format("") == "text"


def test_parse_crawler_line_variants():
    gobuster_line = "/admin (Status: 200) [Size: 123]"
    ferox_line = "/img (Status: 301)"
    malformed_line = "not a hit"
    assert utils.parse_crawler_line(gobuster_line) == {"path": "/admin", "status": 200, "size": 123}
    assert utils.parse_crawler_line(ferox_line)["status"] == 301
    assert utils.parse_crawler_line(malformed_line) is None


def test_parse_json_output_object_array_and_line_by_line():
    obj = utils.parse_json_output('{"a":1,"b":2}')
    arr = utils.parse_json_output('[{"a":1},{"a":2}]')
    lines = utils.parse_json_output('{"a":1}\n{"a":2}', line_by_line=True)
    assert obj["data"] == [{"a": 1, "b": 2}]
    assert arr["data"][0]["a"] == 1 and arr["data"][1]["a"] == 2
    assert lines["data"] == [{"a": 1}, {"a": 2}]


def test_parse_json_output_error_and_empty():
    bad = utils.parse_json_output("{bad")
    empty = utils.parse_json_output("")
    assert bad["error"] is not None
    assert empty["data"] == []


def test_parse_xml_output_valid_and_error():
    xml = "<root><child key='1'>val</child></root>"
    parsed = utils.parse_xml_output(xml, root_element="root")
    assert parsed["elements"][0]["tag"] == "root"
    assert parsed["elements"][0]["children"][0]["tag"] == "child"
    assert parsed["attributes"] == {}
    error = utils.parse_xml_output("<root>", root_element="root")
    assert error["error"] is not None


def test_parse_csv_output_with_and_without_header():
    csv_text = "a,b\n1,2\n3,4\n"
    parsed = utils.parse_csv_output(csv_text, has_header=True)
    assert parsed["headers"] == ["a", "b"]
    assert parsed["row_count"] == 2
    no_header = utils.parse_csv_output("1;2\n3;4", delimiter=";", has_header=False)
    assert no_header["rows"][0]["columns"] == ["1", "2"]


def test_normalize_severity_variants():
    assert utils.normalize_severity("Critical") == "Critical"
    assert utils.normalize_severity("high") == "High"
    assert utils.normalize_severity(9.1) == "Critical"
    assert utils.normalize_severity(2) == "Low"
    assert utils.normalize_severity("info", output_format="simple") == "Low"
    assert utils.normalize_severity("7.5", output_format="cvss") == "7.0"


def test_extract_vulnerabilities_from_dict_and_list():
    data_dict = {"vulnerabilities": [{"type": "xss", "severity": "high", "url": "http://a"}]}
    data_list = [{"issues": [{"name": "sqli", "risk": 5, "path": "/login"}]}]
    extracted_dict = utils.extract_vulnerabilities(data_dict)
    extracted_list = utils.extract_vulnerabilities(data_list)
    assert extracted_dict[0]["severity"] == "High"
    assert extracted_list[0]["location"] == "/login"
    assert extracted_list[0]["severity"] == "High"


def test_parse_json_output_extract_nested_keys():
    payload = json.dumps({"summary": {"count": 1}, "vulnerabilities": [{"severity": "medium"}]})
    result = utils.parse_json_output(payload, default_keys=["summary"], extract_nested=True)
    assert result["summary"]["summary"]["count"] == 1
    assert result["summary"]["vulnerabilities"][0]["severity"] == "medium"

