from saptest.core.status import MessageType, StatusMessage, from_text


def test_key_is_class_plus_number():
    assert StatusMessage(MessageType.ERROR, "ME", "083").key == "ME083"


def test_key_is_empty_without_structured_identity():
    assert StatusMessage(MessageType.ERROR, text="Enter plant").key == ""


def test_message_type_parsing_is_forgiving():
    assert MessageType.parse("e") is MessageType.ERROR
    assert MessageType.parse(None) is MessageType.NONE
    assert MessageType.parse("nonsense") is MessageType.NONE


def test_only_error_and_abort_are_problems():
    assert StatusMessage(MessageType.ERROR).is_problem
    assert StatusMessage(MessageType.ABORT).is_problem
    assert not StatusMessage(MessageType.WARNING).is_problem
    assert not StatusMessage(MessageType.SUCCESS).is_problem


def test_empty_message_is_detected():
    assert StatusMessage().is_empty
    assert not StatusMessage(text="something").is_empty


def test_from_text_recovers_an_embedded_key():
    assert from_text("Message ME 083 delivery date").key == "ME083"
    assert from_text("no key here").key == ""


def test_from_statusbar_tolerates_missing_properties():
    from saptest.core.status import from_statusbar

    class Partial:
        Text = "Enter plant"

        def __getattr__(self, name):
            raise RuntimeError("property not supported on this screen")

    # Text is present and read; every structured property raises and degrades to "".
    message = from_statusbar(Partial())
    assert message.text == "Enter plant"
    assert message.key == ""
    assert message.type is MessageType.NONE
