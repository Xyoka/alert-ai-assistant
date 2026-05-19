from alert_ai_assistant.sanitizer import sanitize_text


def test_sanitize_responsible_person_line_and_configured_names():
    text = "负责人：王超(wangchao01),张晏瑞(zhangyr01)\n告警内容：端口down\n张晏瑞"

    sanitized = sanitize_text(text, ["张晏瑞"])

    assert "wangchao01" not in sanitized
    assert "zhangyr01" not in sanitized
    assert "张晏瑞" not in sanitized
    assert "负责人：<已脱敏>" in sanitized

