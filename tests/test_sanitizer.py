from alert_ai_assistant.sanitizer import sanitize_text


def test_sanitize_responsible_person_line_and_configured_names():
    text = "负责人：张三(zhangsan01),李四(lisi01)\n告警内容：端口down\n张三"

    sanitized = sanitize_text(text, ["张三"])

    assert "zhangsan01" not in sanitized
    assert "lisi01" not in sanitized
    assert "张三" not in sanitized
    assert "负责人：<已脱敏>" in sanitized

