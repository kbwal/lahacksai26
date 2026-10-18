import re

from pipeline.enron import split_thread

ME = re.compile("dasovich", re.I)


def texts(body):
    conversation, dropped = split_thread(body, ME)
    return [(m["from"], m["text"]) for m in conversation], dropped


def test_outlook_reply():
    body = """thanks.

 -----Original Message-----
From: \tEtringer, Michael
Sent:\tMonday, October 22, 2001 7:36 PM
To:\tDasovich, Jeff
Subject:\tQF Presentation

Here you go...
"""
    assert texts(body) == ([("them", "Here you go..."), ("me", "thanks.")], 0)


def test_notes_chain_oldest_first_and_unwrapped():
    body = """I think the Chancellor's office should make the 
contact.

Best,
Jeff



\tTom Riley/Western Region/The Bentley Company@Exchange
\t02/12/2001 02:58 PM
\t\t 
\t\t To: Jeff Dasovich/NA/Enron@ENRON@EES
\t\t cc: Sandra McCubbin/NA/Enron@Enron, Susan J Mara/NA/Enron@ENRON, Richard 
Shapiro/NA/Enron@Enron
\t\t Subject: RE: FW: Top Level Energy Person

Who should initiate contact?

Tom

-----Original Message-----
From: Jeff Dasovich/NA/Enron@ENRON@EES  On Behalf Of Jeff 
Dasovich/Na/Enron@EES
Sent: Monday, February 12, 2001 11:26 AM
To: Riley, Tom
Subject: Re: FW: Top Level Energy Person

Kari Dohn would be the best candidate.
"""
    assert texts(body) == (
        [
            ("me", "Kari Dohn would be the best candidate."),
            ("them", "Who should initiate contact?\n\nTom"),
            ("me", "I think the Chancellor's office should make the contact.\n\nBest,\nJeff"),
        ],
        0,
    )


def test_forward_with_sender_on_date_line():
    body = """FYI.
---------------------- Forwarded by Jeff Dasovich/SFO/EES on 08/30/2000 06:51 
PM ---------------------------


"Mary Hain" <Mary.Hain@enron.com> on 08/29/2000 08:11:22 PM

\t
To:\tJames D Steffes/HOU/EES@EES, "David W Delainey" 
<David.W.Delainey@enron.com>, Jeff Dasovich/SFO/EES@EES
cc:\t"Christi Nicolay" <Christi.Nicolay@enron.com>
\t
Subject:\tFERC Presentation

Last Thursday, I made the presentation.
"""
    assert texts(body) == ([("them", "Last Thursday, I made the presentation."), ("me", "FYI.")], 0)


def test_quoted_printable_and_gt_quotes():
    body = "sounds good=20\nto me.  see=\n you there=09\n\nOn Mon, Cameron Sellers wrote:\n> are we still on\n> for tonight?\n"
    assert texts(body) == ([("them", "are we still on\nfor tonight?"), ("me", "sounds good to me.  see you there")], 0)


def test_unparsed_header_dropped():
    body = """ok

 -----Original Message-----
From: \tSomeone
Sent:\tMonday
Subject:\thi

see table
                    To:     Jacqueline Kelly
"""
    assert texts(body) == ([("me", "ok")], 1)
