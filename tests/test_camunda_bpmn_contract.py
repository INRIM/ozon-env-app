from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

BPMN = "{http://www.omg.org/spec/BPMN/20100524/MODEL}"
ZEEBE = "{http://camunda.org/schema/zeebe/1.0}"


def test_test_request_bpmn_contract_matches_workers():
    tree = ET.parse(Path("tests/camunda_e2e/test_request.bpmn"))
    root = tree.getroot()

    process = root.find(f".//{BPMN}process[@id='Test_Process']")
    assert process is not None

    task_types = [
        item.attrib["type"]
        for item in root.findall(f".//{ZEEBE}taskDefinition")
    ]
    assert task_types.count("ckeck_user") == 2
    assert "sed_message_approved" in task_types
    assert "sed_message_refused" in task_types

    user_task_ids = {
        item.attrib["id"] for item in root.findall(f".//{BPMN}userTask")
    }
    assert {
        "start_request",
        "resp_see",
        "manager_appvive_refuse",
    }.issubset(user_task_ids)

    conditions = [
        (item.text or "").strip()
        for item in root.findall(f".//{BPMN}conditionExpression")
    ]
    assert "=is_resp = false" in conditions
    assert "=is_resp = true" in conditions
    assert "=approved=true" in conditions
    assert "=refused=true" in conditions
