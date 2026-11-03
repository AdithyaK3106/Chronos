"""Turn pytest JUnit XML failures into GitHub ::error annotations.

Annotations are readable through the public API without auth (job logs are not), and they
show the failing test right on the run / PR page. Usage: python junit_annotations.py junit.xml
"""

import sys
import xml.etree.ElementTree as ET


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape(text).replace(":", "%3A").replace(",", "%2C")


def main(path: str) -> None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::warning::no JUnit report to annotate ({exc})")
        return
    count = 0
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is None:
                continue
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (node.get("message") or "") + "\n" + (node.text or "")
            # Keep the tail: the assertion and the line that raised are at the end.
            detail = detail.strip()[-1800:]
            print(f"::error title={_escape_property(name)}::{_escape(detail)}")
            count += 1
    print(f"{count} failing test(s) annotated")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "junit.xml")
