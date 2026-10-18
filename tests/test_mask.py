import pytest

from train.mask import mask_reply


@pytest.mark.parametrize(
    "reply, expected",
    [
        # dates
        ("Tuesday works for me!", "[DATE] works for me!"),
        ("How about Monday, Oct 21?", "How about [DATE]?"),
        ("Can we do 10/21 instead?", "Can we do [DATE] instead?"),
        ("Deadline is 2026-11-03.", "Deadline is [DATE]."),
        ("See you on March 3rd, 2026", "See you on [DATE]"),
        ("Let's say the 21st of October.", "Let's say the [DATE]."),
        ("I'll send it tomorrow", "I'll send it [DATE]"),
        ("Free Tue 10/21?", "Free [DATE]?"),
        ("Fri at 2 is fine", "[DATE] at [TIME] is fine"),
        # times
        ("Let's meet at 3pm.", "Let's meet at [TIME]."),
        ("3:30 PM PT works", "[TIME] works"),
        ("I'm free 2-4pm", "I'm free [TIME]"),
        ("10am to 2pm is open", "[TIME] is open"),
        ("lunch at noon?", "lunch at [TIME]?"),
        ("Call me at 10:45", "Call me at [TIME]"),
        ("Let's meet at 3.", "Let's meet at [TIME]."),
        ("Monday at 3 works", "[DATE] at [TIME] works"),
        # amounts
        ("Venmo me $40", "Venmo me [AMOUNT]"),
        ("It's $1,200.50 total", "It's [AMOUNT] total"),
        ("Budget is $5k", "Budget is [AMOUNT]"),
        ("about 40 bucks each", "about [AMOUNT] each"),
        ("Rent is €950", "Rent is [AMOUNT]"),
        ("Somewhere around $40-50", "Somewhere around [AMOUNT]"),
        # phones
        ("Text me at 310-555-1234", "Text me at [PHONE]"),
        ("My cell is (310) 555 1234.", "My cell is [PHONE]."),
        ("Office: +44 20 7946 0958", "Office: [PHONE]"),
        # addresses
        ("I'm at 742 Evergreen Terrace", "I'm at [ADDRESS]"),
        ("Ship to 123 Main St, Apt 4B, Los Angeles, CA 90024.", "Ship to [ADDRESS]."),
        ("Meet at 350 W 5th Ave", "Meet at [ADDRESS]"),
        # links
        ("Slides: https://docs.google.com/d/abc123/edit.", "Slides: [LINK]."),
        ("Join at zoom.us/j/9876543210", "Join at [LINK]"),
        ("Email sarah.chen@ucla.edu", "Email [LINK]"),
        ("check www.example.com", "check [LINK]"),
    ],
)
def test_masks(reply, expected):
    assert mask_reply(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "I have 2 questions about version 3.5.",
        "Room 204 on the 3rd floor.",
        "We're on Python 3.12 now.",
        "Here are my top 10 picks.",
        "Sounds good, thanks!",
        "Talk soon, maybe next week.",
        "I'll get to it at some point.",
        "There were 3 people at 2 tables.",
        "It's 50/50 at this point.",
        "Node.js and e.g. React",
        "I may 3D print it.",
        "Got 2 more on the way",
        "Score was 98.5 out of 100",
    ],
)
def test_leaves_non_facts(reply):
    assert mask_reply(reply) == reply


def test_echoes_facts_given_in_thread():
    thread = "Can you Venmo $40 to 310-555-1234? Details at https://example.com/trip/ and we meet at 12 Oak St."
    reply = "Sent $40 to (310) 555-1234, read example.com/trip, see you at 12 Oak St!"
    assert mask_reply(reply, thread) == reply


def test_masks_facts_not_in_thread():
    thread = "Can you Venmo me for dinner?"
    assert mask_reply("Sent $40, my number is 310-555-1234", thread) == "Sent [AMOUNT], my number is [PHONE]"


def test_dates_and_times_masked_even_if_in_thread():
    thread = "Does Tuesday at 3pm work?"
    assert mask_reply("Tuesday at 3pm works!", thread) == "[DATE] at [TIME] works!"


def test_echo_can_be_disabled():
    thread = "Venmo $40 please"
    assert mask_reply("Sent $40", thread, echo=frozenset()) == "Sent [AMOUNT]"


def test_kept_link_digits_not_masked_as_phone():
    thread = "zoom.us/j/9876543210"
    assert mask_reply("Joining zoom.us/j/9876543210 now", thread) == "Joining zoom.us/j/9876543210 now"


def test_idempotent():
    once = mask_reply("Tuesday at 3pm, $40, call 310-555-1234, https://x.com/a")
    assert mask_reply(once) == once


def test_request_facts_kept_whatever_their_type():
    request = "ask sarah to get dinner tuesday at 7pm, venmo her $40"
    assert mask_reply("Dinner Tuesday at 7pm? I'll send $40", request=request) == "Dinner Tuesday at 7pm? I'll send $40"


def test_facts_not_in_request_still_masked():
    assert mask_reply("Dinner Tuesday at 7pm?", request="ask sarah to dinner tuesday") == "Dinner Tuesday at [TIME]?"
