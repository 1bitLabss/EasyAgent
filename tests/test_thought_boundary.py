"""The first answer word is not part of the thought."""

from easyagent.llm import drop_answer_leak


def test_the_answer_word_leaves_the_thought():
    thought = (
        "Let me do these in parallel where possible.\n\n"
        "user asked to test memory and run the script.\n"
        "Everything"
    )
    answer = "Everything's operational. What's next?"
    assert drop_answer_leak(thought, answer) == (
        "Let me do these in parallel where possible.\n\n"
        "user asked to test memory and run the script."
    )
    assert drop_answer_leak(thought, "A different reply.") == thought
    assert drop_answer_leak("", answer) == ""
